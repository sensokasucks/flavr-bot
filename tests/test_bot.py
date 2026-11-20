"""The Discord client builds and registers every command (no Discord connection needed)."""

from flavrbot import config, groups
from flavrbot.bot import FlavrBot
from flavrbot.core_link import CoreLink
from flavrbot.pings import Pinger
from flavrbot.store import Store
from flavrbot.watcher import Watcher


async def test_commands_register():
    s = config.load({"DISCORD_TOKEN": "t", "OWNER_ID": "1", "FLAVR_CORE_KEY": "k"})
    store = Store(":memory:")
    groups.init(store)
    bot = FlavrBot(s, store, CoreLink(store, "k"), Pinger(store, None), Watcher(store, {}, {}))
    from discord import app_commands
    names = sorted(c.name for c in bot.tree.get_commands() if not isinstance(c, app_commands.ContextMenu))
    # the community calendar's message menu entries (flavrbot/promo.py; Discord allows 5 per app)
    menus = sorted(c.name for c in bot.tree.get_commands() if isinstance(c, app_commands.ContextMenu))
    assert menus == ["Add to community calendar", "Remove from community calendar"]
    community = next(c for c in bot.tree.get_commands() if c.name == "community")
    assert sorted(c.name for c in community.commands) == ["list", "remove", "report-channel", "roles", "schedule"]
    # /calendar view and /calendar post take the same "when" as /calendar focus (Sen 2026-11-20)
    cal = next(c for c in bot.tree.get_commands() if c.name == "calendar")
    for name in ("view", "post", "focus", "share"):
        cmd = next(c for c in cal.commands if c.name == name)
        assert "when" in [p.name for p in cmd.parameters], name
    assert names == ["backup", "birthday", "calendar", "community", "friend", "group", "growth", "live", "live-mode", "lockdown",
                     "me", "mod", "modmail", "pings", "points", "queue", "roles", "said", "schedule", "setup", "status",
                     "talent", "wake", "warn", "warnings", "welcome"]
    me = next(c for c in bot.tree.get_commands() if c.name == "me")
    assert sorted(c.name for c in me.commands) == ["hide", "main", "posts", "schedule", "share", "unschedule", "x"]
    friend = next(c for c in bot.tree.get_commands() if c.name == "friend")
    assert sorted(c.name for c in friend.commands) == ["add", "drop", "edit", "group", "hide", "import", "list", "main",
                                                       "remove", "schedule", "unschedule", "x"]
    group = next(c for c in bot.tree.get_commands() if c.name == "group")
    assert sorted(c.name for c in group.commands) == ["add", "edit", "list", "remove"]
    setup = next(c for c in bot.tree.get_commands() if c.name == "setup")
    assert sorted(c.name for c in setup.commands) == ["calendar-channel", "check", "day-posts", "live-channel", "mod-log",
                                                      "modmail", "moments", "recap-channel", "share-style", "starboard",
                                                      "youtube-check"]
    # without the developer-portal switches the bot asks for neither privileged intent (it would be refused)
    assert not bot.intents.members and not bot.intents.message_content
    # every command's description fits Discord's 100-character limit
    for c in bot.tree.walk_commands():
        assert len(c.description) <= 100, c.name
        for p in getattr(c, "parameters", []):
            assert len(p.description) <= 100, (c.name, p.name)
    await bot.close()


def test_parse_time():
    from flavrbot.bot import parse_time
    assert parse_time("7 PM") == 19 * 60
    assert parse_time("7:30pm") == 19 * 60 + 30
    assert parse_time("19:30") == 19 * 60 + 30
    assert parse_time("12 am") == 0 and parse_time("12 PM") == 12 * 60
    assert parse_time("9") == 9 * 60
    assert parse_time("25:00") is None and parse_time("13 pm") is None and parse_time("soon") is None
