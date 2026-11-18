"""
Copy-ready calendar text for posting elsewhere (other Discord servers, X / Twitter). The bot can't
post to X by itself (X's API needs a paid plan), so it DMs the owner a package to paste:

  1. the calendar picture (save it, attach it anywhere)
  2. a Discord version: the day-by-day list with links, every stream, one message per day. Times are Discord time stamps (<t:…:t>), so
     every reader sees their own time zone in any server. Links are wrapped in <…> so pasting it
     doesn't make a wall of link previews. Sent as its own message: long-press / right-click →
     Copy Text copies exactly what to paste.
  3. an X version: plain text, times written in the bot's time zone (TZ, X has no time stamps),
     people's X handles when set (/friend x). Three styles (X_STYLES; default set with /setup
     share-style, kv ``share_x_style``, default "reply"): the schedule post plus a "Links 👇" post
     to paste as its reply (links in the main post lower its reach on X), everything in one long
     post (X Premium allows 25,000 characters), or a 280-character numbered thread without links.
     Each post is its own message.
  4. an X Article (style "article"): X's long-form editor takes rich text (headings, bold, lists,
     links), which a Discord DM can't carry, so ``article`` builds an HTML page and the DM links to
     it (flavrbot/web.py /calendar/article, home network only); its Copy button copies the
     formatted text to paste into the Article editor, plus the title and the picture for the cover.
     Each day is one bulleted list of short lines ("12:21a · Sensoka · Weeb Night · 5h · VOD ↗"),
     titles without @names / #hashtags (article_title), and one "Follow:" line at the end.

Who calls it: flavrbot/calendar_post.py ``CalendarPoster.share`` (after the Sunday post and the
Saturday recap, when the /pings switch "share" is on, held until quiet hours end) and /calendar
share in flavrbot/bot.py.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, List

from . import groups
from .calendar import DAY_NAMES, Tile, Week

DISCORD_MAX = 2000          # one Discord message
X_MAX = 280                 # one X post (weighted: see x_len)
X_LINK = 23                 # X counts every link as 23 characters


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


def _tz_name(week: Week) -> str:
    return datetime.fromtimestamp(week.start.timestamp() + 86400, week.tz).tzname() or ""


ZWSP = "\u200b"             # zero-width space: invisible, but "@\u200bname" no longer tags anyone


def no_tags(text: str) -> str:
    """Make pasted text tag nobody, on Discord or X: a zero-width space after every @ ("@everyone",
    "@here", "@name", Discord's <@123> / <@&123> codes). Looks the same; nobody gets a notification."""
    return (text or "").replace("@", "@" + ZWSP)


def _clean(text: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", (text or "").replace("[", "(").replace("]", ")").replace("`", "'")).strip()
    return no_tags(t if len(t) <= n else t[: n - 1].rstrip() + "…")


def _heading(week: Week) -> str:
    return groups.heading(week.group, week.recap)


def discord_parts(week: Week) -> List[str]:
    """The Discord version, EVERY stream, one message per day (Sen pastes them one after another).
    A day longer than 2000 characters (what a bot may send) is split between streams. Past streams
    link their VOD, scheduled ones their page."""
    head = f"🗓️ **{_heading(week)}** · {week.range_text}"
    blocks: List[str] = []
    for i, tiles in enumerate(week.days):
        if not tiles:
            continue
        date = week.start + timedelta(days=i)
        day = [f"**{DAY_NAMES[i].title()} {date:%b} {date.day}**"]
        for t in tiles:
            when = f"<t:{int(t.at)}:t>" + (f"–<t:{int(t.end)}:t>" if t.kind == "past" and t.end else "")
            what = _clean(t.title or t.game)
            mark = {"past": "✅", "live": "🔴", "scheduled": "•"}[t.kind]
            link = f" [{'VOD' if t.kind == 'past' else 'watch'}](<{t.url}>)" if t.url else ""
            day.append(f"{mark} {when} **{no_tags(t.name)}**" + (f" · {what}" if what else "") + link)
        # a day longer than one message (rare) is cut between streams, its heading repeated
        chunk: List[str] = []
        for row in day[1:]:
            if chunk and len("\n".join([day[0]] + chunk + [row])) > DISCORD_MAX - 50:
                blocks.append("\n".join([day[0]] + chunk))
                chunk = []
            chunk.append(row[: DISCORD_MAX - 100])
        blocks.append("\n".join([day[0]] + chunk))
    if not blocks:
        return [head + "\n" + ("No streams this week." if week.recap else "Nothing on the calendar.")]
    if week.recap:
        blocks.append(f"{week.stream_count} streams · {week.hours_live:.0f} hours live")
    # one message per day (Sen: easier to paste, and a day never reaches the limit); the heading rides
    # on the first day, the totals line on the last
    parts = list(blocks[:-1] if week.recap else blocks)
    parts[0] = head + "\n\n" + parts[0]
    if week.recap:
        parts[-1] += "\n\n" + blocks[-1]
    return parts


def discord_text(week: Week) -> str:
    """All of the Discord version in one string (tests, the web page); messages use discord_parts."""
    return "\n\n".join(discord_parts(week))


def _who(t: Tile, handles: Dict[int, str]) -> str:
    """The name on X: a real @tag only for handles x_posts kept (tag_people on), else their name
    with any @ in it defused."""
    h = (handles.get(t.person_id) or "").strip().lstrip("@")
    return f"@{h}" if h else no_tags(t.name)


X_STYLES = {
    # /setup share-style and /calendar share style: (label, what you get)
    "reply": ("Post + links replies", "the schedule as one post, then one links post per day to paste as replies"),
    "one": ("All in one post", "one long post with every stream and its link (needs X Premium past 280)"),
    "thread": ("280-character thread", "no X Premium: the schedule split into numbered posts, no links"),
    "article": ("X Article", "a link to a page on the bot's web server to copy into X's Article editor"),
}
X_PREMIUM_MAX = 25000       # X Premium's longest post


def _link(t: Tile) -> str:
    """The stream's link: its VOD / live page / scheduled page, else any of its platform links."""
    return t.url or next((u for u in t.links.values() if u), "")


def _ampm(ts: float, tz) -> str:
    """"7 PM", "7:30 PM" (the X text's times; easier to read than "7p")."""
    d = datetime.fromtimestamp(ts, tz)
    return f"{d.hour % 12 or 12}{':%02d' % d.minute if d.minute else ''} {'AM' if d.hour < 12 else 'PM'}"


BOLD = {**{chr(ord("A") + k): chr(0x1D5D4 + k) for k in range(26)},
        **{chr(ord("0") + k): chr(0x1D7EC + k) for k in range(10)}}


def _x_day(i: int, week: Week, bold: bool = False) -> str:
    """A day heading for X: "SUN · NOV 15", or in bold-looking letters ("𝗦𝗨𝗡 · 𝗡𝗢𝗩 𝟭𝟱") when ``bold``.
    Those letters are Unicode look-alikes (X has no real bold): screen readers and X search don't read
    them, so they're only ever used for these short headings, never names or titles."""
    date = week.start + timedelta(days=i)
    text = f"{DAY_NAMES[i]} · {date:%b}".upper() + f" {date.day}"
    return "".join(BOLD.get(c, c) for c in text) if bold else text


def _x_row(t: Tile, week: Week, handles: Dict[int, str]) -> str:
    """One stream: "▸ 7 PM · @luluteala" (recap: "▸ 7 PM · @luluteala · 3 h")."""
    row = f"▸ {_ampm(t.at, week.tz)} · {_who(t, handles)}"
    if t.kind == "past" and t.end:
        hours = t.length / 3600
        row += f" · {hours:.1f} h".replace(".0 h", " h") if hours >= 1 else f" · {int(t.length // 60)} min"
    return row


def channel_url(person: Dict[str, Any]) -> str:
    """A person's channel page on their main platform (default YouTube, then Twitch, Kick): the one
    link per person in the X week in review. "" when no account is known yet."""
    accs = {a["platform"]: a for a in person.get("accounts") or []}
    for plat in [person.get("main_platform") or "youtube", "youtube", "twitch", "kick"]:
        a = accs.get(plat)
        if not a:
            continue
        h = (a.get("handle") or "").strip()
        if plat == "youtube":
            if h.startswith("UC") and len(h) == 24:
                return f"https://www.youtube.com/channel/{h}"
            return f"https://www.youtube.com/@{h.lstrip('@')}" if h else ""
        if h:
            return f"https://{'twitch.tv' if plat == 'twitch' else 'kick.com'}/{h.lstrip('@')}"
    return ""


def _recap_people(week: Week) -> List[Dict[str, Any]]:
    """The week in review per person, most hours first: [{person_id, name, tile, streams, hours}]."""
    by: Dict[int, Dict[str, Any]] = {}
    for t in week.tiles():
        if t.kind != "past":
            continue
        r = by.setdefault(t.person_id, {"person_id": t.person_id, "name": t.name, "tile": t, "streams": 0, "hours": 0.0})
        r["streams"] += 1
        r["hours"] += t.length / 3600
    return sorted(by.values(), key=lambda r: (-r["hours"], r["name"].lower()))


def _day_links(days, week: Week, handles: Dict[int, str], bold: bool, channels: Dict[int, str]) -> List[str]:
    """The links as replies, one post per day ("🔗 SUN · NOV 8", then "Name · 6:57 AM" over each
    stream's own link: its VOD once it's over, its page while live or scheduled). The channel page
    only stands in when a stream has no link of its own."""
    posts = []
    for i, tiles in days:
        rows, seen = [], set()
        for t in tiles:
            url = _link(t) or channels.get(t.person_id, "")
            if url and url not in seen:
                seen.add(url)
                rows.append(f"{_who(t, handles)} · {_ampm(t.at, week.tz)}\n{url}")
        if rows:
            posts.append("\n\n".join([f"🔗 {_x_day(i, week, bold)}"] + rows)[:X_PREMIUM_MAX])
    return posts


def _recap_posts(week: Week, head: str, handles: Dict[int, str], channels: Dict[int, str], style: str,
                 tail: List[str], days, bold: bool) -> List[str]:
    """The X week in review: the main post is one line per person ("▸ Lulu Teala · 5 streams · 19 h"),
    not per stream (38 lines is too much to read on X). The VODs follow: one reply per day ("reply"),
    or day blocks under the summary ("one")."""
    rows = []
    for r in _recap_people(week):
        hours = f"{r['hours']:.0f} h" if r["hours"] >= 1 else f"{r['hours'] * 60:.0f} min"
        rows.append(f"▸ {_who(r['tile'], handles)} · {r['streams']} stream{'s' if r['streams'] != 1 else ''} · {hours}")
    summary = "\n\n".join([head, "\n".join(rows)] + tail)[:X_PREMIUM_MAX]
    if style == "reply":
        return [summary] + _day_links(days, week, handles, bold, channels)
    if style == "one":
        blocks = []
        for i, tiles in days:
            lines = [_x_day(i, week, bold)]
            for t in tiles:
                lines.append(_x_row(t, week, handles))
                if _link(t):
                    lines.append(f"   {_link(t)}")
            blocks.append("\n".join(lines))
        return ["\n\n".join([head, "\n".join(rows)] + blocks + tail)[:X_PREMIUM_MAX]]
    packed: List[List[str]] = [[head, ""]]
    for row in rows:
        if x_len("\n".join(packed[-1] + [row])) > X_MAX - 8:
            packed.append([])
        packed[-1].append(row)
    texts = ["\n".join(p).strip() for p in packed]
    if tail and x_len(texts[-1] + "\n\n" + tail[0]) <= X_MAX - 8:
        texts[-1] += "\n\n" + tail[0]
    if len(texts) > 1:
        texts = [f"{t}\n\n({i}/{len(texts)})" for i, t in enumerate(texts, 1)]
    return texts


def x_posts(week: Week, handles: Dict[int, str] | None = None, style: str = "reply", bold: bool = False,
            tags: str = "", channels: Dict[int, str] | None = None, tag_people: bool = False) -> List[str]:
    """The X version as a list of posts to paste in order. ``handles``: person id → X handle
    (/friend x). ``style`` (X_STYLES): "reply" = schedule post + links reply, "one" = one long post
    with links, "thread" = 280-character numbered thread without links.

    X shows no markdown (asterisks would print as they are), so the look comes from layout alone:
    a two-line heading, then a block per day (a "SUN · NOV 15" heading, one "▸ 7 PM · name" line per
    stream) with a blank line between blocks. Links go on their own lines. ``tag_people``: the /friend x
    handles as real @tags (default off: names, nobody notified). ``bold``: bold-looking day
    headings; ``tags``: a hashtag line at the end of the schedule post (/setup share-style).
    "reply" puts the links in one reply post per day. The week in review's main post is a per-person
    summary (``_recap_posts``). ``channels``: person id → their channel page, only used for a stream
    with no link of its own."""
    # Tags: the X handles saved with /friend x become real @tags only with tag_people on (/setup
    # share-style); off (the default) = everyone by their name. @s inside names and titles are
    # always defused (no_tags), so nothing pasted tags anyone by accident.
    handles = (handles or {}) if tag_people else {}
    if week.recap:
        head = (f"📼 {_heading(week)}\n{week.range_text} · {week.stream_count} streams · "
                f"{week.hours_live:.0f} hours live")
    else:
        head = f"🗓️ {_heading(week)}\n{week.range_text} · times in {_tz_name(week)}"
    days = [(i, tiles) for i, tiles in enumerate(week.days) if tiles]
    tail = [tags.strip()] if tags.strip() else []
    if not days:
        return ["\n\n".join([head + ("\n\nNo streams this week." if week.recap else "\n\nNothing scheduled yet.")] + tail)]

    if week.recap:
        return _recap_posts(week, head, handles, channels or {}, style, tail, days, bold)

    if style == "one":
        blocks = [head]
        for i, tiles in days:
            rows = [_x_day(i, week, bold)]
            for t in tiles:
                # no stream titles on X: they're long, and their own #tags and @names would tag people
                # and clutter the post (a busy week had 38 of them)
                rows.append(_x_row(t, week, handles))
                if _link(t):
                    rows.append(f"   {_link(t)}")
            blocks.append("\n".join(rows))
        return ["\n\n".join(blocks + tail)[:X_PREMIUM_MAX]]

    blocks = ["\n".join([_x_day(i, week, bold)] + [_x_row(t, week, handles) for t in tiles]) for i, tiles in days]
    if style == "reply":
        return ["\n\n".join([head] + blocks + tail)[:X_PREMIUM_MAX]] + _day_links(days, week, handles, bold, channels or {})

    # "thread": pack whole day blocks into 280-character posts; leave room for a "(2/3)" counter
    packed: List[List[str]] = [[head]]
    for b in blocks:
        while x_len(b) > X_MAX - 8:              # one day too long for a post: drop its last streams
            b = b.rsplit("\n", 1)[0]
        if x_len("\n\n".join(packed[-1] + [b])) > X_MAX - 8:
            packed.append([])
        packed[-1].append(b)
    if tail and x_len("\n\n".join(packed[-1] + tail)) <= X_MAX - 8:
        packed[-1] += tail
    texts = ["\n\n".join(p) for p in packed]
    if len(texts) > 1:
        texts = [f"{t}\n\n({i}/{len(texts)})" for i, t in enumerate(texts, 1)]
    return texts


# ── X Article (style "article") ──────────────────────────────────────────────
def _link_label(t: Tile) -> str:
    """The X Article's short link words at the end of a stream's line ("VOD ↗"). The space before
    the arrow is a no-break space, so the link never wraps onto a line of its own."""
    word = {"past": "VOD", "live": "Live", "scheduled": "Page"}.get(t.kind, "Watch")
    return word + NBSP + "↗"


NBSP = " "
ARTICLE_TITLE_MAX = 45      # characters of a stream title in the X Article (cut at a word)
# @names, Discord mentions (<@123>, <@&123>, <#123>) and #hashtags inside stream titles: X turns
# them into tags and hashtag links, so the X Article drops them (the week's hashtag line stays)
_TITLE_TAGS = re.compile(r"<[@#][&!]?\d+>|[@＠]\w[\w.]*|(?<![\w&])#\w+")
# words left dangling at the end once a name is gone: "Weeb Night with @batsharkvt" → "Weeb Night"
_TITLE_TAIL = re.compile(r"(?:\s*[-–—|,:/&+]|\s+(?:with|w/|ft\.?|feat\.?|x|and|collab(?: with)?|vs\.?))+\s*$",
                         re.IGNORECASE)


def article_title(text: str, n: int = ARTICLE_TITLE_MAX) -> str:
    """A stream title for the X Article: no @names / #hashtags, no dangling "with", cut at a word
    near ``n`` characters with "…". "" when nothing is left (the line then has no title)."""
    t = _TITLE_TAGS.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[(\[【]\s*[)\]】]", "", t).strip()        # brackets that only held a tag
    for _ in range(3):
        t = _TITLE_TAIL.sub("", t).strip()
    if len(t) > n:
        cut = t[:n].rsplit(" ", 1)[0] if " " in t[:n] else t[:n]
        t = _TITLE_TAIL.sub("", cut.rstrip(" ,.;:-–—")).strip() + "…"
    return no_tags(t)


def article_length(sec: float) -> str:
    """Stream length for the X Article: "45m", "5h", "1h 54m"."""
    m = int(round((sec or 0) / 60))
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h" + (f" {m % 60:02d}m" if m % 60 else "")


def _short_time(ts: float, tz) -> str:
    """"12:21a", "4p": the X Article's times (short, so a line rarely wraps)."""
    d = datetime.fromtimestamp(ts, tz)
    return f"{d.hour % 12 or 12}{':%02d' % d.minute if d.minute else ''}{'a' if d.hour < 12 else 'p'}"


def article(week: Week, handles: Dict[int, str] | None = None, tags: str = "", tag_people: bool = False,
            channels: Dict[int, str] | None = None, save: Any = None) -> Dict[str, Any]:
    """The week as an X Article: {"title", "html", "text", "pictures"}. ``html`` is the body (no
    <html>): a summary line, a "Who streamed" list for the recap, then per day an <h2>, that day's
    picture and that day's streams as one bulleted list (bold "time · name", a short title, its
    length, and a short "VOD ↗" link: Sen picked this over "▶ Watch the VOD", which wrapped and
    looked messy); then one "Follow:" line linking each person who appears to their channel
    (``channels``: person id → channel_url), once at the end; then the hashtag line.

    X Articles allow only so many pictures, so it's one picture per day (render.day_card: that day's
    thumbnails in a grid), not one per stream. ``pictures``: [(number, zip file name, day index)].
    ``save``(day index) gives the bot's own address of that day's picture (flavrbot/web.py
    /calendar/day.png); it sits in a ``nocopy`` block (the Copy button leaves it out: X can't fetch
    pictures from the home network) with a Save link, to upload with X's picture button. "" or no
    ``save`` = no picture block. ``text`` is plain text (fallback, tests). Titles go through
    ``article_title`` (no @names or #hashtags, cut near ARTICLE_TITLE_MAX); names become real @tags
    only with tag_people (/setup share-style), the same rule as x_posts."""
    handles = (handles or {}) if tag_people else {}
    channels = channels or {}
    e = html.escape
    title = f"{_heading(week)} · {week.range_text}"
    if week.recap:
        summary = f"{week.stream_count} streams · {week.hours_live:.0f} hours live · times in {_tz_name(week)}"
    else:
        summary = f"Times in {_tz_name(week)} · {sum(len(d) for d in week.days)} streams so far"
    parts = [f"<p>{e(summary)}</p>"]
    text = [title, summary]
    pictures: List[Any] = []
    follow: Dict[int, str] = {}         # person id → name as shown, in order of first stream (the Follow line)
    if week.recap and week.tiles():
        rows = []
        for r in _recap_people(week):
            hours = f"{r['hours']:.0f} h" if r["hours"] >= 1 else f"{r['hours'] * 60:.0f} min"
            line = f"{_who(r['tile'], handles)} · {r['streams']} stream{'s' if r['streams'] != 1 else ''} · {hours}"
            rows.append(f"<li><b>{e(_who(r['tile'], handles))}</b> · {e(line.split(' · ', 1)[1])}</li>")
            text.append("• " + line)
        parts += ["<h2>Who streamed</h2>", "<ul>" + "".join(rows) + "</ul>"]
        text.insert(2, "Who streamed")
    for i, tiles in enumerate(week.days):
        if not tiles:
            continue
        date = week.start + timedelta(days=i)
        head = f"{date:%A}, {date:%B} {date.day}"
        rows = []
        text.append(head)
        for t in tiles:
            short = article_title(t.title or t.game)
            bits = [short] if short else []
            if t.kind == "past" and t.end:
                bits.append(article_length(t.length))
            if t.kind == "live":
                bits.append("live now")
            url = _link(t) or channels.get(t.person_id, "")
            who = _who(t, handles)
            if t.person_id not in follow:
                follow[t.person_id] = who
            when = _short_time(t.at, week.tz)
            link = f' ·{NBSP}<a href="{e(url, quote=True)}">{e(_link_label(t))}</a>' if url else ""
            rows.append(f"<li><b>{e(when)} · {e(who)}</b>" + "".join(f" · {e(b)}" for b in bits) + link + "</li>")
            text.append(f"• {when} · {who}" + "".join(f" · {b}" for b in bits) + (f" · {url}" if url else ""))
        pics = []
        n = len(pictures) + 1
        name = f"{n:02d}-{DAY_NAMES[i][:3].lower()}.png"
        pictures.append((n, name, i))
        local = save(i) if save else ""
        if local:
            # the day picture lives on the bot (home network), which X can't fetch: it's on the page
            # to save and upload with X's picture button, and the copy leaves it out (nocopy)
            pics = [f'<div class="nocopy"><img src="{e(local, quote=True)}" alt="{e(head)}">'
                    f'<p>Picture {n} · <a href="{e(local, quote=True)}" download="{name}">Save</a> · '
                    "put it here with X's picture button</p></div>"]
        # one tight list per day (separate <p>s left a big gap between every stream in X's editor)
        parts += [f"<h2>{e(head)}</h2>"] + pics + ["<ul>" + "".join(rows) + "</ul>"]
    if not week.tiles():
        parts.append(f"<p>{'No streams this week.' if week.recap else 'Nothing scheduled yet.'}</p>")
    # everyone who appears, once, linked to their channel (main platform first, see channel_url)
    links = [(who, channels[pid]) for pid, who in follow.items() if channels.get(pid)]
    if links:
        parts.append("<p><b>Follow:</b> " + " · ".join(f'<a href="{e(u, quote=True)}">{e(w)}</a>' for w, u in links) + "</p>")
        text.append("Follow: " + " · ".join(f"{w} {u}" for w, u in links))
    if tags.strip():
        parts.append(f"<p>{e(tags.strip())}</p>")
        text.append(tags.strip())
    return {"title": title, "html": "\n".join(parts), "text": "\n".join(text), "pictures": pictures}


__all__ = ["discord_parts", "discord_text", "x_posts", "x_len", "X_STYLES", "channel_url", "article"]
