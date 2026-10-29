"""Authenticated event stream for an external browser wake relay.

This exposes only existing coordination event metadata. Task instructions,
results, OAuth state, leases, and credentials never cross this stream.
"""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
import stat
import threading
from urllib.parse import parse_qs


class EventSignal:
    def __init__(self):
        self._condition = threading.Condition()
        self._generation = 0

    def notify(self):
        with self._condition:
            self._generation += 1
            self._condition.notify_all()

    def marker(self):
        with self._condition:
            return self._generation

    def wait(self, marker, timeout=25.0):
        with self._condition:
            if self._generation == marker:
                self._condition.wait(timeout)
            return self._generation


def _load_secret(path: Path) -> str:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("wake relay secret must be a regular mode-0600 file")
    value = path.read_text().strip()
    if len(value) < 32 or any(ch.isspace() for ch in value):
        raise ValueError("wake relay secret is invalid")
    return value


class WakeStream:
    PATH = "/wake-stream"

    def __init__(self, app, *, store_getter, signal: EventSignal,
                 secret_path: Path, public_host: str):
        self.app = app
        self.store_getter = store_getter
        self.signal = signal
        self.secret_path = secret_path
        self.public_host = public_host

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "websocket" or scope.get("path") != self.PATH:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin1").lower(): v.decode("latin1")
                   for k, v in scope.get("headers", [])}
        expected = "Bearer " + _load_secret(self.secret_path)
        if (headers.get("host") != self.public_host
                or headers.get("x-forwarded-proto") != "https"
                or not hmac.compare_digest(headers.get("authorization", ""), expected)):
            await send({"type": "websocket.close", "code": 4401, "reason": "unauthorized"})
            return
        try:
            query = parse_qs(scope.get("query_string", b"").decode("ascii"))
            after = int(query.get("after", ["0"])[0])
            if after < 0:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            await send({"type": "websocket.close", "code": 4400, "reason": "bad cursor"})
            return
        await send({"type": "websocket.accept"})
        cursor = after
        marker = self.signal.marker()
        try:
            while True:
                events = self.store_getter().events_after(after=cursor, limit=100)
                if events:
                    for event in events:
                        await send({"type": "websocket.send", "text": json.dumps(
                            {"type": "event", "event": event}, separators=(",", ":"))})
                        cursor = event["sequence"]
                    marker = self.signal.marker()
                    continue
                next_marker = await asyncio.to_thread(self.signal.wait, marker, 25.0)
                if next_marker == marker:
                    await send({"type": "websocket.send", "text": json.dumps(
                        {"type": "heartbeat", "cursor": cursor}, separators=(",", ":"))})
                marker = next_marker
        except Exception:
            try:
                await send({"type": "websocket.close", "code": 1000})
            except Exception:
                pass
