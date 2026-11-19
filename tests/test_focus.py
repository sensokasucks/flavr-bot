"""Focus posts: just today / tomorrow / the weekend (flavrbot/focus.py). No Discord, no Chromium."""

from datetime import date

from flavrbot import focus
from flavrbot.calendar_post import CalendarPoster
from flavrbot.render import ImageCache, tiles_card

from test_calendar import TZ, FakeDiscord, setup, ts


def test_which_days():
    thu = ts(2026, 11, 19, 17)                       # Thursday
    assert focus.pick_days("today", thu, TZ) == ("Today", [date(2026, 11, 19)])
    assert focus.pick_days("tomorrow", thu, TZ) == ("Tomorrow", [date(2026, 11, 20)])
    # the coming weekend; Saturday and Sunday sit in two different board weeks
    assert focus.pick_days("weekend", thu, TZ) == ("This weekend", [date(2026, 11, 21), date(2026, 11, 22)])
    assert focus.pick_days("weekend", ts(2026, 11, 21, 9), TZ)[1] == [date(2026, 11, 21), date(2026, 11, 22)]
    assert focus.pick_days("weekend", ts(2026, 11, 22, 9), TZ)[1] == [date(2026, 11, 21), date(2026, 11, 22)]
    assert focus.pick_days("monday", thu, TZ) == ("Monday", [date(2026, 11, 23)])
    assert focus.pick_days("thursday", thu, TZ) == ("Today", [date(2026, 11, 19)])


def test_focus_builds_from_the_board_across_weeks(tmp_path):
    store, lulu, pip, sen = setup()
    p = CalendarPoster(store, FakeDiscord(), ImageCache(tmp_path), None, clock=lambda: ts(2026, 11, 19, 17))
    store.add_manual(lulu, 6, 19 * 60, "Sat stream")         # Saturday 7 PM (this board week)
    store.add_manual(sen, 0, 15 * 60, "Sun stream")          # Sunday 3 PM (next board week)
    store.add_manual(sen, 4, 20 * 60, "Thu stream")
    f = focus.build(p, "flavr", *focus.pick_days("weekend", p.clock(), TZ))
    assert [[t.name for t in d] for d in f.days] == [["Lulu"], ["Sensoka"]]
    data = focus.embed(f)
    assert data["title"] == "This weekend on FlaVR · Nov 21–22"
    assert [n for n, _ in data["fields"]] == ["Saturday Nov 21", "Sunday Nov 22"]
    assert "Sat stream" in data["fields"][0][1] and "Thu stream" not in str(data)
    # a talent channel: only that person
    only = focus.build(p, "flavr", "This weekend", f.dates, person_id=lulu)
    assert only.tiles()[0].name == "Lulu" and len(only.tiles()) == 1
    assert focus.embed(only, "Lulu")["title"] == "Lulu: this weekend · Nov 21–22"
    # an empty day says so
    empty = focus.build(p, "flavr", *focus.pick_days("monday", p.clock(), TZ))
    assert focus.embed(empty)["description"] == "Nothing on the calendar yet." and focus.embed(empty)["fields"] == []
    # the picture: weekday in front of each time when it covers more than one day
    html = tiles_card("flavr", "THIS WEEKEND", f.tiles(), TZ, lambda u: "", show_day=True)
    assert "Sat 7 PM" in html and "Sun 3 PM" in html


async def test_automatic_day_posts(tmp_path):
    store, lulu, pip, sen = setup()
    clock = {"t": ts(2026, 11, 19, 9)}                        # Thursday 9 AM
    d = FakeDiscord()
    p = CalendarPoster(store, d, ImageCache(tmp_path), None, clock=lambda: clock["t"])
    store.add_manual(lulu, 4, 19 * 60, "Thu stream")
    store.add_manual(lulu, 6, 19 * 60, "Sat stream")
    await p.tick()
    assert d.sent == []                                       # off until /setup day-posts
    store.put("day_posts.flavr", {"channel": "77", "hour": 10, "daily": True, "weekend": True})
    await p.tick()
    assert d.sent == []                                       # before 10 AM
    clock["t"] = ts(2026, 11, 19, 10, 1)
    await p.tick()
    await p.tick()
    assert d.sent == [("77", "Today on FlaVR · Thu Nov 19", "daily-2026-11-19.png")]   # once
    # she goes live: the post is edited in place
    s = store.start_session(lulu, ts(2026, 11, 19, 19, 2), platforms=["youtube"], title="Cozy", url="https://youtu.be/x")
    clock["t"] = ts(2026, 11, 19, 19, 5)
    await p.tick()
    assert d.edits == [("101", "Today on FlaVR · Thu Nov 19")]
    store.update_session(s, ended_at=ts(2026, 11, 19, 21), vod_url="https://youtu.be/x")
    clock["t"] = ts(2026, 11, 20, 0, 5)                       # past midnight: one last look, then left alone
    await p.tick()
    assert len(d.edits) == 2
    assert store.get("day_post.flavr.daily")["message"] == ""
    # Friday: no Today post (nothing on), but the weekend one
    clock["t"] = ts(2026, 11, 20, 10, 1)
    await p.tick()
    assert d.sent[1:] == [("77", "This weekend on FlaVR · Nov 21–22", "weekend-2026-11-21.png")]
    await p.tick()
    assert len(d.sent) == 2
