"""
Phone pings: Discord DMs from the bot to the streamer (OWNER_ID). The Discord app on the phone
turns each DM into a notification, so no other app is needed.

Kinds (each can be switched with /pings; the defaults are in KINDS):
  stream   your stream started / ended (from Core, core/stream_state.py)
  drop     every platform went offline mid-stream, and when it's back (URGENT)
  core     Stream Core stopped answering during a stream, and when it's back (URGENT)
  raid     a raid came in
  money    a big tip / Super Chat (BIG_TIP or more) or a big sub gift (BIG_GIFT subs or more)
  friends  someone the go-live watcher follows went live (sent as one list every 10 minutes)
  calendar the weekly calendar changed: new schedules, go-lives, ends, new week / recap posted
           (collected for 10 minutes, sent with the picture; flavrbot/calendar_post.py)

Quiet hours (QUIET_HOURS, default 3-11 in TZ): ordinary pings wait and arrive as one message
when quiet hours end. URGENT pings always go at once. While Core says you're live, quiet hours
don't apply (you're clearly awake).

Who calls it: flavrbot/__main__.py hooks ``on_stream`` / ``on_alert`` / ``core_lost`` /
``core_back`` to flavrbot/core_link.py, ``friend_live`` to flavrbot/watcher.py, and runs
``run()`` (flushes held / batched pings every minute). ``send`` is the bot's "DM the owner".
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .store import Store

log = logging.getLogger("flavrbot.pings")

# kind -> (what /pings shows, on by default)
KINDS: Dict[str, Tuple[str, bool]] = {
    "stream": ("Your stream started / ended", True),
    "drop": ("Stream dropped mid-stream / back (urgent)", True),
    "core": ("Stream Core stopped answering during a stream (urgent)", True),
    "raid": ("Raids", True),
    "money": ("Big tips, Super Chats and sub gifts", True),
    "friends": ("Friends going live (every 10 minutes, in one message)", False),
    "calendar": ("Calendar changes, with the new picture (every 10 minutes at most)", True),
}
FRIENDS_EVERY = 600.0
# Core's alert kinds (core/alerts.py KINDS in flavr-leftovers): real money, and sub gifts with a qty
MONEY_KINDS = ("superchat", "supersticker", "donation", "charity")
GIFT_KINDS = ("gift", "member_gift")
# Platform coins worth about 1 cent each (bits, Twitch power-ups, Kick KICKs): BIG_TIP x 100 of them
CENT_CURRENCIES = ("bits", "kicks")

Send = Callable[[str], Awaitable[None]]


def length(sec: Any) -> str:
    m = int(sec or 0) // 60
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def money_text(a: Dict[str, Any], big_tip: float, big_gift: int) -> Optional[str]:
    """The ping text for a big tip / gift, or None when it's below the line (or not money)."""
    kind = str(a.get("kind") or "")
    who = a.get("display_name") or a.get("username") or "Someone"
    plat = str(a.get("platform") or "").title()
    try:
        amount = float(a.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    cur = str(a.get("currency") or "")
    if kind in GIFT_KINDS:
        qty = int(a.get("qty") or 0)
        return f"🎁 {who} gifted {qty} subs on {plat}" if qty >= big_gift else None
    if cur.lower() in CENT_CURRENCIES:
        return f"💎 {who} sent {int(amount)} {cur} on {plat}" if amount >= big_tip * 100 else None
    if kind in MONEY_KINDS and amount >= big_tip:
        return f"💸 {who} sent {amount:g} {cur} on {plat}" + (f": “{a['message']}”" if a.get("message") else "")
    return None


class Pinger:
    def __init__(self, store: Store, send: Send, tz: str = "America/New_York",
                 quiet: Tuple[int, int] = (3, 11), big_tip: float = 20.0, big_gift: int = 10,
                 live: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.time):
        self.store = store
        self.send = send
        try:
            self.tz = ZoneInfo(tz)
        except Exception:
            log.warning("Unknown TZ %r; using America/New_York", tz)
            self.tz = ZoneInfo("America/New_York")
        self.quiet = quiet
        self.big_tip = big_tip
        self.big_gift = big_gift
        self.live = live
        self.clock = clock
        self._friends: List[str] = []
        self._friends_at = 0.0

    # ── switches ───────────────────────────────────────────────────────
    def switches(self) -> Dict[str, bool]:
        saved = self.store.get("pings", {}) or {}
        return {k: bool(saved.get(k, d)) for k, (_, d) in KINDS.items()}

    def set(self, kind: str, on: bool) -> None:
        saved = self.store.get("pings", {}) or {}
        saved[kind] = bool(on)
        self.store.put("pings", saved)

    def set_quiet(self, start: int, end: int) -> None:
        self.quiet = (start % 24, end % 24)
        self.store.put("quiet_hours", list(self.quiet))

    def load_quiet(self) -> None:
        q = self.store.get("quiet_hours")
        if isinstance(q, list) and len(q) == 2:
            self.quiet = (int(q[0]) % 24, int(q[1]) % 24)

    def quiet_now(self) -> bool:
        start, end = self.quiet
        if start == end or self.live():
            return False
        hour = datetime.fromtimestamp(self.clock(), self.tz).hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    # ── sending ────────────────────────────────────────────────────────
    async def ping(self, kind: str, text: str, urgent: bool = False) -> None:
        if not self.switches().get(kind, False):
            return
        if not urgent and self.quiet_now():
            held = self.store.get("held_pings", []) or []
            held.append(f"{datetime.fromtimestamp(self.clock(), self.tz):%H:%M} {text}")
            self.store.put("held_pings", held[-50:])
            return
        await self._send(("🚨 " if urgent else "") + text)

    async def _send(self, text: str) -> None:
        try:
            await self.send(text[:1900])
        except Exception:
            log.exception("could not DM the owner")

    async def flush(self) -> None:
        """Send what quiet hours held back, and the friends list every FRIENDS_EVERY seconds."""
        if self.quiet_now():
            return
        held = self.store.get("held_pings", []) or []
        if held:
            self.store.put("held_pings", [])
            await self._send("While you were away:\n" + "\n".join(held))
        if self._friends and self.clock() - self._friends_at >= FRIENDS_EVERY:
            names, self._friends = self._friends, []
            self._friends_at = self.clock()
            await self._send("🟣 Live now: " + ", ".join(names))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.flush()
            except Exception:
                log.exception("ping flush failed")

    # ── events ─────────────────────────────────────────────────────────
    async def on_stream(self, change: Dict[str, Any]) -> None:
        ev = change.get("event")
        names = ", ".join(str(p).title() for p in change.get("platforms") or [])
        if ev == "started":
            await self.ping("stream", f"🔴 Your stream started ({names}).")
        elif ev == "ended":
            await self.ping("stream", f"⏹️ Your stream ended after {length(change.get('length_sec'))}.")
        elif ev == "dropped":
            await self.ping("drop", "Every platform went offline mid-stream. The stream counts as ended if "
                                    "nothing is back soon.", urgent=True)
        elif ev == "resumed":
            await self.ping("drop", f"✅ Back live ({names}).", urgent=True)

    async def on_alert(self, a: Dict[str, Any]) -> None:
        kind = str(a.get("kind") or "")
        who = a.get("display_name") or a.get("username") or "Someone"
        if kind == "raid":
            n = a.get("viewers") or a.get("qty") or 0
            await self.ping("raid", f"🚀 {who} raided with {n} viewers ({str(a.get('platform') or '').title()}).")
            return
        text = money_text(a, self.big_tip, self.big_gift)
        if text:
            await self.ping("money", text)

    async def core_lost(self, info: Dict[str, Any]) -> None:
        await self.ping("core", f"Stream Core stopped answering {info.get('silent_sec', 0)} s ago while you were "
                                "live. The stream PC may have crashed or lost its network.", urgent=True)

    async def core_back(self, stream: Dict[str, Any]) -> None:
        await self.ping("core", "✅ Stream Core is answering again.", urgent=True)

    def friend_live(self, name: str, where: str) -> None:
        """The watcher saw someone start; collected and sent as one list (flush)."""
        if self.switches().get("friends"):
            self._friends.append(f"{name} ({where})")
