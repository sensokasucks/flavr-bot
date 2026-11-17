"""
The calendar on the home network, served by the same little web server Stream Core talks to
(flavrbot/core_link.py, port LISTEN_PORT, default 3870). No password: these pages only show
what is public anyway (stream titles and times). Never forward the port on the router.

  GET /calendar                  this week's board as a live page (reloads every 5 minutes). Also
                                 the OBS Browser Source: 1600 x 900, ?group=friends for the friends
                                 board, ?week=next / ?week=last (last week = the recap)
  GET /calendar.png              the same board as a picture (needs Chromium, like the Discord post)
  GET /calendar.ics              calendar feed (Google Calendar, Apple Calendar, phones: "add by URL"):
                                 this week's and next week's streams, past and scheduled, ?group=
  GET /calendar/img/<file>       the cached pictures the page shows

Who calls it: flavrbot/__main__.py ``add_routes(app, poster)`` through CoreLink.extra_routes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from aiohttp import web

from .calendar import Tile, Week
from .calendar_post import CalendarPoster
from .render import logo_data_url, page
from .store import GROUPS

REFRESH_SEC = 300


def _group(request: web.Request) -> str:
    g = request.query.get("group", "flavr").lower()
    return g if g in GROUPS else "flavr"


def _offset(request: web.Request) -> int:
    return {"next": 1, "last": -1}.get(request.query.get("week", "").lower(), 0)


def _ics_text(s: str) -> str:
    """RFC 5545 text: escape \\ ; , and newlines."""
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics(weeks: List[Week], now: float) -> str:
    """Every tile of ``weeks`` as VEVENTs. Scheduled / live streams get a 2-hour placeholder length."""
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FlaVR Bot//calendar//EN", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:" + _ics_text(weeks[0].title.title() if weeks else "FlaVR"), "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    seen = set()
    for w in weeks:
        for t in w.tiles():
            uid = f"{t.person_id}-{int(t.at // 60)}@flavr-bot"
            if uid in seen:
                continue
            seen.add(uid)
            end = t.end if t.kind == "past" and t.end else t.at + 2 * 3600
            what = {"past": "", "live": " (live now)", "scheduled": ""}[t.kind]
            out += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{_stamp(now)}", f"DTSTART:{_stamp(t.at)}", f"DTEND:{_stamp(end)}",
                    "SUMMARY:" + _ics_text(f"{t.name}: {t.title or t.game or 'stream'}{what}"),
                    "DESCRIPTION:" + _ics_text(" ".join(f"{p}: {u}" for p, u in t.links.items() if u) or t.url)]
            if t.url:
                out.append("URL:" + t.url)
            out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def add_routes(app: web.Application, poster: CalendarPoster) -> None:
    async def board(request: web.Request) -> web.Response:
        off = _offset(request)
        if off < 0:
            await poster.ensure_backfill(poster.week_of(off))     # last week: fill in from the platforms once
        week = poster.build(_group(request), poster.week_of(off), recap=off < 0)
        await poster.images.fetch_all(week)
        img = lambda u: f"/calendar/img/{poster.images.name(u)}" if u and poster.images.path(u).exists() else ""  # noqa: E731
        return web.Response(text=page(week, img, logo=logo_data_url(), refresh=REFRESH_SEC),
                            content_type="text/html", charset="utf-8")

    async def picture(request: web.Request) -> web.Response:
        off = _offset(request)
        if off < 0:
            await poster.ensure_backfill(poster.week_of(off))
        week = poster.build(_group(request), poster.week_of(off), recap=off < 0)
        png = await poster.png(week)
        if not png:
            return web.Response(status=503, text="The picture needs Chromium (see the README).")
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "max-age=60"})

    async def feed(request: web.Request) -> web.Response:
        g = _group(request)
        weeks = [poster.build(g, poster.week_of(0)), poster.build(g, poster.week_of(1))]
        return web.Response(text=ics(weeks, poster.clock()), content_type="text/calendar", charset="utf-8")

    async def image(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if "/" in name or "\\" in name or name.startswith("."):
            raise web.HTTPNotFound()
        p = poster.images.folder / name
        if not p.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "max-age=3600"})

    app.router.add_get("/calendar", board)
    app.router.add_get("/calendar.png", picture)
    app.router.add_get("/calendar.ics", feed)
    app.router.add_get("/calendar/img/{name}", image)


__all__ = ["add_routes", "ics", "Tile"]
