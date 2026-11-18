"""Weekly calendar (flavrbot/calendar.py, calendar_post.py, render.py, web.py): no network, no Discord."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from flavrbot.calendar import build_week, changes, week_start
from flavrbot.calendar_post import CalendarPoster, embed
from flavrbot.render import ImageCache, page
from flavrbot.store import Store
from flavrbot.web import ics

TZ = ZoneInfo("America/New_York")


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


def setup():
    store = Store(":memory:")
    lulu = store.add_person("Lulu", "flavr")
    store.add_account(lulu, "youtube", "@luluteala")
    pip = store.add_person("Pippa", "friends")
    store.add_account(pip, "youtube", "@pipkinpippa")
    sen = store.add_person("Sensoka", "flavr", is_owner=True)
    store.add_account(sen, "twitch", "sensoka")
    return store, lulu, pip, sen


def test_week_runs_sunday_to_saturday_and_survives_dst():
    # Tue Nov 17 2026 → Sun Nov 15; Sun 00:30 is its own week; Sat 23:59 still the same week
    assert week_start(ts(2026, 11, 17, 13), TZ) == datetime(2026, 11, 15, tzinfo=TZ)
    assert week_start(ts(2026, 11, 15, 0, 30), TZ) == datetime(2026, 11, 15, tzinfo=TZ)
    assert week_start(ts(2026, 11, 21, 23, 59), TZ) == datetime(2026, 11, 15, tzinfo=TZ)
    # the week with the clock change (Sun Nov 1 2026, 2 AM) still starts at local midnight
    assert week_start(ts(2026, 11, 4, 12), TZ).hour == 0


def test_past_live_scheduled_tiles_and_matching():
    store, lulu, pip, sen = setup()
    now = ts(2026, 11, 17, 13, 12)                     # Tuesday afternoon
    # Monday: Lulu streamed 8:02p-11:30p (past, VOD); a restart counted
    s1 = store.start_session(lulu, ts(2026, 11, 16, 20, 2), platforms=["youtube"], title="Cozy games",
                             url="https://www.youtube.com/watch?v=abc", links={"youtube": "https://www.youtube.com/watch?v=abc"})
    store.update_session(s1, ended_at=ts(2026, 11, 16, 23, 30), vod_url="https://www.youtube.com/watch?v=abc",
                         vod_thumb="https://i.ytimg.com/vi/abc/hqdefault.jpg", restarts=1)
    # Sensoka live now (Tuesday 1 PM)
    store.start_session(sen, ts(2026, 11, 17, 13), platforms=["twitch"], title="Campfire", url="https://twitch.tv/sensoka",
                        viewers=1200)
    # scheduled: Sensoka at 1 PM today (replaced by the live stream), Lulu Thu 7 PM on YouTube AND Twitch
    store.replace_scheduled(sen, "twitch", [{"key": "a", "at": ts(2026, 11, 17, 13), "title": "Campfire"}], now=now)
    store.replace_scheduled(lulu, "youtube", [{"key": "v1", "at": ts(2026, 11, 19, 19), "title": "Thu stream",
                                               "url": "https://www.youtube.com/watch?v=v1"}], now=now)
    store.replace_scheduled(lulu, "twitch", [{"key": "t1", "at": ts(2026, 11, 19, 19, 10), "title": "Thu stream"}], now=now)
    # an old scheduled time that never happened (Mon 9 AM) drops off
    store.replace_scheduled(lulu, "kick", [{"key": "k0", "at": ts(2026, 11, 16, 9), "title": "never"}], now=now - 86400 * 2)
    w = build_week(store, "flavr", week_start(now, TZ), now, TZ)
    kinds = [[t.kind for t in d] for d in w.days]
    assert kinds == [[], ["past"], ["live"], [], ["scheduled"], [], []]
    thu = w.days[4][0]
    assert thu.platforms == ["youtube", "twitch"]            # one multistream tile, YouTube first
    assert w.days[1][0].restarts == 1 and w.days[1][0].url.endswith("v=abc")
    assert w.quiet == [] and w.today_index() == 2
    # friends are their own calendar
    assert build_week(store, "friends", week_start(now, TZ), now, TZ).quiet == ["Pippa"]


def test_recap_late_notice_and_manual_schedule():
    store, lulu, pip, sen = setup()
    start = datetime(2026, 11, 15, tzinfo=TZ)
    # Lulu scheduled Wed 8 PM two days ahead, then streamed on time → not late
    store.replace_scheduled(lulu, "youtube", [{"key": "v", "at": ts(2026, 11, 18, 20)}], now=ts(2026, 11, 16, 12))
    a = store.start_session(lulu, ts(2026, 11, 18, 20, 5), platforms=["youtube"])
    store.update_session(a, ended_at=ts(2026, 11, 18, 22))
    # Sensoka went live with no schedule → LATE NOTICE
    b = store.start_session(sen, ts(2026, 11, 19, 21), platforms=["twitch"])
    store.update_session(b, ended_at=ts(2026, 11, 19, 23))
    w = build_week(store, "flavr", start, ts(2026, 11, 21, 23, 30), TZ, recap=True)
    tiles = {t.name: t for t in w.tiles()}
    assert tiles["Lulu"].late is False and tiles["Sensoka"].late is True
    assert w.stream_count == 2 and round(w.hours_live, 2) == 3.92
    # a weekly hand-set time shows on its weekday, and is never "late"
    store.add_manual(pip, 5, 21 * 60, "Friday karaoke")          # Friday 9 PM
    w2 = build_week(store, "friends", start, ts(2026, 11, 17, 12), TZ)
    assert [t.title for t in w2.days[5]] == ["Friday karaoke"]
    store.set_hidden(pip, True)
    assert build_week(store, "friends", start, ts(2026, 11, 17, 12), TZ).tiles() == []


def test_changes_lines():
    store, lulu, pip, sen = setup()
    start = datetime(2026, 11, 15, tzinfo=TZ)
    now = ts(2026, 11, 17, 12)
    before = {t.key: t for t in build_week(store, "flavr", start, now, TZ).tiles()}
    store.replace_scheduled(lulu, "youtube", [{"key": "v", "at": ts(2026, 11, 19, 19)}], now=now)
    sid = store.start_session(sen, ts(2026, 11, 17, 11, 58), platforms=["twitch"])
    mid = {t.key: t for t in build_week(store, "flavr", start, now, TZ).tiles()}
    assert sorted(changes(before, mid, TZ)) == ["Lulu scheduled Thu 7 PM", "Sensoka went live"]
    store.update_session(sid, ended_at=now)
    after = {t.key: t for t in build_week(store, "flavr", start, now, TZ).tiles()}
    assert changes(mid, after, TZ) == ["Sensoka ended"]


def test_embed_text_fits_discord_and_page_draws():
    store, lulu, pip, sen = setup()
    start = datetime(2026, 11, 15, tzinfo=TZ)
    for i in range(40):                                 # a very busy Monday
        p = store.add_person(f"Streamer {i}", "flavr")
        sid = store.start_session(p, ts(2026, 11, 16, 8) + i * 600, platforms=["kick"], title="x" * 90)
        store.update_session(sid, ended_at=ts(2026, 11, 16, 12) + i * 600)
    w = build_week(store, "flavr", start, ts(2026, 11, 17, 12), TZ)
    e = embed(w)
    assert all(len(v) <= 1024 for _, v in e["fields"])
    assert sum(len(n) + len(v) for n, v in e["fields"]) + len(e["title"]) + len(e["footer"]) <= 6000
    assert e["fields"][0][1].endswith("more")
    html = page(w, lambda u: "", logo="")
    assert "+25 more" in html and 'class="day d1 past slim"' in html and "THIS WEEK" in html and "TODAY" in html
    assert "<script" not in html


def test_each_day_fills_its_column():
    """Sen's Nov 8-14 recap: Friday's 9 streams showed 6 + "+3 more" with a third of the column empty."""
    from flavrbot.render import day_layout
    assert day_layout(2) == ("big", 2) and day_layout(4) == ("row", 4) and day_layout(9) == ("row", 9)
    assert day_layout(10) == ("slim", 10) and day_layout(16) == ("slim", 16)
    assert day_layout(17) == ("slim", 15)               # 15 rows + the "+2 more" line


def test_page_escapes_titles():
    store, lulu, pip, sen = setup()
    store.start_session(lulu, ts(2026, 11, 17, 10), platforms=["youtube"], title='<img src=x onerror=alert(1)>')
    w = build_week(store, "flavr", datetime(2026, 11, 15, tzinfo=TZ), ts(2026, 11, 17, 12), TZ)
    assert "<img src=x" not in page(w, lambda u: "")


def test_ics_feed():
    store, lulu, pip, sen = setup()
    sid = store.start_session(lulu, ts(2026, 11, 16, 20), platforms=["youtube"], title="Hi, there; you",
                              links={"youtube": "https://www.youtube.com/watch?v=abc"})
    store.update_session(sid, ended_at=ts(2026, 11, 16, 22), vod_url="https://www.youtube.com/watch?v=abc")
    w = build_week(store, "flavr", datetime(2026, 11, 15, tzinfo=TZ), ts(2026, 11, 17, 12), TZ)
    text = ics([w], ts(2026, 11, 17, 12))
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.count("BEGIN:VEVENT") == 1
    assert r"SUMMARY:Lulu: Hi\, there\; you" in text and "DTSTART:20261117T010000Z" in text


class FakeDiscord:
    def __init__(self):
        self.sent, self.edits, self.dms, self.gone = [], [], [], False

    async def cal_send(self, channel, data, png, name):
        self.sent.append((channel, data["title"], name))
        return str(100 + len(self.sent))

    async def cal_edit(self, channel, message, data, png, name):
        if self.gone:
            return False
        self.edits.append((message, data["title"]))
        return True

    async def dm_owner(self, text, png=None):
        self.dms.append(text)


async def test_poster_rhythm(tmp_path):
    store, lulu, pip, sen = setup()
    clock = {"t": ts(2026, 11, 17, 12)}                 # Tuesday noon
    d = FakeDiscord()
    p = CalendarPoster(store, d, ImageCache(tmp_path), None, ping=lambda: True, clock=lambda: clock["t"])
    await p.tick()
    assert d.sent == []                                  # no channel yet: nothing
    store.put("calendar_channel.flavr", "55")
    await p.tick()
    assert d.sent == [("55", "This week in FlaVR · Nov 15 – 21", "calendar-2026-11-15.png")]
    await p.tick()
    assert d.edits == []                                 # nothing changed: no edit
    store.replace_scheduled(lulu, "youtube", [{"key": "v", "at": ts(2026, 11, 19, 19)}], now=clock["t"])
    await p.tick()
    assert len(d.edits) == 1
    clock["t"] += 11 * 60
    await p.tick()
    assert d.dms == ["🗓️ Calendar updated: Lulu scheduled Thu 7 PM"]
    # Saturday 11 PM: the recap, once
    clock["t"] = ts(2026, 11, 21, 23, 5)
    await p.tick()
    await p.tick()
    assert [s[1] for s in d.sent].count("FlaVR: week in review · Nov 15 – 21") == 1
    # Sunday 9 AM: still last week's post; 10 AM: a new post for the new week
    clock["t"] = ts(2026, 11, 22, 9)
    await p.tick()
    assert d.sent[-1][1].startswith("FlaVR: week in review")
    clock["t"] = ts(2026, 11, 22, 10, 1)
    await p.tick()
    assert d.sent[-1][1] == "This week in FlaVR · Nov 22 – 28"
    assert store.get("calendar_post.flavr")["week"] == "2026-11-22"
    # the post was deleted: the next change makes a new one
    d.gone = True
    store.replace_scheduled(lulu, "youtube", [{"key": "w", "at": ts(2026, 11, 26, 19)}], now=clock["t"])
    await p.tick()
    d.gone = False
    await p.tick()
    assert d.sent[-1][1] == "This week in FlaVR · Nov 22 – 28" and len([s for s in d.sent if "Nov 22" in s[1]]) == 2


async def test_find_vod_youtube_and_kick():
    from flavrbot.watcher import Watcher
    store, lulu, pip, sen = setup()
    w = Watcher(store, {}, {})
    person = store.person("Lulu")
    url, thumb = await w.find_vod(person, {"links": json.dumps({"youtube": "https://www.youtube.com/watch?v=abc12345678"}),
                                           "started_at": 1.0})
    assert url.endswith("abc12345678") and thumb == "https://i.ytimg.com/vi/abc12345678/hqdefault.jpg"
    url, thumb = await w.find_vod(person, {"links": {"kick": "https://kick.com/lulu"}, "started_at": 1.0})
    assert url == "https://kick.com/lulu/videos" and thumb == ""


async def test_web_pages(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from flavrbot.core_link import CoreLink
    from flavrbot.web import add_routes
    store, lulu, pip, sen = setup()
    store.start_session(lulu, ts(2026, 11, 17, 10), platforms=["youtube"], title="Live now")
    poster = CalendarPoster(store, FakeDiscord(), ImageCache(tmp_path), None, clock=lambda: ts(2026, 11, 17, 12))
    link = CoreLink(store, "k")
    link.extra_routes.append(lambda app: add_routes(app, poster))
    async with TestClient(TestServer(link.app())) as client:
        r = await client.get("/calendar")
        body = await r.text()
        assert r.status == 200 and "Live now" in body and 'http-equiv="refresh"' in body
        r = await client.get("/calendar.ics?group=friends")
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/calendar")
        assert (await client.get("/calendar.png")).status == 503          # no Chromium in tests
        assert (await client.get("/calendar/img/..%2Fsecret")).status == 404
        assert (await client.get("/health")).status == 200
