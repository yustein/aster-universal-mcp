#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from websockets.asyncio.client import connect

CONFIG = Path(os.environ.get(
    "ASTER_CHAT_WAKE_CONFIG",
    "/home/tony/tools/aster-chat-wake-relay/config.json",
))
STATE = Path(os.environ.get(
    "ASTER_CHAT_WAKE_STATE",
    "/home/tony/.local/state/aster-chat-wake-relay/state.sqlite",
))


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


class DeliveryError(RuntimeError):
    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(message)


class State:
    def __init__(self, path: Path, initial_cursor: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS receipts(
          wake_key TEXT PRIMARY KEY,
          sequence INTEGER NOT NULL,
          event_id TEXT NOT NULL,
          task_id TEXT NOT NULL,
          recipient TEXT NOT NULL,
          status TEXT NOT NULL,
          message_sha256 TEXT NOT NULL,
          conversation_url TEXT NOT NULL,
          attempts INTEGER NOT NULL,
          last_error TEXT,
          delivered_at REAL
        )""")
        if self.db.execute("SELECT 1 FROM meta WHERE key='cursor'").fetchone() is None:
            self.db.execute("INSERT INTO meta VALUES('cursor',?)", (str(initial_cursor),))
            self.db.commit()

    def cursor(self) -> int:
        return int(self.db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])

    def advance(self, value: int):
        self.db.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(int(value)),))
        self.db.commit()

    def status(self, key: str):
        row = self.db.execute("SELECT status FROM receipts WHERE wake_key=?", (key,)).fetchone()
        return row[0] if row else None

    def record(self, key, event, url, msg_hash, status, error=None):
        row = self.db.execute("SELECT attempts FROM receipts WHERE wake_key=?", (key,)).fetchone()
        attempts = (row[0] if row else 0) + (1 if status == "sending" else 0)
        self.db.execute(
            """INSERT INTO receipts(
              wake_key,sequence,event_id,task_id,recipient,status,
              message_sha256,conversation_url,attempts,last_error,delivered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(wake_key) DO UPDATE SET
              status=excluded.status,
              attempts=excluded.attempts,
              last_error=excluded.last_error,
              delivered_at=excluded.delivered_at,
              message_sha256=excluded.message_sha256""",
            (
                key,
                event["sequence"],
                event["event_id"],
                event["task_id"],
                event["recipient"],
                status,
                msg_hash,
                url,
                attempts,
                error,
                time.time() if status == "delivered" else None,
            ),
        )
        self.db.commit()


def remote_value(item):
    if not isinstance(item, dict):
        return item
    typ = item.get("type")
    if typ in ("string", "boolean", "number"):
        return item.get("value")
    if typ == "null":
        return None
    if typ == "array":
        return [remote_value(x) for x in item.get("value", [])]
    if typ == "object":
        return {k: remote_value(v) for k, v in item.get("value", [])}
    return item


class BiDiBrowser:
    def __init__(self, config):
        self.endpoint = config.get("bidi_endpoint", "ws://127.0.0.1:9229/session")
        self.browser_service = config.get(
            "browser_service",
            "aster-chat-wake-bidi-firefox.service",
        )

    def _restart_browser(self):
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR="/run/user/1000",
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
        )
        p = subprocess.run(
            ["/usr/bin/systemctl", "--user", "restart", self.browser_service],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if p.returncode:
            raise DeliveryError("pre_submit", "dedicated BiDi Firefox restart failed")
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                import socket
                with socket.create_connection(("127.0.0.1", 9229), timeout=1):
                    return
            except OSError:
                time.sleep(0.5)
        raise DeliveryError("pre_submit", "dedicated BiDi Firefox did not become ready")

    async def _call(self, ws, ident, method, params, timeout=30):
        await ws.send(json.dumps({"id": ident, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") == ident:
                return msg

    async def _open_session(self):
        try:
            ws = await connect(
                self.endpoint,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                max_size=4_000_000,
            )
        except Exception:
            await asyncio.to_thread(self._restart_browser)
            ws = await connect(
                self.endpoint,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                max_size=4_000_000,
            )

        status = await self._call(ws, 1, "session.status", {})
        if not status.get("result", {}).get("ready"):
            await ws.close()
            await asyncio.to_thread(self._restart_browser)
            ws = await connect(
                self.endpoint,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                max_size=4_000_000,
            )

        created = await self._call(
            ws,
            2,
            "session.new",
            {"capabilities": {"alwaysMatch": {"browserName": "firefox"}}},
        )
        if created.get("type") != "success":
            await ws.close()
            raise DeliveryError("pre_submit", "could not create Firefox BiDi session")
        return ws

    @staticmethod
    def _message(route, event):
        wake_id = event["event_id"]
        task_id = event["task_id"]
        if event["event_type"] == "task.queued":
            return (
                f"ASTER_WAKE_ID={wake_id}\n"
                f"Aster Coordinator queued task {task_id} for this regular Chat worker. "
                "Stay in this regular ChatGPT conversation and do not switch to Work. "
                "Use your own Aster Worker Chat Pro 01 tools to read, claim, and complete "
                "only that task. Do not create polling and do not accept your own result."
            )
        return (
            f"ASTER_WAKE_ID={wake_id}\n"
            f"Aster Coordinator completed task {task_id} for the main conversation. "
            "Stay in this regular ChatGPT conversation and do not switch to Work. "
            "Use Aster Coordinator Amsterdam to retrieve the exact task and new outcomes, "
            "review and surface the useful result here, then continue only the next "
            "already-authorized step. Do not repeat completed work."
        )

    async def send(self, route, event):
        ws = await self._open_session()
        session_open = True
        submitted = False
        try:
            tree = await self._call(ws, 3, "browsingContext.getTree", {})
            contexts = tree.get("result", {}).get("contexts", [])
            if not contexts:
                raise DeliveryError("pre_submit", "Firefox has no top-level browsing context")
            ctx = contexts[0]["context"]
            nav = await self._call(
                ws,
                4,
                "browsingContext.navigate",
                {
                    "context": ctx,
                    "url": route["conversation_url"],
                    "wait": "interactive",
                },
                45,
            )
            if nav.get("type") != "success":
                raise DeliveryError("pre_submit", "conversation navigation failed")
            await asyncio.sleep(5)

            wake_id = event["event_id"]
            cid = route["conversation_id"]
            wake_js = json.dumps(wake_id)
            cid_js = json.dumps(cid)
            check_expr = f"""(()=>{{
              const es=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"]')];
              const e=es.find(x=>!!(x.offsetWidth||x.offsetHeight||x.getClientRects().length));
              return {{
                url:location.href,
                title:document.title,
                composer:!!e,
                wakeAlready:document.body.innerText.includes({wake_js}) &&
                  ![...document.querySelectorAll('[contenteditable="true"]')]
                    .some(x=>(x.innerText||x.textContent||"").includes({wake_js})),
                regular:location.href.includes({cid_js}) &&
                        !location.pathname.includes("/work") &&
                        ![...document.querySelectorAll('button,[role="button"]')].some(x=>{{
                          const text=(x.innerText||x.textContent||"").trim();
                          const selected=x.getAttribute("aria-selected")==="true" ||
                                         x.getAttribute("aria-pressed")==="true" ||
                                         x.getAttribute("data-state")==="active";
                          return text==="Work" && selected;
                        }})
              }};
            }})()"""
            check = await self._call(
                ws,
                5,
                "script.evaluate",
                {
                    "expression": check_expr,
                    "target": {"context": ctx},
                    "awaitPromise": False,
                    "serializationOptions": {"maxObjectDepth": 3},
                },
            )
            result = remote_value(check["result"]["result"])
            if wake_id not in result.get("url", "") and cid not in result.get("url", ""):
                raise DeliveryError("pre_submit", "conversation identity mismatch")
            if not result.get("composer"):
                raise DeliveryError("pre_submit", "visible ChatGPT composer unavailable")
            if not result.get("regular"):
                raise DeliveryError("pre_submit", "regular Chat surface was not verified")
            if result.get("wakeAlready"):
                return {"message": self._message(route, event), "status": "already_present"}

            message = self._message(route, event)
            msg_js = json.dumps(message)
            insert_expr = f"""(()=>{{
              const es=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"]')];
              const e=es.find(x=>!!(x.offsetWidth||x.offsetHeight||x.getClientRects().length));
              if(!e) return {{ok:false,why:"composer"}};
              e.focus();
              document.execCommand("selectAll",false,null);
              document.execCommand("delete",false,null);
              const inserted=document.execCommand("insertText",false,{msg_js});
              e.dispatchEvent(new InputEvent("input",{{
                bubbles:true,inputType:"insertText",data:{msg_js}
              }}));
              const b=[...document.querySelectorAll('button[aria-label="Send"]')]
                .find(x=>!!(x.offsetWidth||x.offsetHeight||x.getClientRects().length)&&!x.disabled);
              return {{
                ok:inserted,
                text:(e.innerText||e.textContent||"").trim(),
                send:!!b
              }};
            }})()"""
            inserted = await self._call(
                ws,
                6,
                "script.evaluate",
                {
                    "expression": insert_expr,
                    "target": {"context": ctx},
                    "awaitPromise": False,
                    "serializationOptions": {"maxObjectDepth": 3},
                },
            )
            insert_result = remote_value(inserted["result"]["result"])
            if not insert_result.get("ok") or insert_result.get("text") != message:
                raise DeliveryError("pre_submit", "wake draft readback failed")

            click_expr = """(()=>{
              const b=[...document.querySelectorAll('button[aria-label="Send"]')]
                .find(x=>!!(x.offsetWidth||x.offsetHeight||x.getClientRects().length)&&!x.disabled);
              if(!b) return false;
              b.click();
              return true;
            })()"""
            clicked_ok = False
            for attempt in range(12):
                clicked = await self._call(
                    ws,
                    7 + attempt,
                    "script.evaluate",
                    {
                        "expression": click_expr,
                        "target": {"context": ctx},
                        "awaitPromise": False,
                        "userActivation": True,
                    },
                )
                if remote_value(clicked["result"]["result"]) is True:
                    clicked_ok = True
                    break
                await asyncio.sleep(0.25)
            if not clicked_ok:
                raise DeliveryError("pre_submit", "ChatGPT send button did not become available")
            submitted = True
            await asyncio.sleep(3)

            post_expr = f"""(()=>{{
              const es=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"]')];
              const e=es.find(x=>!!(x.offsetWidth||x.offsetHeight||x.getClientRects().length));
              return {{
                wakeVisible:document.body.innerText.includes({wake_js}) &&
                  ![...document.querySelectorAll('[contenteditable="true"]')]
                    .some(x=>(x.innerText||x.textContent||"").includes({wake_js})),
                composerText:e?(e.innerText||e.textContent||"").trim():null,
                url:location.href
              }};
            }})()"""
            post = await self._call(
                ws,
                30,
                "script.evaluate",
                {
                    "expression": post_expr,
                    "target": {"context": ctx},
                    "awaitPromise": False,
                    "serializationOptions": {"maxObjectDepth": 3},
                },
            )
            post_result = remote_value(post["result"]["result"])
            if not post_result.get("wakeVisible"):
                raise DeliveryError("post_submit_uncertain", "wake marker not visible after send")
            if post_result.get("composerText") not in ("", None):
                raise DeliveryError("post_submit_uncertain", "composer did not clear after send")
            return {"message": message, "status": "submitted"}
        except DeliveryError:
            raise
        except Exception as exc:
            stage = "post_submit_uncertain" if submitted else "pre_submit"
            raise DeliveryError(stage, f"{type(exc).__name__}: {str(exc)[:400]}") from exc
        finally:
            if session_open:
                try:
                    await self._call(ws, 99, "session.end", {}, 10)
                except Exception:
                    pass
            try:
                await ws.close()
            except Exception:
                pass


def trigger_for(event, routes):
    recipient = event.get("recipient")
    if recipient not in routes:
        return None
    typ = event.get("event_type")
    if typ == "task.queued" and routes[recipient].get("role") == "worker":
        return f"queued:{recipient}:{event['task_id']}"
    if typ == "task.completed" and routes[recipient].get("role") == "main":
        payload = event.get("payload") or {}
        # Coordinator uses the task.completed event type for terminal states
        # including cancellation/expiry. Only result-bearing terminal states
        # should wake the main Chat result surface.
        if payload.get("status") not in ("completed", "blocked"):
            return None
        return f"completed:{recipient}:{event['task_id']}"
    return None


async def run():
    cfg = json.loads(CONFIG.read_text())
    secret = Path(cfg["secret_file"]).read_text().strip()
    if len(secret) < 32:
        raise RuntimeError("relay secret is invalid")
    state = State(STATE, int(cfg["initial_cursor"]))
    browser = BiDiBrowser(cfg)

    while True:
        cursor = state.cursor()
        try:
            async with connect(
                f"{cfg['stream_url']}?after={cursor}",
                additional_headers={"Authorization": "Bearer " + secret},
                open_timeout=20,
                ping_interval=20,
                ping_timeout=20,
                max_size=262144,
            ) as ws:
                log("connected", cursor=cursor)
                async for raw in ws:
                    item = json.loads(raw)
                    if item.get("type") != "event":
                        continue
                    event = item["event"]
                    seq = int(event["sequence"])
                    key = trigger_for(event, cfg["routes"])
                    if key is None:
                        state.advance(seq)
                        continue

                    prior = state.status(key)
                    if prior in ("delivered", "uncertain"):
                        state.advance(seq)
                        continue

                    route = cfg["routes"][event["recipient"]]
                    placeholder_hash = hashlib.sha256(
                        (event["event_id"] + event["task_id"]).encode()
                    ).hexdigest()
                    state.record(
                        key,
                        event,
                        route["conversation_url"],
                        placeholder_hash,
                        "sending",
                    )
                    try:
                        result = await browser.send(route, event)
                        msg_hash = hashlib.sha256(result["message"].encode()).hexdigest()
                        state.record(
                            key,
                            event,
                            route["conversation_url"],
                            msg_hash,
                            "delivered",
                        )
                        state.advance(seq)
                        log(
                            "delivered",
                            key=key,
                            sequence=seq,
                            delivery_status=result["status"],
                        )
                    except DeliveryError as exc:
                        if exc.stage == "post_submit_uncertain":
                            state.record(
                                key,
                                event,
                                route["conversation_url"],
                                placeholder_hash,
                                "uncertain",
                                str(exc),
                            )
                            state.advance(seq)
                            log("uncertain", key=key, sequence=seq, error=str(exc))
                        else:
                            state.record(
                                key,
                                event,
                                route["conversation_url"],
                                placeholder_hash,
                                "failed",
                                str(exc),
                            )
                            log("delivery_failed", key=key, sequence=seq, error=str(exc))
                            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(
                "stream_reconnect",
                cursor=state.cursor(),
                error=f"{type(exc).__name__}: {str(exc)[:400]}",
            )
            await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(run())
