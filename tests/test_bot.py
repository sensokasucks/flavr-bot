"""The Discord client builds and registers every command (no Discord connection needed)."""

from flavrbot import config
from flavrbot.bot import FlavrBot
from flavrbot.core_link import CoreLink
from flavrbot.pings import Pinger
from flavrbot.store import Store
from flavrbot.watcher import Watcher


async def test_commands_register():
    s = config.load({"DISCORD_TOKEN": "t", "OWNER_ID": "1", "FLAVR_CORE_KEY": "k"})
    store = Store(":memory:")
    bot = FlavrBot(s, store, CoreLink(store, "k"), Pinger(store, None), Watcher(store, {}, {}))
    names = sorted(c.name for c in bot.tree.get_commands())
    assert names == ["friend", "live", "pings", "points", "queue", "schedule", "setup", "status", "wake"]
    friend = next(c for c in bot.tree.get_commands() if c.name == "friend")
    assert sorted(c.name for c in friend.commands) == ["add", "import", "list", "main", "remove"]
    # every command's description fits Discord's 100-character limit
    for c in bot.tree.walk_commands():
        assert len(c.description) <= 100, c.name
    await bot.close()
