"""Phone pings (flavrbot/pings.py): switches, thresholds, quiet hours, urgent, friends batching."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flavrbot.pings import Pinger, money_text
from flavrbot.store import Store

TZ = ZoneInfo("America/New_York")


def at(hour):
    return datetime(2026, 11, 17, hour, 30, tzinfo=TZ).timestamp()


def make(hour=14, live=False):
    sent = []
    clock = {"t": at(hour)}

    async def send(text):
        sent.append(text)

    p = Pinger(Store(":memory:"), send, quiet=(3, 11), live=lambda: live, clock=lambda: clock["t"])
    return p, sent, clock


def test_money_thresholds():
    assert money_text({"kind": "gift", "qty": 10, "display_name": "Amy", "platform": "twitch"}, 20, 10)
    assert money_text({"kind": "gift", "qty": 5}, 20, 10) is None
    assert money_text({"kind": "member_gift", "qty": 20}, 20, 10)
    assert money_text({"kind": "superchat", "amount": 25, "currency": "USD"}, 20, 10).startswith("💸")
    assert money_text({"kind": "superchat", "amount": 5, "currency": "USD"}, 20, 10) is None
    assert money_text({"kind": "bits", "amount": 1000, "currency": "bits"}, 20, 10) is None
    assert money_text({"kind": "bits", "amount": 2000, "currency": "bits"}, 20, 10)
    assert money_text({"kind": "kicks", "amount": 5000, "currency": "KICKs"}, 20, 10)
    assert money_text({"kind": "subscribe"}, 20, 10) is None


async def test_switches_and_events():
    p, sent, _ = make()
    await p.on_alert({"kind": "raid", "display_name": "Bo", "viewers": 40, "platform": "twitch"})
    await p.on_stream({"event": "started", "platforms": ["twitch", "youtube"]})
    await p.on_stream({"event": "ended", "length_sec": 7380})
    assert "Bo raided with 40" in sent[0]
    assert "Twitch, Youtube" in sent[1]
    assert "2 h 3 min" in sent[2]
    p.set("raid", False)
    await p.on_alert({"kind": "raid", "display_name": "Cy"})
    assert len(sent) == 3


async def test_quiet_hours_hold_ordinary_but_not_urgent():
    p, sent, clock = make(hour=4)
    assert p.quiet_now()
    await p.on_alert({"kind": "raid", "display_name": "Bo", "viewers": 3, "platform": "kick"})
    await p.core_lost({"silent_sec": 95})
    assert len(sent) == 1 and sent[0].startswith("🚨")
    await p.flush()
    assert len(sent) == 1                     # still quiet
    clock["t"] = at(11)
    await p.flush()
    assert sent[1].startswith("While you were away") and "Bo raided" in sent[1]


async def test_quiet_hours_off_while_live():
    p, sent, _ = make(hour=4, live=True)
    assert not p.quiet_now()


async def test_friends_batched():
    p, sent, clock = make()
    p.friend_live("Lulu", "YouTube")          # off by default
    assert p._friends == []
    p.set("friends", True)
    p.friend_live("Lulu", "YouTube")
    p.friend_live("Raimi", "Twitch")
    await p.flush()
    assert sent == ["🟣 Live now: Lulu (YouTube), Raimi (Twitch)"]
    p.friend_live("Choco", "Kick")
    await p.flush()                           # within 10 minutes: waits
    assert len(sent) == 1
    clock["t"] += 601
    await p.flush()
    assert sent[-1] == "🟣 Live now: Choco (Kick)"


def test_quiet_hours_wrap_and_saved():
    p, _, clock = make(hour=23)
    p.set_quiet(22, 6)
    assert p.quiet_now()
    clock["t"] = at(7)
    assert not p.quiet_now()
    p2 = Pinger(p.store, None)
    p2.load_quiet()
    assert p2.quiet == (22, 6)
