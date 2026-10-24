"""Bounded ASGI form inspection must preserve valid SDK token requests."""
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oauth_token_resource import MAX_TOKEN_FORM_BYTES, TokenResourceBinding

RESOURCE = "https://mcp.example.invalid/mcp"


class TokenResourceBindingTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, body, *, split=None, content_type=b"application/x-www-form-urlencoded"):
        sent, forwarded = [], []
        chunks = [body] if split is None else [body[:split], body[split:]]
        messages = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                    for index, chunk in enumerate(chunks)]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        async def app(scope, receive, send):
            request = await receive()
            forwarded.append(request["body"])
            self.assertFalse(request.get("more_body", False))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        guard = TokenResourceBinding(app, resource=RESOURCE)
        await guard({"type": "http", "method": "POST", "path": "/token",
                     "headers": [(b"content-type", content_type)]}, receive, send)
        return sent, forwarded

    async def test_wrong_blank_or_duplicate_resource_never_reaches_grant_consumer(self):
        for values in [["https://elsewhere.invalid/mcp"], [""], [RESOURCE, RESOURCE],
                       [RESOURCE, "https://elsewhere.invalid/mcp"]]:
            with self.subTest(resources=values):
                body = urlencode({"grant_type": "refresh_token", "refresh_token": "private-test-value",
                                  "resource": values}, doseq=True).encode()
                sent, forwarded = await self.call(body, split=7)
                self.assertEqual(sent[0]["status"], 400)
                self.assertEqual(json.loads(sent[1]["body"]), {"error": "invalid_target"})
                self.assertIn((b"cache-control", b"no-store"), sent[0]["headers"])
                self.assertNotIn(b"private-test-value", sent[1]["body"])
                self.assertEqual(forwarded, [])

    async def test_valid_chunked_form_replays_original_bytes_unchanged(self):
        body = urlencode({"grant_type": "refresh_token", "resource": RESOURCE,
                          "refresh_token": "private+test value", "client_id": "owner-client"}).encode()
        sent, forwarded = await self.call(body, split=31,
                                         content_type=b"application/x-www-form-urlencoded; charset=utf-8")
        self.assertEqual(sent[0]["status"], 204)
        self.assertEqual(forwarded, [body])

    async def test_omitted_resource_retains_single_bound_resource_compatibility(self):
        body = b"grant_type=refresh_token&refresh_token=private-test-value"
        sent, forwarded = await self.call(body)
        self.assertEqual(sent[0]["status"], 204)
        self.assertEqual(forwarded, [body])

    async def test_oversized_or_unparseable_body_does_not_reach_sdk(self):
        cases = [(b"x=" + b"x" * MAX_TOKEN_FORM_BYTES, 413),
                 (b"resource=%FF", 400),
                 (b"&".join([b"field=value"] * 129), 400)]
        for body, status in cases:
            with self.subTest(status=status, size=len(body)):
                sent, forwarded = await self.call(body, split=10)
                self.assertEqual(sent[0]["status"], status)
                self.assertEqual(json.loads(sent[1]["body"]), {"error": "invalid_request"})
                self.assertEqual(forwarded, [])

    async def test_non_form_token_request_cannot_bypass_resource_validation(self):
        sent, forwarded = await self.call(b'{"resource":"https://elsewhere.invalid/mcp"}',
                                         content_type=b"application/json")
        self.assertEqual(sent[0]["status"], 400)
        self.assertEqual(forwarded, [])


if __name__ == "__main__":
    unittest.main()
