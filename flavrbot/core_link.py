"""
The Stream Core side door: Core (on the stream PC) sends what it knows to this bot.

  POST /core/push   Authorization: Bearer <FLAVR_CORE_KEY>
                    {"type": "hello" | "heartbeat" | "stream" | "alert" | "snapshot", "data": {...}, "at": unix s}
  GET  /health      {"ok": true}  (no password; Docker's health check)
  GET  /calendar...  the weekly calendar page, picture and .ics feed (flavrbot/web.py, added
                     through ``extra_routes`` by flavrbot/__main__.py)

The sender is flavr-leftovers fridge-stream-core/core/companion_link.py; docs/CORE_LINK.md lists
every message and field. Keep the three in sync.

What this file does with them (``CoreLink``):
  * remembers the newest heartbeat and snapshot (also in the database, so /points and /queue
    answer after the bot restarts while the stream PC is off);
  * calls ``on_stream(change)`` for each stream start / drop / resume / end and
    ``on_alert(alert)`` for each alert (flavrbot/pings.py turns those into phone pings);
  * watches the heartbeat: no heartbeat for HEARTBEAT_LATE_SEC while Core last said "live" calls
    ``on_core_lost()``; the next heartbeat after that calls ``on_core_back()``.

Only listens on the home network (LISTEN_HOST / LISTEN_PORT, default 0.0.0.0:3870). Never forward
this port on the router: the password is the only lock.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from aiohttp import web

from .store import Store

log = logging.getLogger("flavrbot.core_link")

Callback = Callable[[Dict[str, Any]], Awaitable[None]]
MAX_BODY = 2 * 1024 * 1024      # a 500-row points snapshot is ~50 KB; this is plenty


class CoreLink:
    def __init__(self, store: Store, key: str, late_sec: int = 90,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.key = key or ""
        self.late_sec = late_sec
        self.clock = clock
        self.on_stream: Optional[Callback] = None
        self.on_alert: Optional[Callback] = None
        self.on_core_lost: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self.on_core_back: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self.lost = False                       # True after on_core_lost until a heartbeat comes
        self.heartbeat: Dict[str, Any] = store.get("core.heartbeat", {}) or {}
        self.heartbeat_at: Optional[float] = store.get("core.heartbeat_at")
        self.snapshot: Dict[str, Any] = store.get("core.snapshot", {}) or {}
        self.snapshot_at: Optional[float] = store.get("core.snapshot_at")
        self.refused = 0                        # pushes with a wrong password (shown on /status)
        # more pages on the same port: each is called with the aiohttp app (flavrbot/web.py add_routes)
        self.extra_routes: list[Callable[[web.Application], None]] = []
        self._runner: Optional[web.AppRunner] = None
        self._watch: Optional[asyncio.Task] = None

    # ── reading ────────────────────────────────────────────────────────
    def core_online(self) -> bool:
        return bool(self.heartbeat_at and self.clock() - self.heartbeat_at < self.late_sec)

    def stream(self) -> Dict[str, Any]:
        """Core's last word on the stream (core/stream_state.py status()), {} if never heard."""
        return dict(self.heartbeat.get("stream") or {})

    def stream_live(self) -> bool:
        return bool(self.stream().get("live"))

    # ── the web side ───────────────────────────────────────────────────
    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_post("/core/push", self._push)
        app.router.add_get("/health", self._health)
        for add in self.extra_routes:
            add(app)
        return app

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    def _authorized(self, request: web.Request) -> bool:
        got = request.headers.get("Authorization", "")
        want = f"Bearer {self.key}"
        return bool(self.key) and hmac.compare_digest(got.encode(), want.encode())

    async def _push(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            self.refused += 1
            log.warning("Core push refused: wrong password from %s", request.remote)
            return web.json_response({"ok": False, "error": "wrong password"}, status=401)
        try:
            msg = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "not JSON"}, status=400)
        if not isinstance(msg, dict):
            return web.json_response({"ok": False, "error": "not an object"}, status=400)
        try:
            await self.handle(str(msg.get("type") or ""), msg.get("data") or {})
        except Exception:
            log.exception("Core push %s failed", msg.get("type"))
            return web.json_response({"ok": False, "error": "bot error"}, status=500)
        return web.json_response({"ok": True})

    async def handle(self, kind: str, data: Dict[str, Any]) -> None:
        """One message from Core (also called directly by tests)."""
        now = self.clock()
        if kind in ("hello", "heartbeat"):
            if kind == "heartbeat" and isinstance(data, dict):
                self.heartbeat = data
                self.store.put("core.heartbeat", data)
            self.heartbeat_at = now
            self.store.put("core.heartbeat_at", now)
            if self.lost:
                self.lost = False
                if self.on_core_back:
                    await self.on_core_back(self.stream())
        elif kind == "snapshot" and isinstance(data, dict):
            # keep the old points / queue when one part is missing (that switch is off in Core)
            self.snapshot = {**self.snapshot, **data}
            self.snapshot_at = now
            self.store.put("core.snapshot", self.snapshot)
            self.store.put("core.snapshot_at", now)
        elif kind == "stream" and isinstance(data, dict):
            self.heartbeat = {**self.heartbeat, "stream": {**self.stream(), **data,
                                                           "state": "offline" if data.get("event") == "ended" else
                                                           "dropped" if data.get("event") == "dropped" else "live"}}
            self.store.put("core.heartbeat", self.heartbeat)
            if self.on_stream:
                await self.on_stream(data)
        elif kind == "alert" and isinstance(data, dict):
            if self.on_alert:
                await self.on_alert(data)
        else:
            log.info("Core sent an unknown message type %r (newer Core?); ignored", kind)

    # ── heartbeat watch ────────────────────────────────────────────────
    async def check_late(self) -> None:
        """Core went quiet during a stream → on_core_lost once (called every 15 s by watch())."""
        if self.lost or not self.heartbeat_at:
            return
        if self.clock() - self.heartbeat_at >= self.late_sec and self.stream().get("state") in ("live", "dropped"):
            self.lost = True
            if self.on_core_lost:
                await self.on_core_lost({"silent_sec": int(self.clock() - self.heartbeat_at), **self.stream()})

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.check_late()
            except Exception:
                log.exception("heartbeat check failed")

    async def start(self, host: str, port: int) -> None:
        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        self._watch = asyncio.create_task(self._watch_loop(), name="core-heartbeat-watch")
        log.info("Listening for Stream Core on %s:%s", host, port)

    async def stop(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self._runner:
            await self._runner.cleanup()
