"""
Copy-ready calendar text for posting elsewhere (other Discord servers, X / Twitter). The bot can't
post to X by itself (X's API needs a paid plan), so it DMs the owner a package to paste:

  1. the calendar picture (save it, attach it anywhere)
  2. a Discord version: the day-by-day list with links. Times are Discord time stamps (<t:…:t>), so
     every reader sees their own time zone in any server. Links are wrapped in <…> so pasting it
     doesn't make a wall of link previews. Sent as its own message: long-press / right-click →
     Copy Text copies exactly what to paste.
  3. an X version: plain text, times written in the bot's time zone (TZ, X has no time stamps),
     people's X handles when set (/friend x). Three styles (X_STYLES; default set with /setup
     share-style, kv ``share_x_style``, default "reply"): the schedule post plus a "Links 👇" post
     to paste as its reply (links in the main post lower its reach on X), everything in one long
     post (X Premium allows 25,000 characters), or a 280-character numbered thread without links.
     Each post is its own message.

Who calls it: flavrbot/calendar_post.py ``CalendarPoster.share`` (after the Sunday post and the
Saturday recap, when the /pings switch "share" is on, held until quiet hours end) and /calendar
share in flavrbot/bot.py.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List

from .calendar import DAY_NAMES, Tile, Week

DISCORD_MAX = 2000          # one Discord message
X_MAX = 280                 # one X post (weighted: see x_len)
X_LINK = 23                 # X counts every link as 23 characters
GROUP_NAMES = {"flavr": "FlaVR", "friends": "friends"}


def x_len(text: str) -> int:
    """How long X thinks ``text`` is: links count 23, emoji and CJK count 2, the rest 1."""
    n = 0
    for word in re.split(r"(\s+)", text):
        if re.match(r"https?://", word):
            n += X_LINK
            continue
        for ch in word:
            wide = ord(ch) > 0xFFFF or unicodedata.east_asian_width(ch) in ("W", "F") \
                or unicodedata.category(ch) == "So"
            n += 2 if wide else 1
    return n


def _clock(ts: float, tz) -> str:
    """"7p", "7:30p", "12a" (short, for X)."""
    d = datetime.fromtimestamp(ts, tz)
    h = d.hour % 12 or 12
    return f"{h}{':%02d' % d.minute if d.minute else ''}{'a' if d.hour < 12 else 'p'}"


def _tz_name(week: Week) -> str:
    return datetime.fromtimestamp(week.start.timestamp() + 86400, week.tz).tzname() or ""


def _clean(text: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", (text or "").replace("[", "(").replace("]", ")").replace("`", "'")).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _heading(week: Week) -> str:
    name = GROUP_NAMES.get(week.group, week.group)
    if week.recap:
        return f"{name.capitalize() if name == 'friends' else name} week in review"
    return f"This week in {name}" if week.group == "flavr" else f"This week with {name}"


def discord_text(week: Week) -> str:
    """The Discord version (≤ 2000 characters). Past streams link their VOD, scheduled ones their page."""
    lines = [f"🗓️ **{_heading(week)}** · {week.range_text}"]
    rest = sum(len(d) for d in week.days)
    for i, tiles in enumerate(week.days):
        if not tiles:
            continue
        date = week.start + timedelta(days=i)
        day = [f"\n**{DAY_NAMES[i].title()} {date:%b} {date.day}**"]
        for t in tiles:
            when = f"<t:{int(t.at)}:t>" + (f"–<t:{int(t.end)}:t>" if t.kind == "past" and t.end else "")
            what = _clean(t.title or t.game)
            mark = {"past": "✅", "live": "🔴", "scheduled": "•"}[t.kind]
            link = f" [{'VOD' if t.kind == 'past' else 'watch'}](<{t.url}>)" if t.url else ""
            day.append(f"{mark} {when} **{t.name}**" + (f" · {what}" if what else "") + link)
        block = "\n".join(day)
        if len("\n".join(lines)) + len(block) + 20 > DISCORD_MAX:
            lines.append(f"\n+{rest} more")
            break
        lines.append(block)
        rest -= len(tiles)
    if len(lines) == 1:
        lines.append("Nothing on the calendar." if not week.recap else "No streams this week.")
    elif week.recap:
        lines.append(f"\n{week.stream_count} streams · {week.hours_live:.0f} hours live")
    return "\n".join(lines)[:DISCORD_MAX]


def _who(t: Tile, handles: Dict[int, str]) -> str:
    h = (handles.get(t.person_id) or "").strip().lstrip("@")
    return f"@{h}" if h else t.name


X_STYLES = {
    # /setup share-style and /calendar share style: (label, what you get)
    "reply": ("Post + links reply", "the schedule as one post, the links as a second post to paste as its reply"),
    "one": ("All in one post", "one long post with every stream and its link (needs X Premium past 280)"),
    "thread": ("280-character thread", "no X Premium: the schedule split into numbered posts, no links"),
}
X_PREMIUM_MAX = 25000       # X Premium's longest post


def _link(t: Tile) -> str:
    """The stream's link: its VOD / live page / scheduled page, else any of its platform links."""
    return t.url or next((u for u in t.links.values() if u), "")


