"""The Discord client builds and registers every command (no Discord connection needed)."""

from flavrbot import config
from flavrbot.bot import FlavrBot
from flavrbot.core_link import CoreLink
from flavrbot.pings import Pinger
from flavrbot.promo_link import PromoLink
from flavrbot.store import Store


async def test_commands_register():
    s = config.load({"DISCORD_TOKEN": "t", "OWNER_ID": "1", "FLAVR_CORE_KEY": "k"})
    store = Store(":memory:")
    bot = FlavrBot(s, store, CoreLink(store, "k"), Pinger(store, None), PromoLink(None, ""))
    from discord import app_commands
    names = sorted(c.name for c in bot.tree.get_commands() if not isinstance(c, app_commands.ContextMenu))
    # the community calendar's message menu moved to FlaVR Promo with the split (2026-11-20)
    assert not [c for c in bot.tree.get_commands() if isinstance(c, app_commands.ContextMenu)]
    assert names == ["backup", "birthday", "core", "growth", "live-mode", "lockdown", "mod", "modmail", "pings",
                     "points", "queue", "roles", "said", "setup", "status", "wake", "warn", "warnings", "welcome"]
    setup = next(c for c in bot.tree.get_commands() if c.name == "setup")
    assert sorted(c.name for c in setup.commands) == ["check", "mod-log", "modmail", "moments", "recap-channel",
                                                      "starboard"]
    core = next(c for c in bot.tree.get_commands() if c.name == "core")
    assert [c.name for c in core.commands] == ["done"]
    # without the developer-portal switches the bot asks for neither privileged intent (it would be refused)
    assert not bot.intents.members and not bot.intents.message_content
    # every command's description fits Discord's 100-character limit
    for c in bot.tree.walk_commands():
        assert len(c.description) <= 100, c.name
        for p in getattr(c, "parameters", []):
            assert len(p.description) <= 100, (c.name, p.name)
    await bot.close()
