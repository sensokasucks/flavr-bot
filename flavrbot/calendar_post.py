"""
Keeps each group's weekly calendar post in Discord, plus the Saturday recap and the owner's
calendar pings. The data is flavrbot/calendar.py, the picture flavrbot/render.py.

The week's rhythm (times in TZ; CALENDAR_PREVIEW_HOUR / CALENDAR_RECAP_HOUR):
  Sunday 10 AM   a NEW post for the new week: the picture + a day-by-day list with links.
                 Before 10 AM on Sunday last week's post stays up (it gets its final edits).
  all week       that post is edited in place whenever the board changes (someone schedules,
                 goes live, ends), checked every minute and right after a go-live / end; while
                 someone is live it is also redrawn every LIVE_REDRAW for a fresh live preview.
  Saturday 11 PM a separate recap post: every stream the watcher saw this week with real start
                 and end times, length, VOD links and the LATE NOTICE tag (Discord only).
A group's posts only start once /setup calendar-channel picks its channel (posted at once, even
mid-week). If the post is deleted, the next change makes a new one.

Calendar pings (the "calendar" kind in /pings): what changed on the board ("Lulu scheduled Thu
7 PM", "Pippa went live", "new week posted"), collected for PING_BATCH and sent as one DM with
the newest FlaVR picture. During quiet hours they wait like other pings (text only).

Saved in the database (kv):
  calendar_channel.<group>  channel id (/setup calendar-channel)
  calendar_post.<group>     {week, channel, message, sig, drawn_at}: the live board post
  calendar_recap.<group>    the week (ISO date of its Sunday) whose recap was posted
  backfill.<week>           when that week (ISO date of its Sunday) was last filled in from the
                            platforms (``ensure_backfill``; the bot only knows what it saw live)

Filling in (``backfill``, set by flavrbot/__main__.py to Watcher.backfill): last week and this week
once at start-up, a past week the first time someone looks at it (/calendar view, the web page),
again once a week is over, and right before the Saturday recap. /calendar refresh does it by hand.

Who calls it: flavrbot/__main__.py builds one and runs ``run()``; the watcher's start / end /
scheduled callbacks call ``poke()``; flavrbot/bot.py's /calendar and /setup calendar-channel use
``view_png``, ``embed`` and ``refresh``. ``discord`` is the bot (FlavrBot): it must offer
``cal_send(channel, embed, png, name)`` → message id or "", ``cal_edit(channel, message, embed,
png, name)`` → True/False, ``dm_owner(text, png=None)`` and ``dm_share(messages, png)``.

Share (flavrbot/share.py): after the Sunday post and the Saturday recap, when the /pings switch
"share" is on, the owner gets copy-ready text for other Discords and X (held until quiet hours
end). /calendar share sends it any time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .calendar import DAY_NAMES, Tile, Week, build_week, changes, week_start
from .share import X_STYLES, discord_text, x_posts
from .render import ImageCache, Screenshotter, logo_data_url, page
from .store import GROUPS, Store

log = logging.getLogger("flavrbot.calendar_post")
LIVE_REDRAW = 20 * 60.0         # while someone is live, redraw this often (fresh live preview)
PING_BATCH = 10 * 60.0          # calendar pings: changes within this window arrive as one DM
TICK = 60.0                     # how often the loop looks (a poke wakes it early)
GROUP_NAMES = {"flavr": "FlaVR", "friends": "Friends"}
GROUP_COLOURS = {"flavr": 0xF72CA2, "friends": 0x2AE0D4}
DOTS = {"youtube": "▶️", "twitch": "🟣", "kick": "🟢"}
FIELD_MAX = 1024                # Discord: one embed field's text
EMBED_MAX = 5600                # Discord: 6000 for a whole embed, minus room for title / footer


def _safe(text: str, n: int = 60) -> str:
    """A title fit for a markdown link: no brackets / backticks, cut to ``n`` characters."""
    t = (text or "").replace("[", "(").replace("]", ")").replace("`", "'").replace("*", "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _hm(sec: float) -> str:
    m = int(sec) // 60
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def line(t: Tile, recap: bool) -> str:
    """One stream in the text list under the picture. <t:…:t> shows in each reader's own time zone."""
    dots = "".join(DOTS.get(p, "") for p in t.platforms)
    when = f"<t:{int(t.at)}:t>"
    title = _safe(t.title or t.game)
    if t.kind == "live":
        watch = f"[{title or 'watch'}]({t.url})" if t.url else title
        return f"🔴 {when} **{t.name}** {dots} live now · {watch}"
    if t.kind == "past":
        tail = f"[{title or 'VOD'}]({t.url})" if t.url else title
        late = " · ⏰ late notice" if recap and t.late else ""
        return f"✅ {when}–<t:{int(t.end or t.at)}:t> **{t.name}** {dots} {_hm(t.length)}{late}" + (f" · {tail}" if tail else "")
    tail = f"[{title or 'stream'}]({t.url})" if t.url else title
    return f"🗓️ {when} **{t.name}** {dots}" + (f" · {tail}" if tail else "")


