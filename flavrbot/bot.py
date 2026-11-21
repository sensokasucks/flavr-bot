"""
The Discord side of FlaVR Bot: slash commands, DMs to the owner, server care and the recap posts.

Commands (registered on GUILD_ID at once when it is set, else globally, which Discord can take
up to an hour to show the first time):

  Anyone
    /points [name]      points from Stream Core's last copy (works while the stream PC is off)
    /queue              video requests: now playing, up next, waiting
  Owner (OWNER_ID) or Manage Server
    /setup recap-channel [channel]   where the after-stream recap goes (flavrbot/recap.py; none = DM only)
  Server care (flavrbot/server_care.py; owner or Manage Server)
    /setup mod-log channel    where the mod log goes      /setup check   what the bot still needs
    /roles add role [label] [emoji] | remove role | list | post channel [title] [text]   role buttons
    /mod block domain | unblock domain | allow domain | list    the scam link lists
    /backup now               a fresh backup of the server's layout, sent to you
  Server care round 2 (flavrbot/community.py)
    /welcome setup [channel] [message] [starter_role] [dm] | rules-button channel | off
    /lockdown on|off          raid lockdown by hand (it also switches on by itself on a burst of joins)
    /warn member reason       /warnings member [clear]          (moderators)
    /setup modmail channel    members' DMs to the bot open a thread there; /modmail close inside it
    /live-mode add|remove channel | slowmode seconds | voice [channel] | show   while you're live
    /setup starboard channel [emoji] [count]     /setup moments channel [role]     /birthday set|remove
    /growth                   joins and leaves, the last 4 weeks
  Moderators (owner, Manage Server or Manage Messages)
    /said [words] [who] [platform] [days] [page]   search past stream chat (Core's saved chat; flavrbot/said.py)
  Owner only (answers only you can see)
    /status             Stream Core, the stream, this PC, FlaVR Promo's answer
    /wake               Wake-on-LAN the stream PC
    /core done          my stream is over: no "Stream Core stopped answering" alarm until Core connects again
                        (the alarm also stays quiet by itself when FlaVR Promo sees you offline everywhere)
    /pings view | set kind on | quiet start end          phone ping switches and quiet hours

Moved to FlaVR Promo (sensokasucks/flavr-promo) with the split on 2026-11-20: the go-live watcher
and its cards, /live, /schedule, /friend, /group, /calendar, /talent, /me, /community, the
calendar / day post / review / share settings under /setup, and the community calendar's message
menu. FlaVR Promo is its own Discord app.

Every message sets allowed_mentions to nothing, so a stream title can't ping @everyone.
Commands answer "only you can see this" (ephemeral) unless they are meant for the channel.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Dict, Optional

import discord
from discord import app_commands

from . import sysinfo, wol
from .config import Settings
from .server_care import MAX_ROLE_BUTTONS, ServerCare, missing
from .community import Community, missing_more
from . import community as community_cmds
from . import said
from .core_link import CoreLink
from .pings import KINDS, Pinger, length
from .promo_link import PromoLink
from .store import Store

log = logging.getLogger("flavrbot.bot")
NO_PINGS = discord.AllowedMentions.none()


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    sec = int(time.time() - ts)
    if sec < 90:
        return f"{sec} s ago"
    if sec < 5400:
        return f"{sec // 60} min ago"
    return f"{sec // 3600} h ago"


class FlavrBot(discord.Client):
    def __init__(self, settings: Settings, store: Store, link: CoreLink, pinger: Pinger, promo: PromoLink,
                 gateway: Optional[Dict[str, bool]] = None):
        # ``gateway``: which privileged switches are on in the developer portal (server_care.gateway_intents,
        # read before connecting). Asking for one that's off would make Discord refuse to connect at all.
        gateway = gateway or {"members": False, "content": False}
        intents = discord.Intents.default()
        intents.members = gateway.get("members", False)
        intents.message_content = gateway.get("content", False)
        # max_messages: recent messages kept in memory, so the mod log can show what a deleted message said
        super().__init__(intents=intents, allowed_mentions=NO_PINGS, max_messages=5000)
        self.settings = settings
        self.store = store
        self.link = link
        self.pinger = pinger
        # FlaVR Promo's /live.json (flavrbot/promo_link.py): shown on /status
        self.promo = promo
        self.tree = app_commands.CommandTree(self)
        # set once Discord says we're in; the background jobs wait for it so their first posts don't get lost
        self.ready_event = asyncio.Event()
        # role buttons, mod log, scam links, backups (settings: SCAM_TIMEOUT_MIN, BACKUP_HOUR, BACKUP_KEEP)
        self.care = ServerCare(self, store, settings.data_dir, tz=settings.timezone,
                               scam_timeout_min=settings.scam_timeout_min, backup_hour=settings.backup_hour,
                               backup_keep=settings.backup_keep, gateway=gateway)
        # round 2: welcome, raid lockdown, warnings, modmail, live mode, starboard, member moments, growth
        # (flavrbot/community.py; settings RAID_JOINS ... MOMENTS_HOUR); growth DM through /pings kind "growth"
        self.community = Community(self, store, tz=settings.timezone, raid_joins=settings.raid_joins,
                                   raid_window=settings.raid_window_sec, warn_limit=settings.warn_limit,
                                   warn_days=settings.warn_days, warn_timeout_min=settings.warn_timeout_min,
                                   moments_hour=settings.moments_hour, ping=pinger.ping)
        self._register()

    # ── helpers ────────────────────────────────────────────────────────
    async def dm_owner(self, text: str, png: Optional[bytes] = None) -> None:
        """flavrbot/pings.py's ``send``: one DM to OWNER_ID (with a picture when ``png`` is given)."""
        user = self.get_user(self.settings.owner_id) or await self.fetch_user(self.settings.owner_id)
        files = [discord.File(io.BytesIO(png), filename="calendar.png")] if png else []
        await user.send(text, files=files, allowed_mentions=NO_PINGS)

    async def post_text(self, channel_id: str, text: str) -> None:
        """Plain text into a channel: the after-stream recap (flavrbot/recap.py, /setup recap-channel).
        No pings and no link previews (the recap's VOD / clip links would each unfurl)."""
        channel = self.get_channel(int(channel_id))
        if channel is None:
            log.info("recap channel %s not found (deleted, or the bot can't see it)", channel_id)
            return
        await channel.send(text[:2000], allowed_mentions=NO_PINGS, suppress_embeds=True)

    def is_owner_id(self, user: discord.abc.User) -> bool:
        return user.id == self.settings.owner_id

    def may_manage(self, inter: discord.Interaction) -> bool:
        perms = getattr(inter.user, "guild_permissions", None)
        return self.is_owner_id(inter.user) or bool(perms and perms.manage_guild)

    async def setup_hook(self) -> None:
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Discord: logged in as %s", self.user)
        self.ready_event.set()

    # ── server care: Discord events go to flavrbot/server_care.py ──────
    async def on_message(self, m: discord.Message) -> None:
        await self.care.on_message(m)
        await self.community.on_message(m)          # modmail (DMs in, mods' thread answers out)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        await self.care.on_message_edit(before, after)

    async def on_raw_message_delete(self, p: discord.RawMessageDeleteEvent) -> None:
        await self.care.on_raw_message_delete(p)

    async def on_member_join(self, member: discord.Member) -> None:
        await self.care.on_member_join(member)
        await self.community.on_member_join(member)  # raid watch, welcome, weekly growth

    async def on_member_remove(self, member: discord.Member) -> None:
        await self.care.on_member_remove(member)
        await self.community.on_member_remove(member)

    async def on_raw_reaction_add(self, p: discord.RawReactionActionEvent) -> None:
        await self.community.on_raw_reaction_add(p)  # starboard

    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        await self.care.on_audit_log_entry_create(entry)

    async def on_interaction(self, inter: discord.Interaction) -> None:
        await self.care.on_interaction(inter)       # role buttons; slash commands go through self.tree
        await self.community.on_interaction(inter)  # the "I read the rules" button

    # ── commands ───────────────────────────────────────────────────────
    def _register(self) -> None:
        tree = self.tree
        bot = self

        async def owner_only(inter: discord.Interaction) -> bool:
            if bot.is_owner_id(inter.user):
                return True
            await inter.response.send_message("Only the streamer can use this.", ephemeral=True)
            return False

        async def managers_only(inter: discord.Interaction) -> bool:
            if bot.may_manage(inter):
                return True
            await inter.response.send_message("Only the streamer or people with Manage Server can use this.",
                                              ephemeral=True)
            return False

        @tree.command(name="status", description="Stream Core, the stream, this PC and FlaVR Promo (owner only)")
        async def status(inter: discord.Interaction):
            if not await owner_only(inter):
                return
            hb = bot.link.heartbeat
            st = bot.link.stream()
            lines = [f"**Stream Core:** {'🟢 answering' if bot.link.core_online() else '⚪ not answering'} "
                     f"(last heartbeat {ago(bot.link.heartbeat_at)})"]
            if st:
                state = st.get("state") or ("live" if st.get("live") else "offline")
                where = ", ".join(str(p).title() for p in st.get("platforms") or [])
                lines.append(f"**Stream:** {state}" + (f" on {where}" if where else "")
                             + (f" for {length(st.get('length_sec'))}" if state == "live" else ""))
                if hb.get("viewers") is not None:
                    lines.append(f"**Viewers:** {hb.get('viewers')} · chat {hb.get('cpm', 0)}/min · "
                                 f"credits {hb.get('credits', 0)}")
            if bot.link.refused:
                lines.append(f"⚠️ {bot.link.refused} pushes had the wrong password.")
            si = sysinfo.info(bot.settings.data_dir)
            if si:
                up = length(si.get("uptime_sec", 0))
                lines.append(f"**This PC:** up {up} · load {si.get('load', '?')} · "
                             f"{si.get('disk_free_gb', '?')} GB free ({si.get('disk_used_pct', '?')}% used)")
            # FlaVR Promo (flavrbot/promo_link.py): the go-live watcher's answer, if PROMO_URL is set
            pr = bot.promo
            if not pr.ready:
                lines.append("**FlaVR Promo:** ➖ PROMO_URL not set")
            elif pr.fresh():
                me = {True: "you're live", False: "you're offline everywhere", None: "it can't tell if you're live"}
                lines.append(f"**FlaVR Promo:** ✅ answering ({ago(pr.answer_at)}): {me[pr.owner_live()]}, "
                             f"{len(pr.answer.get('live') or [])} live")
            else:
                lines.append(f"**FlaVR Promo:** ⚠️ not answering ({pr.error or 'no answer yet'})")
            await inter.response.send_message("\n".join(lines), ephemeral=True)

        core = app_commands.Group(name="core", description="The Stream Core link (owner only)")

        @core.command(name="done", description="My stream is over: stop the Stream Core alarm until Core next connects")
        async def core_done(inter: discord.Interaction):
            # flavrbot/core_link.py mark_done: Core's last word becomes "offline", so its silence no longer
            # pings ("Stream Core stopped answering"), and its next heartbeat comes back quietly
            if not await owner_only(inter):
                return
            was = bot.link.mark_done()
            await inter.response.send_message(
                ("Got it: the stream counts as over, and Stream Core going quiet won't ping you. Checks start "
                 "again by themselves the next time Core connects." if was else
                 "The bot already had your stream as over; Core going quiet won't ping you. Checks start again "
                 "the next time Core connects."), ephemeral=True)

        tree.add_command(core)

        @tree.command(name="wake", description="Turn the stream PC on (Wake-on-LAN, owner only)")
        async def wake(inter: discord.Interaction):
            if not await owner_only(inter):
                return
            if not bot.settings.stream_pc_mac:
                await inter.response.send_message("Set STREAM_PC_MAC first (docs/WAKE_ON_LAN.md).", ephemeral=True)
                return
            try:
                wol.wake(bot.settings.stream_pc_mac, bot.settings.wol_broadcast)
            except (OSError, ValueError) as e:
                await inter.response.send_message(f"Couldn't send the wake-up: {e}", ephemeral=True)
                return
            await inter.response.send_message("Wake-up sent. The stream PC should start in a few seconds; "
                                              "/status shows Stream Core once it runs.", ephemeral=True)

        pings = app_commands.Group(name="pings", description="Phone pings (DMs) from this bot (owner only)")

        @pings.command(name="view", description="Which pings are on, and quiet hours")
        async def pings_view(inter: discord.Interaction):
            if not await owner_only(inter):
                return
            sw = bot.pinger.switches()
            a, b = bot.pinger.quiet
            lines = [f"{'✅' if sw[k] else '➖'} `{k}`: {label}" for k, (label, _) in KINDS.items()]
            lines.append(f"Quiet hours: {a}:00 to {b}:00 ({bot.settings.timezone})" if a != b else "Quiet hours: off")
            await inter.response.send_message("\n".join(lines), ephemeral=True)

        @pings.command(name="set", description="Switch one kind of ping on or off")
        @app_commands.choices(kind=[app_commands.Choice(name=f"{k}: {v[0]}"[:100], value=k) for k, v in KINDS.items()])
        async def pings_set(inter: discord.Interaction, kind: app_commands.Choice[str], on: bool):
            if not await owner_only(inter):
                return
            bot.pinger.set(kind.value, on)
            await inter.response.send_message(f"`{kind.value}` pings {'on' if on else 'off'}.", ephemeral=True)

        @pings.command(name="quiet", description="Quiet hours (0-23); the same number twice turns them off")
        async def pings_quiet(inter: discord.Interaction, start: app_commands.Range[int, 0, 23],
                              end: app_commands.Range[int, 0, 23]):
            if not await owner_only(inter):
                return
            bot.pinger.set_quiet(start, end)
            await inter.response.send_message("Quiet hours off." if start == end else
                                              f"Quiet hours {start}:00 to {end}:00. Urgent pings still come.",
                                              ephemeral=True)

        tree.add_command(pings)

        @tree.command(name="points", description="Points (Stream Core's last copy)")
        async def points(inter: discord.Interaction, name: Optional[str] = None):
            rows = bot.link.snapshot.get("points") or []
            if not rows:
                await inter.response.send_message("No points copy from Stream Core yet.", ephemeral=True)
                return
            when = f"\n-# as of {ago(bot.link.snapshot_at)}"
            if name:
                q = name.lower().lstrip("@")
                hit = [r for r in rows if q == str(r.get("name", "")).lower()
                       or q in [a.split(":", 1)[-1].lower() for a in str(r.get("accounts", "")).split(", ")]]
                if not hit:
                    hit = [r for r in rows if q in str(r.get("name", "")).lower()][:5]
                if not hit:
                    await inter.response.send_message(f"No one called {name} in the top {len(rows)}.", ephemeral=True)
                    return
                text = "\n".join(f"**{r['name']}**: {r['points']:,} points (#{rows.index(r) + 1})" for r in hit)
            else:
                text = "\n".join(f"{i + 1}. **{r['name']}**: {r['points']:,}" for i, r in enumerate(rows[:10]))
            await inter.response.send_message(text + when, allowed_mentions=NO_PINGS)

        @tree.command(name="queue", description="Video requests: now playing and up next")
        async def queue(inter: discord.Interaction):
            q = bot.link.snapshot.get("queue")
            if not q:
                await inter.response.send_message("No queue copy from Stream Core yet.", ephemeral=True)
                return
            lines = []
            if q.get("now"):
                lines.append(f"▶️ **Now:** {q['now'].get('title') or q['now'].get('video_id')} (from {q['now'].get('by') or '?'})")
            for i, v in enumerate(q.get("up_next") or [], 1):
                lines.append(f"{i}. {v.get('title') or v.get('video_id')} (from {v.get('by') or '?'})")
            if q.get("pending"):
                lines.append(f"⏳ {q['pending']} waiting for a yes")
            await inter.response.send_message("\n".join(lines) or "The queue is empty.", allowed_mentions=NO_PINGS)

        setup = app_commands.Group(name="setup", description="Bot settings (owner or Manage Server)")

        @setup.command(name="recap-channel", description="Where the after-stream recap goes (leave out to stop)")
        async def setup_recap(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
            # kv recap_channel, read by flavrbot/recap.py when a recap is due; the owner's DM copy is /pings recap
            if not await managers_only(inter):
                return
            bot.store.put("recap_channel", str(channel.id) if channel else "")
            await inter.response.send_message(
                f"After each stream, the recap goes to {channel.mention}." if channel else
                "Recaps no longer go to a channel (your DM copy follows /pings recap).", ephemeral=True)

        @setup.command(name="mod-log", description="Where the mod log goes (deletes, edits, joins, bans, scams)")
        async def setup_mod_log(inter: discord.Interaction, channel: discord.TextChannel):
            if not await managers_only(inter) or inter.guild is None:
                return
            bot.store.put(f"mod_log.{inter.guild.id}", str(channel.id))
            todo = missing(inter.guild, inter.guild.me, bot.care.gateway, mod_log=channel)
            await inter.response.send_message(f"The mod log goes to {channel.mention}. Keep that channel private to mods."
                                              + ("\n\nStill needed:\n• " + "\n• ".join(todo) if todo else ""),
                                              ephemeral=True)

        @setup.command(name="check", description="What server care still needs (permissions, switches)")
        async def setup_check(inter: discord.Interaction):
            if not await managers_only(inter) or inter.guild is None:
                return
            panel = [r for r in (inter.guild.get_role(int(x["id"])) for x in bot.care.panel(inter.guild.id)["roles"]) if r]
            todo = missing(inter.guild, inter.guild.me, bot.care.gateway, mod_log=bot.care.log_channel(inter.guild),
                           panel=panel)
            if bot.care.log_channel(inter.guild) is None:
                todo.append("Pick the mod log channel: /setup mod-log.")
            todo += missing_more(inter.guild, inter.guild.me, bot.care.gateway, bot.community)   # round 2 parts
            await inter.response.send_message("Everything's set. ✅" if not todo else "Still needed:\n• " + "\n• ".join(todo),
                                              ephemeral=True)

        tree.add_command(setup)

        roles = app_commands.Group(name="roles", description="Role buttons (owner or Manage Server)")

        @roles.command(name="add", description="Put a role on the button panel")
        @app_commands.describe(label="Button text (default: the role's name)", emoji="Optional emoji on the button")
        async def roles_add(inter: discord.Interaction, role: discord.Role, label: Optional[str] = None,
                            emoji: Optional[str] = None):
            if not await managers_only(inter) or inter.guild is None:
                return
            pm = role.permissions
            if role.is_default() or role.managed or pm.administrator or pm.manage_guild or pm.manage_roles \
                    or pm.manage_channels or pm.manage_messages or pm.kick_members or pm.ban_members \
                    or pm.moderate_members or pm.mention_everyone:
                await inter.response.send_message(f"**{role.name}** can't be a button: it's a bot's own role or it has "
                                                  "moderator permissions, and anyone could give it to themselves.",
                                                  ephemeral=True)
                return
            p = bot.care.panel(inter.guild.id)
            p["roles"] = [r for r in p["roles"] if int(r["id"]) != role.id]
            if len(p["roles"]) >= MAX_ROLE_BUTTONS:
                await inter.response.send_message(f"The panel is full ({MAX_ROLE_BUTTONS} buttons).", ephemeral=True)
                return
            p["roles"].append({"id": str(role.id), "label": (label or role.name)[:80], "emoji": (emoji or "").strip()[:40]})
            bot.care.save_panel(inter.guild.id, p)
            note = "" if role.is_assignable() else (f"\n⚠️ Drag my role above **{role.name}** in Server Settings → Roles, "
                                                    "or the button can't give it.")
            await inter.response.send_message(f"Added **{role.name}**. Run /roles post to show the panel (or update it)."
                                              + note, ephemeral=True, allowed_mentions=NO_PINGS)

        @roles.command(name="remove", description="Take a role off the button panel")
        async def roles_remove(inter: discord.Interaction, role: discord.Role):
            if not await managers_only(inter) or inter.guild is None:
                return
            p = bot.care.panel(inter.guild.id)
            before = len(p["roles"])
            p["roles"] = [r for r in p["roles"] if int(r["id"]) != role.id]
            bot.care.save_panel(inter.guild.id, p)
            await inter.response.send_message(f"Removed **{role.name}**. Run /roles post to update the panel."
                                              if len(p["roles"]) < before else f"**{role.name}** wasn't on the panel.",
                                              ephemeral=True, allowed_mentions=NO_PINGS)

        @roles.command(name="list", description="The roles on the button panel")
        async def roles_list(inter: discord.Interaction):
            if not await managers_only(inter) or inter.guild is None:
                return
            p = bot.care.panel(inter.guild.id)
            lines = [f"{r.get('emoji') or '•'} {r['label']} (<@&{r['id']}>)" for r in p["roles"]] or ["No roles yet: /roles add."]
            if p.get("channel"):
                lines.append(f"Panel in <#{p['channel']}>.")
            await inter.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_PINGS)

        @roles.command(name="post", description="Post the role button panel (or update the one already there)")
        @app_commands.describe(title="Panel heading", text="Text above the buttons")
        async def roles_post(inter: discord.Interaction, channel: discord.TextChannel, title: Optional[str] = None,
                             text: Optional[str] = None):
            if not await managers_only(inter) or inter.guild is None:
                return
            p = bot.care.panel(inter.guild.id)
            if title:
                p["title"] = title[:256]
            if text:
                p["text"] = text[:2000]
            bot.care.save_panel(inter.guild.id, p)
            await inter.response.defer(ephemeral=True)
            problem = await bot.care.post_panel(channel)
            await inter.followup.send(problem or f"The role panel is in {channel.mention}.", ephemeral=True)

        tree.add_command(roles)

        mod = app_commands.Group(name="mod", description="Scam link lists (owner or Manage Server)")

        @mod.command(name="block", description="Treat every link to this site as a scam")
        async def mod_block(inter: discord.Interaction, domain: str):
            if not await managers_only(inter):
                return
            d = bot.care.set_domain("blocked", domain, True)
            await inter.response.send_message(f"Links to **{d}** are now removed." if d else "That isn't a web address.",
                                              ephemeral=True)

        @mod.command(name="unblock", description="Take a site off the block list")
        async def mod_unblock(inter: discord.Interaction, domain: str):
            if not await managers_only(inter):
                return
            d = bot.care.set_domain("blocked", domain, False)
            await inter.response.send_message(f"**{d}** is off the block list." if d else "That isn't a web address.",
                                              ephemeral=True)

        @mod.command(name="allow", description="Never treat this site as a scam (fixes a wrong catch)")
        async def mod_allow(inter: discord.Interaction, domain: str, allowed: bool = True):
            if not await managers_only(inter):
                return
            d = bot.care.set_domain("allowed", domain, allowed)
            await inter.response.send_message((f"**{d}** is always allowed." if allowed else f"**{d}** is checked again.")
                                              if d else "That isn't a web address.", ephemeral=True)

        @mod.command(name="list", description="The blocked and always-allowed sites")
        async def mod_list(inter: discord.Interaction):
            if not await managers_only(inter):
                return
            b, a = sorted(bot.care.domains("blocked")), sorted(bot.care.domains("allowed"))
            await inter.response.send_message(f"**Blocked:** {', '.join(b) or 'none'}\n**Always allowed:** "
                                              f"{', '.join(a) or 'none'}\nFake Discord / Steam / Nitro sites are caught "
                                              "without being listed.", ephemeral=True)

        tree.add_command(mod)

        backup = app_commands.Group(name="backup", description="Backups of the server's layout (owner or Manage Server)")

        @backup.command(name="now", description="Back up channels, roles and permissions now (file sent to you)")
        async def backup_now(inter: discord.Interaction):
            if not await managers_only(inter) or inter.guild is None:
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            path = await bot.care.backup(inter.guild)
            await inter.followup.send(f"Saved. A copy is also kept on the bot's PC ({len(list(path.parent.glob('server-*.json')))} "
                                      "daily backups so far).", file=discord.File(path), ephemeral=True)

        tree.add_command(backup)

        # round 2 commands live next to their code (flavrbot/community.py): /welcome, /lockdown, /warn,
        # /warnings, /modmail, /live-mode, /birthday, /growth and /setup modmail | starboard | moments
        community_cmds.register(tree, bot, managers_only)
        said.register(tree, bot)                    # /said chat search (flavrbot/said.py)
