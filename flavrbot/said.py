"""
/said: search past stream chat from Discord ("who said what, when"). Sen asked 2026-11-18.

The lines come from Stream Core's own saved chat (Core: Chat history; only lines Core saves, so
its chat saving must be on). Core sends them over the companion link ("chat" messages, see
docs/CORE_LINK.md) and flavrbot/core_link.py keeps them in the bot's database (store.py, the chat
table and its chat_fts word index). Clearing chat in Core clears this copy too.

  /said [words] [who] [platform] [days] [page]
      words     every word must be in the line, any order ("pizza night" finds "night of pizza")
      who       a chatter's username or display name (exact, any case, @ optional)
      platform  Twitch / YouTube / Kick
      days      only the last N days (default: all the copy has)
      page      PAGE_SIZE lines a page, newest first

Moderators only (owner, Manage Server or Manage Messages): chat history is about viewers, so it
is never shown to everyone. Answers are "only you can see this", with allowed_mentions none and
link previews off, so a searched line can't ping anyone or unfurl a link.

Who calls it: flavrbot/bot.py _register → ``register(tree, bot)``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import discord
from discord import app_commands

PAGE_SIZE = 10
LINE_MAX = 300                    # characters of one chat line shown (Discord answers stop at 2000)
PLATFORM_NAMES = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}


def is_mod(bot: Any, inter: discord.Interaction) -> bool:
    perms = getattr(inter.user, "guild_permissions", None)
    return bot.may_manage(inter) or bool(perms and perms.manage_messages)


def format_rows(result: Dict[str, Any], page: int, size: int = PAGE_SIZE) -> str:
    """The answer text: one line per chat line, newest first, then where you are in the results.
    <t:…:f> shows each reader's own time zone; the text is escaped so it reads as typed."""
    rows, total = result["rows"], result["total"]
    if not rows:
        return "No chat lines match." if total == 0 else f"No page {page} (there are {(total + size - 1) // size})."
    out = []
    for r in rows:
        name = discord.utils.escape_markdown(r.get("display_name") or r.get("username") or "?")
        text = discord.utils.escape_markdown(" ".join((r.get("message") or "").split()))
        if len(text) > LINE_MAX:
            text = text[: LINE_MAX - 1] + "…"
        plat = PLATFORM_NAMES.get(r.get("platform") or "", r.get("platform") or "")
        out.append(f"<t:{int(r['at'])}:f> **{name}**" + (f" ({plat})" if plat else "") + f": {text}")
    pages = (total + size - 1) // size
    out.append(f"-# {total} line{'s' if total != 1 else ''} · page {page} of {pages}"
               + (" · next: same search with page:" + str(page + 1) if page < pages else ""))
    text = "\n".join(out)
    while len(text) > 1990 and len(out) > 2:          # long lines: drop from the oldest end
        out.pop(-2)
        text = "\n".join(out)
    return text


def register(tree: app_commands.CommandTree, bot: Any) -> None:
    """Adds /said (see the module notes)."""
    plat_choices = [app_commands.Choice(name=v, value=k) for k, v in PLATFORM_NAMES.items()]

    @tree.command(name="said", description="Search past stream chat: who said what, when (moderators)")
    @app_commands.choices(platform=plat_choices)
    @app_commands.describe(words="All of these words, any order", who="Chatter's name (username or display name)",
                           days="Only the last N days", page="Which page of results (10 a page)")
    async def said(inter: discord.Interaction, words: Optional[str] = None, who: Optional[str] = None,
                   platform: Optional[app_commands.Choice[str]] = None,
                   days: Optional[app_commands.Range[int, 1, 3650]] = None,
                   page: Optional[app_commands.Range[int, 1, 1000]] = None):
        if not is_mod(bot, inter):
            await inter.response.send_message("Only moderators can search chat.", ephemeral=True)
            return
        if not (words or who):
            await inter.response.send_message("Give some words, a name, or both.", ephemeral=True)
            return
        if bot.store.chat_count() == 0:
            await inter.response.send_message(
                "No chat here yet. In Stream Core: Setup → Discord & video requests → Companion bot, tick "
                "**Send chat history**; Core's chat saving (Chat history) must be on too.", ephemeral=True)
            return
        p = page or 1
        since = time.time() - days * 86400 if days else 0
        result = await asyncio.to_thread(bot.store.search_chat, words or "", who or "",
                                         platform.value if platform else "", since, PAGE_SIZE, (p - 1) * PAGE_SIZE)
        await inter.response.send_message(format_rows(result, p), ephemeral=True, suppress_embeds=True,
                                          allowed_mentions=discord.AllowedMentions.none())
