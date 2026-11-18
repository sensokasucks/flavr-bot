"""Server care starter pack (flavrbot/server_care.py): the scam link check, the repeat-spam watch,
the server layout backup and its change list, and /setup check's list. No Discord connection."""

import json
from types import SimpleNamespace as NS

import discord

from flavrbot import config
from flavrbot.server_care import (RepeatWatch, layout_changes, lookalike, missing, save_backup, scam_reason,
                                  snapshot)


def test_fake_sites_are_caught():
    for text in ("free nitro https://dlscord-gift.com/claim", "https://discord-nitro.ru/x",
                 "https://steamcommunlty.com/tradeoffer/new", "https://d1scord.gift/abc",
                 "https://free-nitro-gift.xyz", "https://discorb.com/invite/x", "https://xn--dscord-gva.com/x"):
        assert scam_reason(text), text


def test_real_and_ordinary_sites_pass():
    for text in ("https://discord.com/channels/1/2", "https://discord.gg/flavr", "https://www.youtube.com/watch?v=x",
                 "https://twitch.tv/sensoka", "https://steamcommunity.com/id/me", "https://store.steampowered.com/app/1",
                 "https://steamdb.info/app/1", "https://www.nitrotype.com", "https://media.discordapp.net/a.png",
                 "look at this https://example.com/cat.png", "no link at all, free nitro lol"):
        assert scam_reason(text) == "", text


def test_masked_link_that_lies_about_where_it_goes():
    assert "says discord.com" in scam_reason("[https://discord.com/gift/abc](https://evil.example/claim)")
    assert scam_reason("[my clip](https://youtu.be/abc)") == ""
    assert scam_reason("[discord.com](https://discord.com/x)") == ""


def test_bait_text_with_an_unknown_link():
    assert "giveaway" in scam_reason("@everyone claim your gift https://get-reward.example")
    assert scam_reason("@everyone new video https://youtu.be/abc") == ""     # a real site is fine


def test_own_block_and_allow_lists():
    assert "blocked" in scam_reason("https://sketchy.example/x", blocked={"sketchy.example"})
    assert scam_reason("https://discordbotlist.example", allowed={"discordbotlist.example"}) == ""
    assert lookalike("discordbotlist.example")      # without the allow list it would be caught


def test_same_link_in_three_channels_is_spam():
    w = RepeatWatch()
    assert w.seen(1, "x.example", 10, 100, now=0) is None
    assert w.seen(1, "x.example", 10, 101, now=5) is None          # same channel again: still fine
    assert w.seen(1, "x.example", 11, 102, now=10) is None
    hit = w.seen(1, "x.example", 12, 103, now=20)
    assert hit and {c for c, _ in hit} == {10, 11, 12}
    assert w.seen(2, "y.example", 10, 1, now=0) is None and w.seen(2, "y.example", 11, 2, now=500) is None
    assert w.seen(2, "y.example", 12, 3, now=1000) is None         # spread out over 15 minutes: fine


def fake_guild(channels, roles):
    default = NS(id=1, name="@everyone", colour=discord.Colour(0), permissions=discord.Permissions(0), position=0,
                 hoist=False, mentionable=False, managed=False, members=[], is_default=lambda: True)
    rs = [default] + [NS(id=i, name=n, colour=discord.Colour(0xF72CA2), permissions=discord.Permissions(8), position=p,
                         hoist=True, mentionable=False, managed=False, members=[NS(id=500)], is_default=lambda: False)
                      for p, (i, n) in enumerate(roles, 1)]
    cs = [NS(id=i, name=n, type="text", category_id=None, position=p, topic="t", nsfw=False, slowmode_delay=0,
             overwrites={}) for p, (i, n) in enumerate(channels)]
    return NS(id=9, name="FlaVR", roles=rs, channels=cs, emojis=[NS(id=7, name="flavr", animated=False)],
              verification_level="low", explicit_content_filter="all_members", afk_channel=None, afk_timeout=300,
              system_channel=None, rules_channel=None)


def test_snapshot_and_changes(tmp_path):
    old = snapshot(fake_guild([(20, "general"), (21, "clips")], [(30, "Mods")]), now=0)
    assert json.loads(json.dumps(old))["roles"][0]["name"] == "Mods"         # highest role first, JSON-able
    assert old["roles"][0]["members"] == ["500"] and old["roles"][-1]["members"] == []
    new = snapshot(fake_guild([(20, "chat"), (22, "art")], [(30, "Mods"), (31, "Artists")]), now=86400)
    lines = layout_changes(old, new)
    assert "➕ channel #art" in lines and "➖ channel #clips" in lines and "✏️ channel #general → #chat" in lines
    assert "➕ role @Artists" in lines
    assert layout_changes(None, new) == []                                    # first backup: nothing to compare
    for day in range(1, 6):
        save_backup(tmp_path, new, f"2026-11-0{day}", keep=3)
    assert [p.name for p in sorted(tmp_path.glob("server-*.json"))] == [
        "server-2026-11-03.json", "server-2026-11-04.json", "server-2026-11-05.json"]


def test_setup_check_lists_what_is_missing():
    perms = discord.Permissions(manage_roles=True, manage_messages=True)
    me = NS(guild_permissions=perms)
    chan = NS(name="mod-log", permissions_for=lambda m: discord.Permissions(send_messages=True, embed_links=True))
    role = NS(name="Artists", is_assignable=lambda: False)
    out = " ".join(missing(None, me, {"members": True, "content": False}, mod_log=chan, panel=[role]))
    assert "Message Content" in out and "Server Members" not in out
    assert "Timeout Members" in out and "View Audit Log" in out and "Manage Roles" not in out
    assert "Attach Files" in out and "above **Artists**" in out


def test_settings():
    s = config.load({})
    assert (s.scam_timeout_min, s.backup_hour, s.backup_keep) == (60, 4, 30)
    s = config.load({"SCAM_TIMEOUT_MIN": "0", "BACKUP_HOUR": "27", "BACKUP_KEEP": "0"})
    assert (s.scam_timeout_min, s.backup_hour, s.backup_keep) == (0, 3, 1)
