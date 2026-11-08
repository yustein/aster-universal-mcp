"""Regression coverage for refresh rotation under failure and concurrency."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull
from provider_adapter import LocalOAuthProvider


class AtomicRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = str(Path(temp.name) / "oauth.sqlite")
        origin = "https://refresh-test.invalid"
        self.resource = origin + "/mcp"
        self.provider = LocalOAuthProvider(self.db_path, issuer=origin, resource=self.resource)
        self.client = OAuthClientInformationFull(
            client_id="test-client", client_name="Refresh regression test",
            redirect_uris=["https://chatgpt.com/connector/oauth/test"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], token_endpoint_auth_method="none",
        )
        await self.provider.register_client(self.client)
        self.original = self.provider.refresh.issue("test-client", self.resource, "system:admin")
        self.grant = await self.provider.load_refresh_token(self.client, self.original)
        self.assertIsNotNone(self.grant)

    async def exchange(self):
        return await self.provider.exchange_refresh_token(self.client, self.grant, [])

    async def test_replacement_write_failure_leaves_original_valid(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("""CREATE TRIGGER reject_replacement BEFORE INSERT ON refresh_tokens
                          BEGIN SELECT RAISE(ABORT, 'injected replacement write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.exchange()
        self.assertIsNotNone(await self.provider.load_refresh_token(self.client, self.original))
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM access_tokens").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_replacement")
        replacement = await self.exchange()
        self.assertIsNotNone(await self.provider.load_access_token(replacement.access_token))
        self.assertIsNotNone(await self.provider.load_refresh_token(self.client, replacement.refresh_token))
        self.assertIsNone(await self.provider.load_refresh_token(self.client, self.original))

    async def test_concurrent_exchange_has_one_winner(self):
        results = await asyncio.gather(self.exchange(), self.exchange(), return_exceptions=True)
        failure, = (result for result in results if isinstance(result, TokenError))
        self.assertEqual(failure.error, "invalid_grant")
        success, = (result for result in results if not isinstance(result, Exception))
        self.assertIsNotNone(await self.provider.load_refresh_token(self.client, success.refresh_token))
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM access_tokens").fetchone()[0], 1)