def x_posts(week: Week, handles: Dict[int, str] | None = None, style: str = "reply") -> List[str]:
    """The X version as a list of posts to paste in order. ``handles``: person id → X handle
    (/friend x). ``style`` (X_STYLES): "reply" = schedule post + "Links 👇" reply, "one" = one long post
    with links, "thread" = 280-character numbered thread without links."""
    handles = handles or {}
    head = f"🗓️ {_heading(week)} ({week.range_text}, {_tz_name(week)})"
    if week.recap:
        head += f"\n{week.stream_count} streams · {week.hours_live:.0f} hours live"
    days = [(i, tiles) for i, tiles in enumerate(week.days) if tiles]
    if not days:
        return [head + ("\nNo streams this week." if week.recap else "\nNothing scheduled yet.")]

    if style == "one":
        lines = [head]
        for i, tiles in days:
            lines.append(f"\n{DAY_NAMES[i].title()}")
            for t in tiles:
                what = _clean(t.title or t.game, 50)
                lines.append(f"{_clock(t.at, week.tz)} {_who(t, handles)}" + (f" · {what}" if what else "")
                             + (f" {_link(t)}" if _link(t) else ""))
        return ["\n".join(lines)[:X_PREMIUM_MAX]]

    # one line a day: "Wed 4p Cara · 9p @luluteala"
    rows = [f"{DAY_NAMES[i].title()} " + " · ".join(f"{_clock(t.at, week.tz)} {_who(t, handles)}" for t in tiles)
            for i, tiles in days]
    if style == "reply":
        links = [f"{_who(t, handles)} ({DAY_NAMES[i].title()} {_clock(t.at, week.tz)}): {_link(t)}"
                 for i, tiles in days for t in tiles if _link(t)]
        posts = ["\n".join([head] + rows)[:X_PREMIUM_MAX]]
        if links:
            posts.append("\n".join(["Links 👇"] + links)[:X_PREMIUM_MAX])
        return posts

    # "thread": pack the day lines into 280-character posts; leave room for a " (2/3)" counter
    packed: List[List[str]] = [[head]]
    for r in rows:
        while x_len(r) > X_MAX - 8:              # a single packed day too long for a post: cut it
            r = r[: max(10, len(r) - 10)].rsplit(" · ", 1)[0] + " …"
        if x_len("\n".join(packed[-1] + [r])) > X_MAX - 8:
            packed.append([])
        packed[-1].append(r)
    texts = ["\n".join(p) for p in packed]
    if len(texts) > 1:
        texts = [f"{t}\n({i}/{len(texts)})" for i, t in enumerate(texts, 1)]
    return texts


__all__ = ["discord_text", "x_posts", "x_len", "X_STYLES"]
