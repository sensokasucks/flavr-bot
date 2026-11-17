"""
Go-live watcher: who of the people you follow (your FlaVR group, your other friends, yourself) is
live right now, on which platforms, and posts about it.

Each platform is checked on its own timer (TWITCH_EVERY_SEC 120, YOUTUBE_EVERY_SEC 600,
KICK_EVERY_SEC 180) through flavrbot/platforms/. After every check ``reconcile()`` merges the
answers per PERSON, not per channel:

  * live on several platforms at once (multistreaming) = one session with several platforms;
  * offline on every platform for less than RESTART_GRACE (15 min) and back = the same session
    ("1 restart"), so a crashed OBS doesn't post twice;
  * the session ends RESTART_GRACE after the last time any platform said live, and its end time
    is that last sighting (not when the watcher noticed).

The main platform (the link and picture a post shows) is the person's own pick, else the first
live one of YouTube, Twitch, Kick.

What happens on a change is the bot's business (flavrbot/bot.py ``LivePoster``): ``on_start``
posts in the person's group channel and returns where, ``on_change`` edits it when the
platforms or title change, ``on_end`` edits it to "was live" with the length and VOD. Sessions
are kept in the database (flavrbot/store.py), which later rounds use for the weekly calendar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .store import PLATFORMS, Store

log = logging.getLogger("flavrbot.watcher")
RESTART_GRACE = 15 * 60.0

# on_start(person, session, infos) -> (channel id, message id) or ("", "")
OnStart = Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]]], Awaitable[Tuple[str, str]]]
OnChange = Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]]], Awaitable[None]]
OnEnd = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[None]]


def main_platform(person: Dict[str, Any], live_on: Dict[str, Any]) -> str:
    """The person's chosen main platform if it is live, else YouTube, Twitch, Kick in that order."""
    pick = person.get("main_platform") or ""
    if pick in live_on:
        return pick
    return next((p for p in PLATFORMS if p in live_on), "")


