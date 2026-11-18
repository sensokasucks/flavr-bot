"""
Draws the weekly calendar: a ``Week`` (flavrbot/calendar.py) → an HTML page → a 1600 x 900 PNG.

The same HTML is used three ways, so they always look the same:
  * flavrbot/calendar_post.py screenshots it (headless Chromium through Playwright) for Discord
    and the owner's DM; pictures are inlined as data: URLs so the browser needs no network;
  * flavrbot/web.py serves it as a page on the home network (also the OBS Browser Source), with
    pictures from /calendar/img/... and a reload every 5 minutes.

Pictures (thumbnails, box art, profile pictures) are downloaded once by ``ImageCache`` into
DATA_DIR/cache/img and reused; anything over PRUNE_DAYS old is deleted. Live previews carry a
"?t=" that changes every 5 minutes, so they refresh by themselves.

Looks (decided with the owner, plan doc "Weekly FlaVR calendar plan"):
  FlaVR    the logo + its colours: pink #f72ca2, lime #b7ef33, yellow #f8cf33, teal #2ae0d4; day
           numbers cycle through them; today outlined teal with a yellow TODAY tag
  Friends  the same layout in plain teal with a text title, so the two never get mixed up
  Busy days (more than BIG_TILES streams) switch that day to compact rows; past COMPACT_MAX the
  day ends with "+N more" (the text list under the Discord post always has every stream).

Chromium: CHROMIUM_PATH if set, else Playwright's own (the Docker image installs it).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import mimetypes
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import aiohttp

from . import groups
from .calendar import DAY_NAMES, Tile, Week

log = logging.getLogger("flavrbot.render")
ASSETS = Path(__file__).resolve().parent / "assets"
WIDTH, HEIGHT = 1600, 900
BIG_TILES = 3           # up to this many streams a day: big picture tiles
COMPACT_MAX = 7         # compact rows that fit in one day's column
PRUNE_DAYS = 21
MAX_PICTURE = 3 * 1024 * 1024
PLATFORM_BADGE = {"youtube": ("Y", "#ff0033", "#fff"), "twitch": ("T", "#9146ff", "#fff"), "kick": ("K", "#53fc18", "#000")}
TZ_WORDS = {"America/New_York": "Eastern time", "America/Chicago": "Central time", "America/Denver": "Mountain time",
            "America/Los_Angeles": "Pacific time", "Europe/London": "UK time"}


# ── pictures ───────────────────────────────────────────────────────────
class ImageCache:
    """Downloads each picture once (who: render + web). ``path(url)`` is where it would be."""

    def __init__(self, folder: Path, http: Optional[aiohttp.ClientSession] = None):
        self.folder = Path(folder)
        self.http = http
        self._fail: dict[str, float] = {}         # url → when it last failed (retried after an hour)

    def name(self, url: str) -> str:
        ext = Path(url.split("?", 1)[0]).suffix.lower()
        ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".img"
        return hashlib.sha1(url.encode()).hexdigest()[:24] + ext

    def path(self, url: str) -> Path:
        return self.folder / self.name(url)

    async def fetch(self, url: str) -> Optional[Path]:
        if not url or not url.startswith(("https://", "http://")):
            return None
        p = self.path(url)
        if p.exists():
            return p
        if self.http is None or time.time() - self._fail.get(url, 0) < 3600:
            return None
        try:
            async with self.http.get(url) as r:
                if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                    raise ValueError(f"HTTP {r.status} {r.headers.get('Content-Type', '')}")
                body = b""
                async for chunk in r.content.iter_chunked(64 * 1024):     # read() alone may stop at one chunk
                    body += chunk
                    if len(body) > MAX_PICTURE:
                        raise ValueError("picture too big")
        except Exception as e:
            self._fail[url] = time.time()
            log.info("picture %s not fetched: %s", url[:80], e)
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(p)
        return p

    async def fetch_all(self, week: Week) -> None:
        urls = {u for t in week.tiles() for u in (t.thumb, t.avatar) if u}
        await asyncio.gather(*(self.fetch(u) for u in urls))

    def prune(self) -> None:
        if not self.folder.exists():
            return
        old = time.time() - PRUNE_DAYS * 86400
        for f in self.folder.iterdir():
            try:
                if f.stat().st_mtime < old:
                    f.unlink()
            except OSError:
                pass

    def data_url(self, url: str) -> str:
        """The cached picture as a data: URL ("" when it isn't cached)."""
        p = self.path(url) if url else None
        if not p or not p.exists():
            return ""
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def logo_data_url() -> str:
    return "data:image/png;base64," + base64.b64encode((ASSETS / "flavr_logo.png").read_bytes()).decode()


# ── text helpers ───────────────────────────────────────────────────────
def clock(ts: float, tz, short: bool = False) -> str:
    """8:02p (short) or 8:02 PM; whole hours drop ":00" in the long form."""
    d = datetime.fromtimestamp(ts, tz)
    h = (d.hour % 12) or 12
    if short:
        return f"{h}:{d.minute:02d}{'a' if d.hour < 12 else 'p'}"
    return f"{h}{':%02d' % d.minute if d.minute else ''} {'AM' if d.hour < 12 else 'PM'}"


def hm(sec: float) -> str:
    m = int(sec) // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def viewers(n: int) -> str:
    return f"{n / 1000:.1f}K".replace(".0K", "K") if n >= 1000 else str(n)


def tz_words(tz) -> str:
    return TZ_WORDS.get(getattr(tz, "key", ""), getattr(tz, "key", "local time"))


# ── the page ───────────────────────────────────────────────────────────
CSS = """
*{box-sizing:border-box;margin:0} html,body{background:#0e0f14}
body{width:1600px;height:900px;overflow:hidden;font-family:"DejaVu Sans","Noto Sans","Noto Sans CJK JP",Arial,sans-serif;color:#eef;padding:28px 36px;
 background-image:radial-gradient(circle at 85% -10%,var(--glow1) 0,transparent 40%),radial-gradient(circle at 0% 110%,var(--glow2) 0,transparent 40%)}
header{display:flex;align-items:flex-end;gap:20px;margin-bottom:18px;height:66px}
h1{display:flex;align-items:center;gap:18px;font-size:44px;letter-spacing:2px;font-weight:800;white-space:nowrap}
.logo{height:62px;filter:drop-shadow(0 0 1px #fff) drop-shadow(0 0 1px #fff)}
.range{font-size:20px;color:#aab;padding-bottom:6px;white-space:nowrap} .stats{margin-left:auto;font-size:17px;color:#ccd;padding-bottom:6px;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:12px;height:735px}
.day{background:#ffffff0a;border:1px solid #ffffff14;border-radius:14px;padding:10px;display:flex;flex-direction:column;gap:10px;overflow:hidden;min-width:0}
.day.today{border:2px solid var(--today);background:var(--today-bg);box-shadow:0 0 30px var(--today-glow)}
.day.past{opacity:.93}
.dh{display:flex;align-items:baseline;gap:8px} .dh span{font-size:15px;color:#aab;letter-spacing:2px} .dh b{font-size:26px}
.d0 .dh b{color:var(--c1)} .d1 .dh b{color:var(--c2)} .d2 .dh b{color:var(--c3)} .d3 .dh b{color:var(--c4)}
.dh em{margin-left:auto;font-style:normal;font-size:12px;background:var(--tag);color:#1a1400;padding:3px 7px;border-radius:6px;font-weight:700}
.tile{background:#00000050;border-radius:11px;overflow:hidden;border:1px solid #ffffff14;flex:none}
.tile.live{outline:2px solid #e5312f} .tile.scheduled .thumb{filter:saturate(.8)}
.thumb{position:relative;aspect-ratio:16/9;background:#1b1d26 center/cover no-repeat}
.thumb.art{background-size:contain;background-color:#1b1d26}
.thumb .noart{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:0 10px;
 font-size:15px;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:#ffffff70}
.badge{position:absolute;bottom:6px;right:6px;font-size:12px;font-weight:700;padding:2px 7px;border-radius:5px;background:#000d;color:#fff}
.badge.live{background:#e5312f;left:6px;right:auto;top:6px;bottom:auto} .badge.scheduled{background:#fffffff0;color:#111}
.plats{position:absolute;top:6px;right:6px;display:flex;gap:3px}
.plat{width:20px;height:20px;border-radius:5px;font-size:11px;font-weight:800;display:flex;align-items:center;justify-content:center}
.late{position:absolute;left:6px;bottom:6px;font-size:10px;font-weight:800;padding:2px 6px;border-radius:5px;background:var(--tag);color:#1a1400;letter-spacing:.5px}
.who{display:flex;gap:8px;align-items:flex-start;padding:8px 9px}
.av{width:30px;height:30px;border-radius:50%;flex:none;border:2px solid #ffffff30;object-fit:cover;background:#2a2d3a;
 display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#ccd}
.who>div{min-width:0} .who b{display:block;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who i{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-style:normal;font-size:11.5px;color:#aab;line-height:1.3}
.who u{display:block;text-decoration:none;font-size:11.5px;color:#cfe;margin-top:3px;white-space:nowrap}
.row{display:flex;gap:8px;align-items:center;background:#00000050;border:1px solid #ffffff14;border-radius:9px;padding:5px;flex:none;min-width:0}
.row.live{outline:2px solid #e5312f}
.row .mini{width:64px;height:36px;border-radius:6px;flex:none;background:#1b1d26 center/cover no-repeat;position:relative}
.row .mini .plat{position:absolute;top:2px;right:2px;width:14px;height:14px;font-size:8px;border-radius:4px}
.row>div{min-width:0} .row b{display:block;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row i{display:block;font-style:normal;font-size:11px;color:#aab;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .t{font-size:11px;font-weight:700;color:#fff} .row .t.live{color:#ff6b69}
.more{font-size:13px;color:#aab;text-align:center;padding-top:2px}
.empty{color:#667;font-size:14px;text-align:center;margin-top:30px}
footer{margin-top:12px;display:flex;justify-content:space-between;gap:30px;color:#99a;font-size:15px;white-space:nowrap}
footer span:first-child{overflow:hidden;text-overflow:ellipsis}
"""
# FlaVR's board keeps the logo's four colours; every other group is built by theme() from its one colour
THEMES = {
    "flavr": "--c1:#f72ca2;--c2:#b7ef33;--c3:#f8cf33;--c4:#2ae0d4;--today:#2ae0d4;--today-bg:#2ae0d414;"
             "--today-glow:#2ae0d433;--tag:#f8cf33;--glow1:#f72ca255;--glow2:#2ae0d444",
}



def theme(group: str) -> str:
    """The board's colours: FlaVR's four logo colours; every other group (Friends, /group add ones) one
    colour throughout, its /group colour (flavrbot/groups.py; Friends = teal)."""
    if group == "flavr":
        return THEMES["flavr"]
    c = groups.colour(group)
    return (f"--c1:{c};--c2:{c};--c3:{c};--c4:{c};--today:{c};--today-bg:{c}14;--today-glow:{c}33;"
            f"--tag:{c};--glow1:{c}33;--glow2:{c}22")


Img = Callable[[str], str]      # picture URL → what goes in src / url() ("" = no picture)


def _plats(t: Tile, cls: str = "plat") -> str:
    out = ""
    for p in t.platforms:
        letter, bg, fg = PLATFORM_BADGE.get(p, ("?", "#888", "#000"))
        out += f'<span class="{cls}" style="background:{bg};color:{fg}">{letter}</span>'
    return out


def _avatar(t: Tile, img: Img) -> str:
    src = img(t.avatar)
    if src:
        return f'<img class="av" src="{html.escape(src, quote=True)}">'
    initials = "".join(w[0] for w in t.name.split()[:2]).upper() or "?"
    return f'<span class="av">{html.escape(initials)}</span>'


def _badge(t: Week | Tile, week: Week) -> str:
    assert isinstance(t, Tile)
    if t.kind == "live":
        return f'<span class="badge live">LIVE{" · " + viewers(t.viewers) if t.viewers else ""}</span>'
    if t.kind == "scheduled":
        return f'<span class="badge scheduled">{clock(t.at, week.tz)}</span>'
    return f'<span class="badge">{hm(t.length)}</span>'


def _tile(t: Tile, week: Week, img: Img) -> str:
    thumb = img(t.thumb) or img(t.avatar)
    art = " art" if t.kind == "scheduled" and t.thumb and "box_art" in t.thumb.lower() else ""
    style = f' style="background-image:url(&quot;{html.escape(thumb, quote=True)}&quot;)"' if thumb else ""
    noart = "" if thumb else f'<div class="noart">{html.escape(t.game or t.title or t.name)[:40]}</div>'
    late = '<span class="late">LATE NOTICE</span>' if week.recap and t.late else ""
    line3 = ""
    if t.kind == "past":
        line3 = f"<u>{clock(t.at, week.tz, True)}–{clock(t.end or t.at, week.tz, True)}" \
                f"{' · %d restart%s' % (t.restarts, 's' if t.restarts > 1 else '') if t.restarts else ''}</u>"
    elif t.kind == "live":
        line3 = f"<u>since {clock(t.at, week.tz, True)}</u>"
    sub = t.title or t.game
    return (f'<div class="tile {t.kind}"><div class="thumb{art}"{style}>{noart}{_badge(t, week)}'
            f'<span class="plats">{_plats(t)}</span>{late}</div>'
            f'<div class="who">{_avatar(t, img)}<div><b>{html.escape(t.name)}</b>'
            f'<i>{html.escape(sub)}</i>{line3}</div></div></div>')


def _row(t: Tile, week: Week, img: Img) -> str:
    thumb = img(t.thumb) or img(t.avatar)
    style = f' style="background-image:url(&quot;{html.escape(thumb, quote=True)}&quot;)"' if thumb else ""
    if t.kind == "live":
        when = '<span class="t live">LIVE</span>'
    elif t.kind == "past":
        when = f'<span class="t">{clock(t.at, week.tz, True)} · {hm(t.length)}</span>'
    else:
        when = f'<span class="t">{clock(t.at, week.tz)}</span>'
    return (f'<div class="row {t.kind}"><span class="mini"{style}>{_plats(t)}</span><div>'
            f'<b>{html.escape(t.name)}</b>{when}<i>{html.escape(t.game or t.title)}</i></div></div>')


def page(week: Week, img: Img, logo: str = "", footer_note: str = "", refresh: int = 0) -> str:
    """The whole calendar as one HTML document (1600 x 900)."""
    today = week.today_index()
    cols = ""
    for i, tiles in enumerate(week.days):
        day = week.start.replace(day=week.start.day) if i == 0 else None
        date = (week.start.date().fromordinal(week.start.date().toordinal() + i))
        state = "today" if i == today else ("past" if 0 <= today and i < today or week.recap else "")
        if len(tiles) <= BIG_TILES:
            body = "".join(_tile(t, week, img) for t in tiles)
        else:
            shown = tiles[:COMPACT_MAX] if len(tiles) <= COMPACT_MAX else tiles[:COMPACT_MAX - 1]
            body = "".join(_row(t, week, img) for t in shown)
            if len(tiles) > len(shown):
                body += f'<div class="more">+{len(tiles) - len(shown)} more</div>'
        if not tiles:
            gone = week.recap or (0 <= today and i < today)
            body = f'<div class="empty">{"No streams" if gone else "Nothing scheduled yet"}</div>'
        del day
        cols += (f'<div class="day d{i % 4} {state}"><div class="dh"><span>{DAY_NAMES[i]}</span><b>{date.day}</b>'
                 f'{"<em>TODAY</em>" if i == today else ""}</div>{body}</div>')
    if week.recap:
        stats = f"{week.stream_count} streams · {week.hours_live:.0f} hours live"
    else:
        sched = sum(1 for t in week.tiles() if t.kind == "scheduled")
        stats = f"{week.stream_count} streamed · {sched} scheduled"
    head = (f'<img class="logo" src="{html.escape(logo, quote=True)}"> ' if logo and week.group == "flavr" else "") \
        + f"<span>{html.escape(week.title)}</span>"
    quiet = ""
    if week.quiet:
        lead = "No streams this week" if week.recap else "Nothing on yet"
        quiet = f"{lead}: {', '.join(week.quiet)}"
    updated = datetime.fromtimestamp(week.now, week.tz)
    foot = footer_note or ("Every VOD link is in the list under this picture" if week.recap else
                           f"Updated {DAY_NAMES[(updated.weekday() + 1) % 7].title()} {clock(week.now, week.tz)}")
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html><head><meta charset="utf-8">{meta}<title>{html.escape(week.title.title())}</title>'
            f'<style>:root{{{theme(week.group)}}}{CSS}</style></head><body>'
            f'<header><h1>{head}</h1><div class="range">{html.escape(week.range_text)} · {html.escape(tz_words(week.tz))}</div>'
            f'<div class="stats">{stats}</div></header><div class="grid">{cols}</div>'
            f'<footer><span>{html.escape(quiet)}</span><span>{html.escape(foot)}</span></footer></body></html>')


# ── the picture ────────────────────────────────────────────────────────
class Screenshotter:
    """One headless Chromium, started on first use and kept (a render is then ~0.3 s)."""

    def __init__(self, chromium_path: str = ""):
        self.chromium_path = chromium_path
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _start(self):
        from playwright.async_api import async_playwright     # only the picture needs it
        self._pw = await async_playwright().start()
        kw = {"executable_path": self.chromium_path} if self.chromium_path else {}
        self._browser = await self._pw.chromium.launch(args=["--no-sandbox"], **kw)

    async def png(self, doc: str) -> bytes:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                await self._start()
            page = await self._browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            try:
                await page.set_content(doc, wait_until="load", timeout=30_000)
                return await page.screenshot(type="png", full_page=False)
            finally:
                await page.close()

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = self._pw = None
