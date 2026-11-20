"""
What FlaVR Bot still needs from the go-live watcher, which moved to FlaVR Promo on 2026-11-20
(repo sensokasucks/flavr-promo, its own stack on port 3871).

``PromoLink`` reads FlaVR Promo's ``GET /live.json`` every EVERY seconds (PROMO_URL, e.g.
http://127.0.0.1:3871; empty = never asks). The answer (flavrpromo/server.py; keep the two in step):

  {"owner_live": true | false | null,
   "live": [{"name", "platforms", "since", "game", "owner"}],
   "owner_streams": [{"started_at", "ended_at", "url", "vod_url"}]}    newest first, last 24 h

Three things use it:
  * ``owner_live()`` → flavrbot/core_link.py: Stream Core going quiet while Promo sees the owner
    offline everywhere is the end of a stream, not a crash, so no "Core stopped answering" alarm.
  * ``on_owner(live, game)`` → live mode (flavrbot/community.py): called when the owner goes live,
    ends, or changes game as Promo sees it, so slowmode / the voice channel name follow the stream
    even while Stream Core is off. Core's own stream messages still drive it too (harmless twice).
  * ``owner_streams`` → the after-stream recap's VOD link (flavrbot/recap.py ``vod``).

When Promo stops answering, ``owner_live()`` goes back to None (unknown) after STALE seconds, so
the Core alarm works as it did before there was a watcher. Who calls it: flavrbot/__main__.py makes
one, wires it as above and runs ``run()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

log = logging.getLogger("flavrbot.promo_link")

EVERY = 60.0            # seconds between reads of /live.json
STALE = 180.0           # an answer older than this (s) no longer counts: owner_live() → None


class PromoLink:
    def __init__(self, http: Optional[aiohttp.ClientSession], url: str,
                 clock: Callable[[], float] = time.time):
        self.http = http
        self.url = (url or "").rstrip("/")
        self.clock = clock
        # live mode hook: (owner live?, the owner's game or ""), set by flavrbot/__main__.py
        self.on_owner: Optional[Callable[[bool, str], Awaitable[None]]] = None
        self.answer: Dict[str, Any] = {}        # Promo's last /live.json
        self.answer_at = 0.0                    # when it came (unix s; 0 = never)
        self.error = ""                         # the last read's problem, for /status ("" = fine)
        self._last: Optional[tuple] = None      # (live, game) last passed to on_owner

    @property
    def ready(self) -> bool:
        return bool(self.url and self.http is not None)

    # ── reading ────────────────────────────────────────────────────────
    def fresh(self) -> bool:
        return bool(self.answer_at and self.clock() - self.answer_at < STALE)

    def owner_live(self) -> Optional[bool]:
        """True live somewhere, False offline everywhere, None unknown (flavrbot/core_link.py)."""
        if not self.fresh():
            return None
        v = self.answer.get("owner_live")
        return v if isinstance(v, bool) else None

    def owner_game(self) -> str:
        row = next((r for r in self.answer.get("live") or [] if isinstance(r, dict) and r.get("owner")), None)
        return str((row or {}).get("game") or "")

    @property
    def owner_streams(self) -> List[Dict[str, Any]]:
        """The owner's streams of the last day, newest first (the recap's VOD link). Kept even when
        the answer is stale: a finished stream's VOD doesn't change."""
        return [s for s in self.answer.get("owner_streams") or [] if isinstance(s, dict)]

    # ── the loop ───────────────────────────────────────────────────────
    async def poll(self) -> bool:
        """Read /live.json once; True when Promo answered."""
        if not self.ready:
            return False
        try:
            async with self.http.get(f"{self.url}/live.json", timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
            if not isinstance(data, dict):
                raise RuntimeError("not a JSON object")
        except Exception as e:                  # Promo restarting or the address wrong: try again later
            if self.error != str(e):
                log.info("FlaVR Promo (%s) didn't answer: %s", self.url, e)
            self.error = str(e) or type(e).__name__
            return False
        self.answer, self.answer_at, self.error = data, self.clock(), ""
        await self._tell()
        return True

    async def _tell(self) -> None:
        """on_owner when the owner's live state or game changed since the last answer. The first
        answer after a start sets the baseline without calling (live mode remembers its own state)."""
        live = self.owner_live()
        if live is None:
            return
        now = (live, self.owner_game() if live else "")
        before, self._last = self._last, now
        if before is None or before == now or not self.on_owner:
            return
        try:
            await self.on_owner(*now)
        except Exception:
            log.exception("live mode from FlaVR Promo failed")

    async def run(self) -> None:
        if not self.ready:
            log.info("PROMO_URL not set: the Core alarm and live mode go by Stream Core alone")
            return
        while True:
            await self.poll()
            await asyncio.sleep(EVERY)