class Watcher:
    def __init__(self, store: Store, checkers: Dict[str, Any], every: Dict[str, int],
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.checkers = {k: c for k, c in checkers.items() if c is not None}
        self.every = every
        self.clock = clock
        # platform → {platform_id: Live}: the newest answer of each platform
        self.seen: Dict[str, Dict[str, Dict[str, Any]]] = {p: {} for p in PLATFORMS}
        self.last_check: Dict[str, float] = {}
        self.errors: Dict[str, str] = {}
        self.on_start: Optional[OnStart] = None
        self.on_change: Optional[OnChange] = None
        self.on_end: Optional[OnEnd] = None
        self._lock = asyncio.Lock()
        # people whose open session saw no platform live at the last merge (a possible restart)
        self._gone: set[int] = set()

    # ── checking ───────────────────────────────────────────────────────
    async def resolve_missing(self, platform: str) -> None:
        """Look up platform ids for accounts added without one (a new /friend add, or a failed look-up)."""
        checker = self.checkers.get(platform)
        todo = [a for a in self.store.accounts(platform) if not a["platform_id"]]
        if not checker or not todo:
            return
        found = await checker.resolve([a["handle"] for a in todo])
        for a in todo:
            info = found.get(a["handle"])
            if info and info.get("platform_id"):
                self.store.set_account_info(a["id"], info["platform_id"], info.get("display", ""), info.get("avatar", ""))

    async def check(self, platform: str) -> None:
        """Ask one platform who is live, then merge (reconcile)."""
        checker = self.checkers.get(platform)
        if not checker or not getattr(checker, "ready", True):
            return
        try:
            await self.resolve_missing(platform)
            accounts = [a for a in self.store.accounts(platform) if a["platform_id"]]
            self.seen[platform] = await checker.live(accounts) if accounts else {}
            self.last_check[platform] = self.clock()
            self.errors.pop(platform, None)
        except Exception as e:      # keep the last answer; a platform hiccup is not "everyone went offline"
            self.errors[platform] = str(e) or type(e).__name__
            log.warning("%s check failed: %s", platform.title(), self.errors[platform])
            return
        await self.reconcile()

    async def run_platform(self, platform: str) -> None:
        while True:
            await self.check(platform)
            await asyncio.sleep(self.every.get(platform, 300))

    # ── merging ────────────────────────────────────────────────────────
    def live_on(self, person: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        out = {}
        for a in person.get("accounts") or []:
            info = self.seen.get(a["platform"], {}).get(a["platform_id"]) if a["platform_id"] else None
            if info:
                out[a["platform"]] = {**info, "display": a.get("display") or a["handle"], "avatar": a.get("avatar") or ""}
        return out

    async def reconcile(self) -> None:
        async with self._lock:
            now = self.clock()
            for person in self.store.people():
                try:
                    await self._one(person, now)
                except Exception:
                    log.exception("watcher update for %s failed", person.get("name"))

    async def _one(self, person: Dict[str, Any], now: float) -> None:
        live_on = self.live_on(person)
        open_s = self.store.open_session(person["id"])
        if live_on:
            main = main_platform(person, live_on)
            info = live_on[main]
            fields = {"last_seen": now, "platforms": sorted(live_on, key=PLATFORMS.index), "title": info.get("title", ""),
                      "url": info.get("url", ""), "thumb": info.get("thumb", ""), "game": info.get("game", ""),
                      "viewers": sum(int(i.get("viewers") or 0) for i in live_on.values())}
            if open_s:
                changed = (json.loads(open_s["platforms"]) != fields["platforms"] or open_s["title"] != fields["title"])
                if person["id"] in self._gone:
                    # it was offline everywhere and came back inside RESTART_GRACE: same stream, one restart
                    self._gone.discard(person["id"])
                    fields["restarts"] = int(open_s["restarts"]) + 1
                    changed = True
                self.store.update_session(open_s["id"], **fields)
                if changed and self.on_change:
                    await self.on_change(person, {**open_s, **fields}, live_on)
                return
            started = min([float(i["started_at"]) for i in live_on.values() if i.get("started_at")] or [now])
            sid = self.store.start_session(person["id"], started, **{k: v for k, v in fields.items() if k != "last_seen"})
            self.store.update_session(sid, last_seen=now)
            session = {**fields, "id": sid, "started_at": started, "restarts": 0}
            if self.on_start:
                channel, message = await self.on_start(person, session, live_on)
                if message:
                    self.store.update_session(sid, post_channel=str(channel), post_message=str(message))
        elif open_s and now - float(open_s["last_seen"]) < RESTART_GRACE:
            self._gone.add(person["id"])        # offline for now; a comeback counts as a restart
        elif open_s:
            self._gone.discard(person["id"])
            self.store.update_session(open_s["id"], ended_at=float(open_s["last_seen"]))
            if self.on_end:
                await self.on_end(person, {**open_s, "ended_at": float(open_s["last_seen"])})

    # ── reading (/live, /schedule, /status) ────────────────────────────
    def status(self) -> Dict[str, Any]:
        return {"platforms": {p: {"ready": bool(getattr(c, "ready", True)), "last_check": self.last_check.get(p),
                                  "error": self.errors.get(p, "")} for p, c in self.checkers.items()},
                "people": len(self.store.people())}

    async def schedule(self, person: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Upcoming streams of one person: Twitch's schedule + YouTube's scheduled streams."""
        out: List[Dict[str, Any]] = []
        for a in person.get("accounts") or []:
            if not a["platform_id"]:
                continue
            if a["platform"] == "twitch" and "twitch" in self.checkers:
                try:
                    out += await self.checkers["twitch"].schedule(a["platform_id"])
                except Exception as e:
                    log.info("Twitch schedule failed: %s", e)
            if a["platform"] == "youtube" and "youtube" in self.checkers:
                out += list(getattr(self.checkers["youtube"], "upcoming", {}).get(a["platform_id"], []))
        now = self.clock()
        return sorted((s for s in out if s.get("at") and s["at"] > now - 3600), key=lambda s: s["at"])
