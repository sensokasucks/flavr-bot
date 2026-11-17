"""Filling in weeks the bot wasn't watching (Watcher.backfill, CalendarPoster.ensure_backfill) and
the platform parsers behind it (Twitch past broadcasts, YouTube uploads + real start / end times)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flavrbot.calendar import build_week
from flavrbot.calendar_post import CalendarPoster
from flavrbot.platforms.twitch import Twitch, duration_sec
from flavrbot.platforms.youtube import YouTube
from flavrbot.store import Store
from flavrbot.watcher import Watcher

TZ = ZoneInfo("America/New_York")
SUN = datetime(2026, 11, 8, tzinfo=TZ)          # Nov 8-14, the empty week in review
A = SUN.timestamp()
NOW = A + 9 * 86400                              # Tuesday of the next week


class Past:
    ready = True

    def __init__(self, rows):
        self.rows = rows                         # platform_id → [{started_at, ended_at, title, url, thumb}]
        self.asked = 0

    async def past_streams(self, pid, since, until):
        self.asked += 1
        return [r for r in self.rows.get(pid, []) if since <= r["started_at"] < until]


def vod(start_h, hours, url, title="Stream"):
    s = A + start_h * 3600
    return {"started_at": s, "ended_at": s + hours * 3600, "title": title, "url": url, "thumb": url + ".jpg"}


def world(yt_rows, tw_rows):
    store = Store(":memory:")
    amanda = store.add_person("Amanda", "flavr")
    store.add_account(amanda, "youtube", "@amanda")
    store.add_account(amanda, "twitch", "amanda")
    for a in store.people()[0]["accounts"]:
        store.set_account_info(a["id"], "UCamanda" if a["platform"] == "youtube" else "111")
    ch = {"youtube": Past({"UCamanda": yt_rows}), "twitch": Past({"111": tw_rows})}
    return store, ch, Watcher(store, ch, {}, clock=lambda: NOW)


async def test_backfill_fills_an_empty_week_and_merges_multistreams():
    store, ch, w = world([vod(20, 3, "yt1"), vod(72, 2, "yt2")], [vod(20.2, 3.1, "tw1")])
    assert await w.backfill(A, A + 7 * 86400) == 2
    week = build_week(store, "flavr", SUN, NOW, TZ, recap=True)
    tiles = week.tiles()
    assert len(tiles) == 2
    first = tiles[0]
    assert first.kind == "past" and sorted(first.platforms) == ["twitch", "youtube"]
    assert first.url == "yt1"                    # main platform YouTube leads
    assert first.end == A + 23.3 * 3600         # the later of the two ends (Twitch 20.2 h + 3.1 h)
    assert not first.late                        # filled-in streams never get LATE NOTICE
    # a second run adds nothing (it overlaps what is now saved)
    assert await w.backfill(A, A + 7 * 86400) == 0


async def test_backfill_skips_what_the_watcher_saw_and_streams_still_running():
    store, ch, w = world([vod(20, 3, "yt1")], [])
    pid = store.people()[0]["id"]
    store.start_session(pid, A + 20.1 * 3600, ended_at=A + 23 * 3600, title="seen live")
    # ended 5 minutes ago: may still be live, the watcher owns it
    ch["youtube"].rows["UCamanda"].append({"started_at": NOW - 3600, "ended_at": NOW - 300, "title": "now",
                                           "url": "yt9", "thumb": ""})
    assert await w.backfill(A, NOW) == 0
    assert [s["title"] for s in store.sessions_between(A, NOW)] == ["seen live"]


async def test_ensure_backfill_runs_once_per_finished_week():
    store, ch, w = world([vod(20, 3, "yt1")], [])
    poster = CalendarPoster(store, None, None, None, clock=lambda: NOW, backfill=w.backfill)
    assert await poster.ensure_backfill(SUN) == 1
    asked = ch["youtube"].asked
    assert await poster.ensure_backfill(SUN) == 0 and ch["youtube"].asked == asked     # not asked again
    await poster.ensure_backfill(SUN, force=True)                                     # /calendar refresh
    assert ch["youtube"].asked == asked + 1


def test_twitch_duration():
    assert duration_sec("3h2m1s") == 3 * 3600 + 121
    assert duration_sec("45m") == 2700 and duration_sec("12s") == 12 and duration_sec("") == 0


class FakeHttp:
    """Answers aiohttp-style GETs from a list of (path piece, json) pairs."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        body = next(j for piece, j in self.answers if piece in url)

        class R:
            status = 200

            async def json(self, content_type=None):
                return body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False
        return R()


async def test_twitch_past_streams_parses_archive():
    http = FakeHttp([("/videos", {"data": [
        {"created_at": "2026-11-09T01:00:00Z", "duration": "2h30m0s", "title": "Hi", "url": "https://twitch.tv/videos/1",
         "thumbnail_url": "https://x/%{width}x%{height}.jpg"},
        {"created_at": "2026-10-01T01:00:00Z", "duration": "1h", "title": "old", "url": "o", "thumbnail_url": ""}]})])
    t = Twitch(http, "id", "secret")
    t._token, t._token_until = "tok", 1e12
    rows = await t.past_streams("111", A, A + 7 * 86400)
    assert len(rows) == 1 and rows[0]["ended_at"] - rows[0]["started_at"] == 9000
    assert rows[0]["thumb"] == "https://x/640x360.jpg"


async def test_youtube_past_streams_uses_real_start_and_end():
    http = FakeHttp([("/playlistItems", {"items": [{"contentDetails": {"videoId": "a", "videoPublishedAt": "2026-11-09T01:00:00Z"}},
                                                   {"contentDetails": {"videoId": "b", "videoPublishedAt": "2026-11-10T01:00:00Z"}}]}),
                     ("/videos", {"items": [
                         {"id": "a", "snippet": {"title": "Stream"}, "liveStreamingDetails": {
                             "actualStartTime": "2026-11-09T01:00:00Z", "actualEndTime": "2026-11-09T04:00:00Z"}},
                         {"id": "b", "snippet": {"title": "Upload"}}]})])
    y = YouTube(http, "key")
    rows = await y.past_streams("UC" + "x" * 22, A, A + 7 * 86400)
    assert [r["url"] for r in rows] == ["https://www.youtube.com/watch?v=a"]
    assert rows[0]["ended_at"] - rows[0]["started_at"] == 3 * 3600
    assert http.calls[0][1]["playlistId"] == "UU" + "x" * 22