def embed(week: Week) -> Dict[str, Any]:
    """The post's text as plain data (flavrbot/bot.py turns it into a discord.Embed):
    {title, colour, fields: [(name, value)], footer}. Days with nothing are left out; a day too
    long for one field ends with "+N more"."""
    name = GROUP_NAMES.get(week.group, week.group.title())
    title = (f"{name}: week in review · {week.range_text}" if week.recap
             else f"This week in {name} · {week.range_text}" if week.group == "flavr"
             else f"This week with {name.lower()} · {week.range_text}")
    fields: List[Tuple[str, str]] = []
    total = len(title)
    today = week.today_index()
    for i, tiles in enumerate(week.days):
        if not tiles:
            continue
        date = week.start + timedelta(days=i)
        head = f"{DAY_NAMES[i].title()} {date:%b} {date.day}" + (" · today" if i == today and not week.recap else "")
        lines: List[str] = []
        used = 0
        for n, t in enumerate(tiles):
            text = line(t, week.recap)
            if used + len(text) + 12 > FIELD_MAX or total + used + len(text) > EMBED_MAX:
                lines.append(f"+{len(tiles) - n} more")
                break
            lines.append(text)
            used += len(text) + 1
        value = "\n".join(lines)
        total += len(head) + len(value)
        fields.append((head, value))
    if week.recap:
        foot = f"{week.stream_count} streams · {week.hours_live:.0f} hours live"
    else:
        foot = "Times below are in your own time zone"
    if week.quiet:
        foot += (" · no streams: " if week.recap else " · nothing on yet: ") + ", ".join(week.quiet)
    return {"title": title, "colour": GROUP_COLOURS.get(week.group, 0x2AE0D4), "fields": fields,
            "footer": foot[:2000], "empty": not fields}


