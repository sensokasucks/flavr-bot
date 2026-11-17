"""Copy-ready calendar text (flavrbot/share.py) and the share DM package (CalendarPoster.share)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flavrbot.calendar import Tile, Week
from flavrbot.calendar_post import CalendarPoster
from flavrbot.share import X_MAX, discord_text, x_len, x_posts
from flavrbot.store import Store

TZ = ZoneInfo("America/New_York")
SUN = datetime(2026, 11, 15, tzinfo=TZ)


def week(n_per_day=1, recap=False):
    days = [[] for _ in range(7)]
    for d in range(7):
        for k in range(n_per_day):
            at = SUN.timestamp() + d * 86400 + (19 + k) * 3600
            days[d].append(Tile(kind="past" if recap else "scheduled", name=f"Streamer{d}{k}", at=at,
                                end=at + 7200 if recap else None, title="Game night", url=f"https://youtu.be/v{d}{k}",
                                person_id=d * 10 + k))
    return Week(group="flavr", start=SUN, tz=TZ, now=SUN.timestamp(), recap=recap, days=days, quiet=[])


def test_discord_text_uses_time_stamps_and_quiet_links():
    t = discord_text(week())
    assert t.startswith("🗓️ **This week in FlaVR** · Nov 15 – 21")
    assert "<t:" in t and "](<https://youtu.be/v00>)" in t and len(t) <= 2000
    big = discord_text(week(n_per_day=12))
    assert len(big) <= 2000 and "more" in big
    assert "hours live" in discord_text(week(recap=True))


def test_x_reply_style_keeps_links_out_of_the_main_post():
    posts = x_posts(week(2), {0: "@lulu"}, "reply")
    assert len(posts) == 2
    assert "https://" not in posts[0] and "Sun 7p @lulu · 8p Streamer01" in posts[0] and "(Nov 15 – 21, EST)" in posts[0]
    assert posts[1].startswith("Links 👇") and "@lulu (Sun 7p): https://youtu.be/v00" in posts[1]


def test_x_one_post_has_everything():
    posts = x_posts(week(3), {}, "one")
    assert len(posts) == 1 and posts[0].count("https://") == 21 and "7p Streamer00 · Game night https://" in posts[0]


def test_x_thread_fits_280():
    posts = x_posts(week(4), {}, "thread")
    assert len(posts) > 1 and all(x_len(p) <= X_MAX for p in posts) and posts[-1].endswith(f"({len(posts)}/{len(posts)})")
    assert x_len("hi https://example.com/very/long/link/indeed") == 3 + 23
    assert x_len("🗓️") >= 2


class FakeDiscord:
    def __init__(self):
        self.sent = []

    async def dm_share(self, messages, png):
        self.sent.append((messages, png))


async def test_share_package_and_default_style():
    store = Store(":memory:")
    pid = store.add_person("Lulu", "flavr")
    store.set_x(pid, "@luluteala")
    d = FakeDiscord()
    poster = CalendarPoster(store, d, None, None, clock=lambda: SUN.timestamp() + 3600)
    w = week()
    w.days[0][0].person_id = pid
    msgs = poster.share_messages(w)
    assert msgs[0].startswith("📋") and msgs[1].startswith("🗓️ **") and "@luluteala" in msgs[2] and msgs[3].startswith("Links")
    store.put("share_x_style", "one")
    assert len(poster.share_messages(w)) == 3
    assert len(poster.share_messages(w, "thread")) >= 3               # an override beats the default
    # automatic shares wait for quiet hours to end, and only with the switch on
    poster.share_on, poster.quiet = (lambda: True), (lambda: True)
    poster._share_later(w, b"png")
    await poster.flush_shares()
    assert d.sent == []
    poster.quiet = lambda: False
    await poster.flush_shares()
    assert len(d.sent) == 1 and d.sent[0][1] == b"png"
