"""Server care round 2 (flavrbot/community.py): the pure parts, plus live mode and the weekly growth
count against fake Discord objects. No Discord connection."""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

from flavrbot.community import (Community, RaidWatch, active_warnings, anniversaries, birthday_today,
                                parse_birthday, warn_triggers_timeout, week_key, welcome_text)
from flavrbot.store import Store


def test_welcome_text_fills_in_and_links():
    t = welcome_text("", "<@5>", "Lulu", "FlaVR", 120, rules="<#1>", calendar="<#2>")
    assert t == "Welcome <@5> to **FlaVR**! You're member #120.\n📜 Rules: <#1> · 🗓️ Stream calendar: <#2>"
    assert welcome_text("Hi {name}!", "<@5>", "Lulu", "FlaVR", 3) == "Hi Lulu!"


def test_raid_watch_alarms_once_per_burst():
    r = RaidWatch(limit=3, window=60)
    assert [r.join(1, t) for t in (0, 10, 20)] == [False, False, True]
    assert r.join(1, 21) is False                       # counting starts over after an alarm
    assert [r.join(2, t) for t in (0, 70, 140)] == [False, False, False]   # spread out: no raid


def test_warnings_count_and_timeout_every_third():
    now = 100 * 86400.0
    rows = [{"at": now - 40 * 86400}, {"at": now - 5 * 86400}, {"at": now - 60}]
    assert len(active_warnings(rows, now, 30)) == 2
    assert [warn_triggers_timeout(n, 3) for n in (1, 2, 3, 4, 6)] == [False, False, True, False, True]
    assert warn_triggers_timeout(3, 0) is False         # WARN_LIMIT=0: never


def test_weeks_birthdays_and_anniversaries():
    assert week_key(date(2026, 11, 18)) == "2026-11-15" and week_key(date(2026, 11, 15)) == "2026-11-15"
    assert birthday_today("11-18", date(2026, 11, 18)) and not birthday_today("11-19", date(2026, 11, 18))
    assert birthday_today("02-29", date(2027, 2, 28)) and not birthday_today("02-29", date(2028, 2, 28))
    assert parse_birthday(2, 29) == "02-29" and parse_birthday(2, 30) == "" and parse_birthday(4, 31) == ""
    joined = [(1, datetime(2024, 11, 18, tzinfo=timezone.utc)), (2, datetime(2026, 11, 18, tzinfo=timezone.utc)),
              (3, datetime(2025, 11, 17, tzinfo=timezone.utc)), (4, None)]
    assert anniversaries(joined, date(2026, 11, 18)) == [(1, 2)]


class FakeChannel:
    def __init__(self, cid, name="chat", slow=0):
        self.id, self.name, self.slowmode_delay = cid, name, slow

    async def edit(self, **kw):
        if "slowmode_delay" in kw:
            self.slowmode_delay = kw["slowmode_delay"]
        if "name" in kw:
            self.name = kw["name"]


def test_live_mode_slows_and_renames_then_puts_back():
    chat, vc = FakeChannel(10, slow=2), FakeChannel(20, name="Hangout")
    guild = NS(id=1, get_channel=lambda i: {10: chat, 20: vc}.get(i))
    c = Community(NS(guilds=[guild]), Store(":memory:"))
    c.put("live_mode", 1, {"channels": ["10"], "slowmode": 15, "voice": "20"})
    asyncio.run(c.live_mode(True, "Schedule I"))
    assert chat.slowmode_delay == 15 and vc.name == "🔴 LIVE: Schedule I"
    asyncio.run(c.live_mode(True, "Schedule I"))       # told twice (watcher + Core): no change
    asyncio.run(c.live_mode(True, "Valheim"))          # new game: the name follows
    assert vc.name == "🔴 LIVE: Valheim"
    asyncio.run(c.live_mode(False))
    assert chat.slowmode_delay == 2 and vc.name == "Hangout"


def test_growth_counts_joins_and_leaves_per_week():
    c = Community(NS(guilds=[]), Store(":memory:"))
    for what in ("joined", "joined", "left"):
        c._count(1, what)
    lines = c.growth_lines(NS(id=1), weeks=2)
    assert lines[0] == "This week: +2 joined, -1 left (+1)" and lines[1].endswith("+0 joined, -0 left (+0)")


def test_lockdown_slows_open_channels_and_undoes_it():
    open_ch, mods_ch, slow_ch = FakeChannel(1, slow=0), FakeChannel(2, slow=0), FakeChannel(3, slow=120)
    can = {1: True, 2: False, 3: True}
    for ch in (open_ch, mods_ch, slow_ch):
        ch.permissions_for = (lambda cid: lambda role: NS(send_messages=can[cid]))(ch.id)
    edits, logs, dms = [], [], []

    async def gedit(**kw):
        edits.append(kw)

    async def post(*a, **kw):
        logs.append(a[2])

    async def dm(text):
        dms.append(text)

    guild = NS(id=7, name="FlaVR", default_role=object(), text_channels=[open_ch, mods_ch, slow_ch], features=[],
               edit=gedit, get_channel=lambda i: {1: open_ch, 2: mods_ch, 3: slow_ch}.get(i))
    c = Community(NS(guilds=[guild], care=NS(post=post), dm_owner=dm), Store(":memory:"), raid_joins=2)
    asyncio.run(c.on_member_join(NS(guild=guild, display_name="x")))      # 1 join: nothing
    assert open_ch.slowmode_delay == 0
    c.welcome = lambda m: asyncio.sleep(0)
    asyncio.run(c.on_member_join(NS(guild=guild)))                         # 2nd: a raid
    assert (open_ch.slowmode_delay, mods_ch.slowmode_delay, slow_ch.slowmode_delay) == (30, 0, 120)
    assert edits == [{"invites_disabled": True, "reason": "FlaVR Bot lockdown"}] and len(dms) == 1
    assert logs == ["🔒 Lockdown on"]
    asyncio.run(c.lockdown(guild, False, by="Sen"))
    assert open_ch.slowmode_delay == 0 and edits[-1]["invites_disabled"] is False and logs[-1] == "🔓 Lockdown off"