class CalendarPoster:
    def __init__(self, store: Store, discord: Any, images: ImageCache, shooter: Optional[Screenshotter],
                 tz: str = "America/New_York", preview_hour: int = 10, recap_hour: int = 23,
                 ping: Optional[Callable[[], bool]] = None, quiet: Optional[Callable[[], bool]] = None,
                 hold: Optional[Callable[[str], Any]] = None, clock: Callable[[], float] = time.time,
                 backfill: Optional[Callable[[float, float], Awaitable[int]]] = None,
                 share_on: Optional[Callable[[], bool]] = None):
        self.store = store
        self.discord = discord
        self.images = images
        self.shooter = shooter              # None = text-only posts (no Chromium)
        try:
            self.tz = ZoneInfo(tz)
        except Exception:
            self.tz = ZoneInfo("America/New_York")
        self.preview_hour = preview_hour    # CALENDAR_PREVIEW_HOUR (Sunday)
        self.recap_hour = recap_hour        # CALENDAR_RECAP_HOUR (Saturday)
        self.ping_on = ping or (lambda: False)      # Pinger switch "calendar"
        self.quiet = quiet or (lambda: False)       # Pinger.quiet_now
        self.hold = hold                            # Pinger.ping("calendar", text): held during quiet hours
        self.clock = clock
        self.backfill = backfill                    # Watcher.backfill(start, end) → sessions added
        self.share_on = share_on or (lambda: False)  # Pinger switch "share"
        self._shares: List[Tuple[Week, Optional[bytes]]] = []    # waiting for quiet hours to end
        self._poke = asyncio.Event()
        self._lock = asyncio.Lock()
        self._seen: Dict[str, Tuple[str, Dict[str, Tile]]] = {}    # group → (week, {key: tile}) last look
        self._pending: List[str] = []
        self._pending_at = 0.0
        self._last_png: Optional[bytes] = None
        self.errors: Dict[str, str] = {}

    # ── outside calls ──────────────────────────────────────────────────
    def poke(self, *_: Any) -> None:
        """Something changed (go-live, end, new schedule): look again within a few seconds."""
        self._poke.set()

    async def on_scheduled(self, person: Dict[str, Any], item: Dict[str, Any]) -> None:
        self.poke()

    def week_of(self, offset: int = 0, now: Optional[float] = None) -> datetime:
        """Sunday 00:00 of this week (0), last week (-1) or next week (1)."""
        start = week_start(self.clock() if now is None else now, self.tz)
        if offset:
            d = (start + timedelta(days=7 * offset)).date()
            start = datetime(d.year, d.month, d.day, tzinfo=self.tz)    # DST-safe
        return start

    def build(self, group: str, start: datetime, recap: bool = False) -> Week:
        return build_week(self.store, group, start, self.clock(), self.tz, recap=recap)

    async def png(self, week: Week) -> Optional[bytes]:
        """The picture, or None when Chromium isn't there / fails (posts go out as text then)."""
        if self.shooter is None:
            return None
        try:
            await self.images.fetch_all(week)
            return await self.shooter.png(page(week, self.images.data_url, logo=logo_data_url()))
        except Exception as e:
            self.errors["picture"] = str(e) or type(e).__name__
            log.warning("calendar picture failed: %s", self.errors["picture"])
            return None

    async def ensure_backfill(self, start: datetime, force: bool = False) -> int:
        """Fill the week starting ``start`` from the platforms' past streams, unless that was already
        done after the week ended (a week still running is done again once it's over). ``force``:
        always (start-up, the recap, /calendar refresh). Returns how many streams were added."""
        if self.backfill is None:
            return 0
        now = self.clock()
        if start.timestamp() > now:
            return 0                                # a future week has no past streams
        key = f"backfill.{start.date().isoformat()}"
        end = (start + timedelta(days=7)).timestamp()
        if not force and float(self.store.get(key) or 0) >= min(end, now - 6 * 3600):
            return 0
        try:
            added = await self.backfill(start.timestamp(), min(end, now))
        except Exception:
            log.exception("filling in past streams failed")
            return 0
        self.store.put(key, now)
        if added:
            self.poke()
        return added

    async def view_png(self, group: str, offset: int = 0) -> Tuple[Week, Optional[bytes]]:
        """/calendar: (the week, its picture). Last week is shown as a recap."""
        start = self.week_of(offset)
        if offset < 0:
            await self.ensure_backfill(start)
        week = self.build(group, start, recap=offset < 0)
        return week, await self.png(week)

    def share_messages(self, week: Week, style: str = "") -> List[str]:
        """The copy-ready texts: a short note, the Discord version, then each X post. ``style``: an X
        style (flavrbot/share.py X_STYLES); "" = the saved default (/setup share-style)."""
        style = style if style in X_STYLES else (self.store.get("share_x_style") or "reply")
        handles = {p["id"]: p.get("x_handle") or "" for p in self.store.people(week.group)}
        xs = x_posts(week, handles, style, bold=bool(self.store.get("share_x_bold")),
                     tags=str(self.store.get("share_x_tags") or ""))
        after = {"reply": "the X post, then its links to paste as a reply." if len(xs) > 1 else "the X post.",
                 "one": "the X post.", "thread": f"an X thread of {len(xs)} posts." if len(xs) > 1 else "the X post."}
        note = ("📋 Copy-ready calendar. Long-press (phone) or right-click (PC) a message → Copy Text. "
                "Next: the Discord version, then " + after.get(style, "the X post."))
        return [note, discord_text(week)] + xs

    async def share(self, group: str, offset: int = 0, style: str = "") -> bool:
        """/calendar share: DM the package for this (0), next (1) or last (-1, the recap) week now."""
        start = self.week_of(offset)
        if offset < 0:
            await self.ensure_backfill(start)
        week = self.build(group, start, recap=offset < 0)
        return await self._send_share(week, await self.png(week), style)

    async def _send_share(self, week: Week, png: Optional[bytes], style: str = "") -> bool:
        try:
            await self.discord.dm_share(self.share_messages(week, style), png)
            return True
        except Exception:
            log.exception("calendar share DM failed")
            return False

    def _share_later(self, week: Week, png: Optional[bytes]) -> None:
        """After an automatic post: queue the package if the switch is on (sent by flush_shares)."""
        if self.share_on():
            self._shares.append((week, png))

    async def flush_shares(self) -> None:
        if self._shares and not self.quiet():
            todo, self._shares = self._shares, []
            for week, png in todo:
                await self._send_share(week, png)

    # ── the loop ───────────────────────────────────────────────────────
    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("calendar update failed")
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=TICK)
                await asyncio.sleep(5)          # let a burst of changes settle into one edit
            except asyncio.TimeoutError:
                pass
            self._poke.clear()

    async def tick(self) -> None:
        async with self._lock:
            for g in GROUPS:
                if self.store.get(f"calendar_channel.{g}"):
                    await self.refresh(g)
            self.images.prune()
            await self.flush_pings()
            await self.flush_shares()

    async def refresh(self, group: str, force_new: bool = False) -> None:
        """Bring one group's posts up to date (see the module notes for the rhythm)."""
        channel = str(self.store.get(f"calendar_channel.{group}") or "")
        if not channel:
            return
        now = self.clock()
        local = datetime.fromtimestamp(now, self.tz)
        this_week = self.week_of(0, now)
        post = self.store.get(f"calendar_post.{group}") or {}
        wday = (local.weekday() + 1) % 7               # 0 = Sunday

        # Saturday night recap (once per week)
        if wday == 6 and local.hour >= self.recap_hour and self.store.get(f"calendar_recap.{group}") != this_week.date().isoformat():
            await self.ensure_backfill(this_week, force=True)     # streams missed while the bot was down
            week = self.build(group, this_week, recap=True)
            png = await self.png(week)
            if await self.discord.cal_send(channel, embed(week), png, f"recap-{this_week:%Y-%m-%d}.png"):
                self.store.put(f"calendar_recap.{group}", this_week.date().isoformat())
                self._note(f"{GROUP_NAMES.get(group, group)} week in review posted", png)
                self._share_later(week, png)

        # which week the live board shows: a new post for a new week from Sunday PREVIEW_HOUR on
        posted_week = post.get("week") or ""
        new_week_due = posted_week != this_week.date().isoformat() and (wday > 0 or local.hour >= self.preview_hour)
        if force_new or not post.get("message") or new_week_due:
            if post.get("message") and posted_week and posted_week != this_week.date().isoformat():
                await self._edit(group, post, final=True)       # last week's post: its final state
            week = self.build(group, this_week)
            png = await self.png(week)
            msg = await self.discord.cal_send(channel, embed(week), png, f"calendar-{this_week:%Y-%m-%d}.png")
            if msg:
                self.store.put(f"calendar_post.{group}", {"week": this_week.date().isoformat(), "channel": channel,
                                                          "message": str(msg), "sig": week.signature(), "drawn_at": now})
                self._seen[group] = (this_week.date().isoformat(), {t.key: t for t in week.tiles()})
                if new_week_due and posted_week:
                    self._note(f"New {GROUP_NAMES.get(group, group)} week posted", png)
                    self._share_later(week, png)
            return
        await self._edit(group, post)

    async def _edit(self, group: str, post: Dict[str, Any], final: bool = False) -> None:
        """Redraw the board post when what it shows changed (or a live preview is stale)."""
        start = datetime.fromisoformat(post["week"]).replace(tzinfo=self.tz)
        week = self.build(group, start)
        now = self.clock()
        sig = week.signature()
        live = any(t.kind == "live" for t in week.tiles())
        stale = live and now - float(post.get("drawn_at") or 0) >= LIVE_REDRAW
        self._diff(group, post["week"], week)
        if sig == post.get("sig") and not stale and not final:
            return
        png = await self.png(week)
        ok = await self.discord.cal_edit(post["channel"], post["message"], embed(week), png,
                                         f"calendar-{start:%Y-%m-%d}.png")
        if ok:
            self.store.put(f"calendar_post.{group}", {**post, "sig": sig, "drawn_at": now})
            if png and group == "flavr":
                self._last_png = png
        elif not final:
            # the post (or its channel) is gone: forget it, the next look makes a new one
            self.store.put(f"calendar_post.{group}", {})

    # ── pings ──────────────────────────────────────────────────────────
    def _diff(self, group: str, week_key: str, week: Week) -> None:
        now_tiles = {t.key: t for t in week.tiles()}
        before = self._seen.get(group)
        self._seen[group] = (week_key, now_tiles)
        if not before or before[0] != week_key:
            return                              # first look since start, or a new week: no diff
        prefix = "" if group == "flavr" else f"{GROUP_NAMES.get(group, group)}: "
        for text in changes(before[1], now_tiles, self.tz):
            self._note(prefix + text)

    def _note(self, text: str, png: Optional[bytes] = None) -> None:
        if not self.ping_on():
            return
        if not self._pending:
            self._pending_at = self.clock()
        self._pending.append(text)
        if png:
            self._last_png = png

    async def flush_pings(self) -> None:
        """Send the collected calendar changes once the oldest is PING_BATCH old."""
        if not self._pending or self.clock() - self._pending_at < PING_BATCH:
            return
        text = "🗓️ Calendar updated: " + "; ".join(self._pending)
        self._pending = []
        if self.quiet() and self.hold:
            await self.hold(text)           # held like other pings; sent when quiet hours end
            return
        try:
            await self.discord.dm_owner(text[:1900], self._last_png)
        except Exception:
            log.exception("calendar ping failed")
