"""
After-stream recap and chat moments (Sen asked 2026-11-18: "clips + stream recaps", official APIs only,
no transcripts).

Chat moments: Stream Core spots a sudden burst of chat during a stream (fridge-stream-core
core/chat_moments.py) and sends a "moment" (docs/CORE_LINK.md): when, how busy, the usual, and a
Twitch clip link when Core made one (moments.twitch_clips; Twitch's own Create Clip API). Kick and
YouTube have no clip API: their moments are links to that point in the VOD. ``on_moment`` saves it
and DMs the owner when /pings kind "chat_moments" is on (off by default).

The recap: RECAP_DELAY_MIN after Core says the stream ended (its "stream" ended event; the delay
lets the last chat lines arrive), one message with
  * how long, which platforms, the most viewers Core reported (heartbeats, tracked by ``watch``),
  * chat: lines and different chatters (the /said chat copy, flavrbot/said.py), top chatters,
  * support: raids (who, how many), subs, gifted subs, tips / Super Chats, bits and KICKs (the
    alerts Core sent, saved by ``on_alert``),
  * the moments, each with a link to that time in the VOD (the owner's stream as FlaVR Promo's
    go-live watcher saw it: its /live.json owner_streams, read by flavrbot/promo_link.py; Twitch ?t=1h2m3s, YouTube &t=123s) and the
    Twitch clip when there is one,
  * with OLLAMA_URL set: a few sentences from a local AI (Ollama) on what happened in chat.
It goes to the channel picked with /setup recap-channel (kv recap_channel) and to the owner's DMs
(/pings kind "recap", on by default). Nothing in it pings anyone (allowed_mentions none).

Saved in kv: recap_live {started_at, peak} while live, recap_pending {started_at, ended_at} until
the recap is out (so a bot restart in between still writes it). Who calls it: flavrbot/__main__.py
wires ``on_stream`` / ``on_alert`` / ``on_moment`` to flavrbot/core_link.py and runs ``run()``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .pings import length
from .store import Store

log = logging.getLogger("flavrbot.recap")

TICK = 30.0                     # how often ``run`` looks (peak viewers, a due recap)
TOP_CHATTERS = 5
AI_LINES = 600                  # chat lines given to the local AI at most (evenly spread over the stream)
AI_CONTEXT = 16384              # Ollama num_ctx (tokens): its default (2-4k) would silently cut the chat
                                # short; 600 lines of up to 200 characters fit in this
AI_TIMEOUT = 240.0              # a CPU model can be slow; the recap waits this long at most
MONEY = ("superchat", "supersticker", "donation", "charity")
PLATFORM_NAMES = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}


def hms(sec: float) -> str:
    """1:02:03 / 4:05 for a time into the stream."""
    s = max(0, int(sec))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def vod_at(url: str, sec: float) -> str:
    """A VOD link that opens ``sec`` seconds in: Twitch videos ?t=1h2m3s, YouTube t=123s; other
    links (a channel page) are returned unchanged and "" stays ""."""
    if not url:
        return ""
    s = max(0, int(sec))
    if "twitch.tv/videos/" in url:
        base = url.split("?")[0]
        return f"{base}?t={s // 3600}h{s % 3600 // 60}m{s % 60}s"
    if "youtube.com/watch" in url or "youtu.be/" in url or "youtube.com/live/" in url:
        return f"{url}{'&' if '?' in url else '?'}t={s}s"
    return url


def support(alerts: List[Dict[str, Any]]) -> List[str]:
    """Lines for the Support part: raids by name, then counts and totals (Core's alert kinds)."""
    out: List[str] = []
    raids = [a for a in alerts if a.get("kind") == "raid"]
    for a in raids[:8]:
        out.append(f"🚀 Raid from {a.get('display_name') or a.get('username') or 'someone'}"
                   + (f" with {int(a['viewers'])}" if a.get("viewers") else ""))
    subs = sum(1 for a in alerts if a.get("kind") in ("subscribe", "resub", "member"))
    gifted = sum(int(a.get("qty") or 1) for a in alerts if a.get("kind") in ("gift", "member_gift"))
    money: Dict[str, float] = {}
    coins: Dict[str, int] = {}
    for a in alerts:
        cur = str(a.get("currency") or "")
        try:
            amt = float(a.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if cur.lower() in ("bits", "kicks"):
            coins[cur.lower()] = coins.get(cur.lower(), 0) + int(amt)
        elif a.get("kind") in MONEY and cur:
            money[cur] = money.get(cur, 0.0) + amt
    bits = []
    if subs:
        bits.append(f"{subs} sub{'s' if subs != 1 else ''}")
    if gifted:
        bits.append(f"{gifted} gifted")
    bits += [f"{v:,.2f} {k}" for k, v in sorted(money.items())]
    bits += [f"{v:,} {'bits' if k == 'bits' else 'KICKs'}" for k, v in sorted(coins.items())]
    if bits:
        out.append("💜 " + " · ".join(bits))
    return out


def recap_text(stream: Dict[str, Any], chat: List[Dict[str, Any]], alerts: List[Dict[str, Any]],
               moments: List[Dict[str, Any]], vod: Dict[str, Any], summary: str = "",
               when: str = "") -> str:
    """The recap message (markdown, under 2000 characters). ``stream``: started_at, ended_at,
    platforms, peak. ``vod``: {url, started_at} of the owner's stream as FlaVR Promo's watcher saw it ({} = none)."""
    a, b = float(stream["started_at"]), float(stream["ended_at"])
    plats = ", ".join(PLATFORM_NAMES.get(p, p) for p in stream.get("platforms") or [])
    head = f"📼 **Stream recap**{' · ' + when if when else ''} · {length(b - a)}" + (f" on {plats}" if plats else "")
    lines = [head]
    over = []
    if stream.get("peak"):
        over.append(f"👀 {int(stream['peak'])} viewers at most")
    if chat:
        people = {(c.get("platform"), (c.get("username") or "").lower()) for c in chat}
        over.append(f"💬 {len(chat):,} chat lines from {len(people):,} chatters")
    if over:
        lines.append(" · ".join(over))
    if chat:
        counts = Counter((c.get("display_name") or c.get("username") or "?") for c in chat)
        lines.append("🏆 Top chatters: " + ", ".join(f"{n} ({k})" for n, k in counts.most_common(TOP_CHATTERS)))
    lines += support(alerts)
    if summary:
        lines += ["", "📝 " + summary.strip()]
    if moments:
        lines += ["", "⚡ **Moments** (chat suddenly busy)"]
        vstart = float(vod.get("started_at") or a)
        for m in moments[:12]:
            t = float(m["at"]) - vstart
            link = vod_at(vod.get("url") or "", t)
            bit = f"[{hms(t)}](<{link}>)" if link else hms(float(m["at"]) - a)
            bit += f" · {int(m.get('cpm') or 0)} lines/min (usually {int(m.get('baseline') or 0)})"
            if m.get("clip"):
                bit += f" · [clip](<{m['clip']}>)"
            lines.append("• " + bit)
    text = "\n".join(lines)
    while len(text) > 1990 and len(lines) > 2:
        lines.pop()
        text = "\n".join(lines)
    return text


def clean_mentions(text: str) -> str:
    """No @everyone / @here / <@id> from the AI's words (allowed_mentions is none too)."""
    return re.sub(r"@(everyone|here)", r"@​\1", re.sub(r"<@[!&]?\d+>", "", text or ""))


async def ai_summary(url: str, model: str, chat: List[Dict[str, Any]], post: Optional[Callable] = None) -> str:
    """A few sentences from a local Ollama on what happened in chat; "" when it isn't set up or fails.
    ``post``(url, json) -> json is swapped in tests; the default uses aiohttp."""
    if not url or not chat:
        return ""
    step = max(1, len(chat) // AI_LINES)
    sample = chat[::step][:AI_LINES]
    log_text = "\n".join(f"{c.get('display_name') or c.get('username')}: {' '.join((c.get('message') or '').split())[:200]}"
                         for c in sample)
    prompt = ("These are chat messages from a livestream, oldest first. In 3 to 5 short, friendly sentences, "
              "say what the stream was about and the funniest or biggest moments chat reacted to. Plain text, "
              "no lists, no @ mentions, no made-up details.\n\n" + log_text)
    body = {"model": model, "prompt": prompt, "stream": False, "options": {"num_ctx": AI_CONTEXT}}
    try:
        if post is None:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT)) as s:
                async with s.post(f"{url}/api/generate", json=body) as r:
                    data = await r.json(content_type=None)
        else:
            data = await post(f"{url}/api/generate", body)
    except Exception as e:
        log.warning("local AI summary failed: %s", e)
        return ""
    text = str((data or {}).get("response") or "").strip()
    return clean_mentions(text)[:700]


class Recapper:
    def __init__(self, store: Store, send_channel: Callable[[str, str], Awaitable[None]],
                 ping: Callable[..., Awaitable[None]], heartbeat: Callable[[], Dict[str, Any]],
                 delay_min: int = 3, ollama_url: str = "", ollama_model: str = "", tz: Any = None,
                 clock: Callable[[], float] = time.time, ai_post: Optional[Callable] = None,
                 streams: Callable[[], List[Dict[str, Any]]] = lambda: []):
        self.store = store
        self.send_channel = send_channel        # (channel id, text): flavrbot/bot.py post_text
        self.ping = ping                        # Pinger.ping(kind, text)
        self.heartbeat = heartbeat              # CoreLink's newest heartbeat (viewers)
        self.delay = delay_min * 60.0
        self.ollama_url, self.ollama_model = ollama_url, ollama_model
        self.tz = tz
        self.clock = clock
        self.ai_post = ai_post
        # the owner's recent streams [{started_at, ended_at, url, vod_url}] from FlaVR Promo
        # (flavrbot/promo_link.py PromoLink.owner_streams; [] without PROMO_URL = no VOD links)
        self.streams = streams

    # ── Core's messages (flavrbot/core_link.py callbacks) ──────────────
    async def on_stream(self, change: Dict[str, Any]) -> None:
        ev = change.get("event")
        if ev == "started":
            self.store.put("recap_live", {"started_at": change.get("started_at") or self.clock(), "peak": 0,
                                          "platforms": change.get("platforms") or []})
        elif ev == "ended":
            live = self.store.get("recap_live") or {}
            self.store.put("recap_pending", {"started_at": change.get("started_at") or live.get("started_at"),
                                             "ended_at": change.get("ended_at") or self.clock(),
                                             "platforms": sorted(set(live.get("platforms") or []) |
                                                                 set(change.get("platforms") or [])),
                                             "peak": live.get("peak") or 0})
            self.store.delete("recap_live")

    async def on_alert(self, a: Dict[str, Any]) -> None:
        self.store.add_alert(a, self.clock())

    async def on_moment(self, m: Dict[str, Any]) -> None:
        self.store.add_moment(m)
        text = f"⚡ Chat moment: {int(m.get('cpm') or 0)} lines/min (usually {int(m.get('baseline') or 0)})"
        if m.get("clip"):
            text += f"\n🎬 Twitch clip: <{m['clip']}>"
        elif m.get("clip_error"):
            text += f"\n(no clip: {m['clip_error']})"
        await self.ping("chat_moments", text)

    # ── the loop ───────────────────────────────────────────────────────
    def watch(self) -> None:
        """While live: keep the most viewers Core reported (and the platforms seen)."""
        live = self.store.get("recap_live")
        if not live:
            return
        hb = self.heartbeat() or {}
        v = int(hb.get("viewers") or 0)
        plats = set(live.get("platforms") or []) | set((hb.get("stream") or {}).get("platforms") or [])
        if v > int(live.get("peak") or 0) or plats != set(live.get("platforms") or []):
            self.store.put("recap_live", {**live, "peak": max(v, int(live.get("peak") or 0)), "platforms": sorted(plats)})

    async def tick(self) -> Optional[str]:
        self.watch()
        p = self.store.get("recap_pending")
        if not p or self.clock() < float(p["ended_at"]) + self.delay:
            return None
        self.store.delete("recap_pending")
        if float(p["ended_at"]) - float(p["started_at"] or 0) < 300:
            return None                         # under 5 minutes: a test, not a stream
        text = await self.build(p)
        channel = str(self.store.get("recap_channel") or "")
        if channel:
            try:
                await self.send_channel(channel, text)
            except Exception:
                log.exception("recap post failed")
        await self.ping("recap", text)
        return text

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("recap failed")
            await asyncio.sleep(TICK)

    async def build(self, p: Dict[str, Any]) -> str:
        a, b = float(p["started_at"]), float(p["ended_at"])
        chat = await asyncio.to_thread(self.store.chat_between, a - 60, b + 60)
        alerts = self.store.alerts_between(a - 60, b + 60)
        moments = self.store.moments_between(a - 60, b + 60)
        summary = await ai_summary(self.ollama_url, self.ollama_model, chat, self.ai_post)
        when = ""
        if self.tz is not None:
            from datetime import datetime
            d = datetime.fromtimestamp(a, self.tz)
            when = f"{d:%A} {d:%b} {d.day}"
        return recap_text(p, chat, alerts, moments, self.vod(a, b), summary, when)

    def vod(self, a: float, b: float) -> Dict[str, Any]:
        """The owner's stream that overlaps [a, b] as FlaVR Promo's watcher saw it: {url, started_at}
        ({} = none). The earliest one that started up to 6 h before Core's start and hadn't ended
        before it; its VOD when Promo found one, else its live link."""
        for s in sorted(self.streams(), key=lambda x: float(x.get("started_at") or 0)):
            try:
                start = float(s["started_at"])
            except (KeyError, TypeError, ValueError):
                continue
            end = float(s.get("ended_at") or b)
            if start < a - 6 * 3600 or start >= b or end < a:
                continue
            url = s.get("vod_url") or s.get("url") or ""
            return {"url": url, "started_at": start} if url else {}
        return {}
