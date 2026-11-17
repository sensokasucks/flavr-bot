"""
The Discord side of FlaVR Bot: slash commands, DMs to the owner, and the go-live posts.

Commands (registered on GUILD_ID at once when it is set, else globally, which Discord can take
up to an hour to show the first time):

  Anyone
    /points [name]      points from Stream Core's last copy (works while the stream PC is off)
    /queue              video requests: now playing, up next, waiting
    /live               who you follow is live right now
    /schedule [name]    upcoming streams (Twitch schedule + YouTube scheduled streams); default the owner
    /friend list        everyone the watcher follows
  Owner (OWNER_ID) or Manage Server
    /friend add name platform handle [group] [owner]   follow a channel (adds the person if new)
    /friend remove name    /friend main name platform   /friend import list
    /setup live-channel group channel                   where go-live posts go for a group
  Owner only (answers only you can see)
    /status             Stream Core, the stream, this PC, the watcher
    /wake               Wake-on-LAN the stream PC
    /pings view | set kind on | quiet start end          phone ping switches and quiet hours

Every message sets allowed_mentions to nothing, so a stream title can't ping @everyone.
Commands answer "only you can see this" (ephemeral) unless they are meant for the channel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands

from . import sysinfo, wol
from .config import Settings
from .core_link import CoreLink
from .pings import KINDS, Pinger, length
from .store import GROUPS, PLATFORMS, Store
from .watcher import Watcher, main_platform

log = logging.getLogger("flavrbot.bot")
NO_PINGS = discord.AllowedMentions.none()
# FlaVR brand colours (the logo's pink and teal); the friends group gets teal
GROUP_COLOURS = {"flavr": 0xF72CA2, "friends": 0x2AE0D4}
ENDED_COLOUR = 0x5B5F66
PLATFORM_ICONS = {"youtube": "▶️ YouTube", "twitch": "🟣 Twitch", "kick": "🟢 Kick"}


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    sec = int(time.time() - ts)
    if sec < 90:
        return f"{sec} s ago"
    if sec < 5400:
        return f"{sec // 60} min ago"
    return f"{sec // 3600} h ago"


def parse_import(text: str) -> List[Tuple[str, str, str, str]]:
    """``/friend import`` text → [(name, platform, handle, group)].

    One line per channel: ``Name, platform, handle[, group]`` (commas or tabs), e.g.
    ``Lulu, youtube, @luluteala, flavr``. Lines starting with # and blank lines are skipped."""
    out = []
    for line in (text or "").replace("\t", ",").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or parts[1].lower() not in PLATFORMS:
            continue
        group = parts[3].lower() if len(parts) > 3 and parts[3].lower() in GROUPS else "flavr"
        out.append((parts[0], parts[1].lower(), parts[2], group))
    return out


class FlavrBot(discord.Client):
    def __init__(self, settings: Settings, store: Store, link: CoreLink, pinger: Pinger, watcher: Watcher):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=NO_PINGS)
        self.settings = settings
        self.store = store
        self.link = link
        self.pinger = pinger
        self.watcher = watcher
        self.tree = app_commands.CommandTree(self)
        # set once Discord says we're in; the go-live watcher waits for it so its first posts don't get lost
        self.ready_event = asyncio.Event()
        self._register()

    # ── helpers ────────────────────────────────────────────────────────
    async def dm_owner(self, text: str) -> None:
        """flavrbot/pings.py's ``send``: one DM to OWNER_ID."""
        user = self.get_user(self.settings.owner_id) or await self.fetch_user(self.settings.owner_id)
        await user.send(text, allowed_mentions=NO_PINGS)

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

    # ── go-live posts (Watcher callbacks) ──────────────────────────────
    def live_embed(self, person: Dict[str, Any], session: Dict[str, Any],
                   live_on: Dict[str, Dict[str, Any]]) -> discord.Embed:
        main = main_platform(person, live_on)
        info = live_on.get(main) or {}
        e = discord.Embed(title=f"{person['name']} is live", description=session.get("title") or info.get("title") or "",
                          url=info.get("url") or session.get("url") or None,
                          colour=GROUP_COLOURS.get(person.get("grp"), GROUP_COLOURS["flavr"]))
        links = " · ".join(f"[{PLATFORM_ICONS.get(p, p)}]({i.get('url')})" for p, i in live_on.items())
        e.add_field(name="Watch", value=links or "—", inline=False)
        if session.get("game"):
            e.add_field(name="Playing", value=session["game"], inline=True)
        if session.get("restarts"):
            e.add_field(name="Restarts", value=str(session["restarts"]), inline=True)
        if info.get("thumb"):
            e.set_image(url=info["thumb"])
        if info.get("avatar"):
            e.set_thumbnail(url=info["avatar"])
        if session.get("started_at"):
            e.timestamp = datetime.fromtimestamp(float(session["started_at"]))
            e.set_footer(text="Started")
        return e

    async def post_start(self, person, session, live_on) -> Tuple[str, str]:
        channel_id = self.store.get(f"live_channel.{person.get('grp') or 'flavr'}")
        main = main_platform(person, live_on)
        if not person.get("is_owner"):
            self.pinger.friend_live(person["name"], PLATFORM_ICONS.get(main, main))
        if not channel_id:
            return "", ""
        channel = self.get_channel(int(channel_id))
        if channel is None:
            return "", ""
        msg = await channel.send(embed=self.live_embed(person, session, live_on), allowed_mentions=NO_PINGS)
        return str(channel.id), str(msg.id)

    async def _message(self, session) -> Optional[discord.Message]:
        if not session.get("post_channel") or not session.get("post_message"):
            return None
        channel = self.get_channel(int(session["post_channel"]))
        if channel is None:
            return None
        try:
            return await channel.fetch_message(int(session["post_message"]))
        except discord.HTTPException:
            return None

    async def post_change(self, person, session, live_on) -> None:
        msg = await self._message(session)
        if msg:
            await msg.edit(embed=self.live_embed(person, session, live_on), allowed_mentions=NO_PINGS)

    async def post_end(self, person, session) -> None:
        msg = await self._message(session)
        if not msg:
            return
        e = msg.embeds[0] if msg.embeds else discord.Embed()
        e.title = f"{person['name']} was live"
        e.colour = ENDED_COLOUR
        e.set_footer(text=f"Live for {length(float(session['ended_at']) - float(session['started_at']))}")
        # the Twitch VOD, when there is one (YouTube's stream page becomes its VOD by itself)
        tw = self.watcher.checkers.get("twitch")
        acc = next((a for a in person.get("accounts") or [] if a["platform"] == "twitch" and a["platform_id"]), None)
        if tw and acc:
            try:
                vod = await tw.last_vod(acc["platform_id"])
                if vod and vod.get("url"):
                    e.add_field(name="VOD", value=f"[Watch the Twitch VOD]({vod['url']})", inline=False)
            except Exception:
                log.info("Twitch VOD look-up failed", exc_info=True)
        await msg.edit(embed=e, allowed_mentions=NO_PINGS)

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

        @tree.command(name="status", description="Stream Core, the stream, this PC and the watcher (owner only)")
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
            ws = bot.watcher.status()
            parts = []
            for p, s in ws["platforms"].items():
                mark = "⚠️" if s["error"] else ("✅" if s["ready"] else "➖ no key")
                parts.append(f"{p.title()} {mark} {ago(s['last_check']) if s['ready'] else ''}".strip())
            lines.append(f"**Watcher:** {ws['people']} people · " + " · ".join(parts))
            await inter.response.send_message("\n".join(lines), ephemeral=True)

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

        @tree.command(name="live", description="Who's live right now")
        async def live(inter: discord.Interaction):
            rows = bot.store.live_now()
            if not rows:
                await inter.response.send_message("Nobody on the list is live right now.", ephemeral=True)
                return
            lines = [f"🔴 **{r['name']}** for {length(time.time() - float(r['started_at']))}: [{r['title'] or 'live'}]({r['url']})"
                     for r in rows]
            await inter.response.send_message("\n".join(lines)[:1900], allowed_mentions=NO_PINGS, suppress_embeds=True)

        @tree.command(name="schedule", description="Upcoming streams (Twitch schedule and YouTube scheduled streams)")
        async def schedule(inter: discord.Interaction, name: Optional[str] = None):
            people = bot.store.people()
            person = (bot.store.person(name) if name else next((p for p in people if p["is_owner"]), None))
            if not person:
                await inter.response.send_message("Who? Add them with /friend add (or mark yourself with owner: True).",
                                                  ephemeral=True)
                return
            await inter.response.defer()
            items = await bot.watcher.schedule(person)
            if not items:
                await inter.followup.send(f"Nothing scheduled for {person['name']}.", allowed_mentions=NO_PINGS)
                return
            lines = [f"<t:{int(s['at'])}:F> (<t:{int(s['at'])}:R>) {PLATFORM_ICONS.get(s['platform'], '')}: "
                     f"{s.get('title') or 'stream'}" + (f" · {s['game']}" if s.get("game") else "") for s in items[:10]]
            await inter.followup.send(f"**{person['name']}**\n" + "\n".join(lines), allowed_mentions=NO_PINGS)

        friend = app_commands.Group(name="friend", description="People the go-live watcher follows")
        plat_choices = [app_commands.Choice(name=p.title(), value=p) for p in PLATFORMS]
        group_choices = [app_commands.Choice(name="FlaVR", value="flavr"), app_commands.Choice(name="Friends", value="friends")]

        @friend.command(name="add", description="Follow a channel (adds the person if new)")
        @app_commands.choices(platform=plat_choices, group=group_choices)
        async def friend_add(inter: discord.Interaction, name: str, platform: app_commands.Choice[str], handle: str,
                             group: Optional[app_commands.Choice[str]] = None, owner: bool = False):
            if not await managers_only(inter):
                return
            pid = bot.store.add_person(name, group.value if group else "flavr", owner, str(inter.user))
            if not bot.store.add_account(pid, platform.value, handle.strip()):
                await inter.response.send_message(f"{platform.name} {handle} is already on the list.", ephemeral=True)
                return
            await inter.response.send_message(f"Following {name} on {platform.name} ({handle}). "
                                              "The first check runs within a few minutes.", ephemeral=True)

        @friend.command(name="remove", description="Stop following someone (all their channels)")
        async def friend_remove(inter: discord.Interaction, name: str):
            if not await managers_only(inter):
                return
            ok = bot.store.remove_person(name)
            await inter.response.send_message(f"Removed {name}." if ok else f"No one called {name}.", ephemeral=True)

        @friend.command(name="main", description="Which platform a person's posts link to first")
        @app_commands.choices(platform=plat_choices)
        async def friend_main(inter: discord.Interaction, name: str, platform: app_commands.Choice[str]):
            if not await managers_only(inter):
                return
            p = bot.store.person(name)
            if not p:
                await inter.response.send_message(f"No one called {name}.", ephemeral=True)
                return
            bot.store.set_main(p["id"], platform.value)
            await inter.response.send_message(f"{name}'s posts now lead with {platform.name}.", ephemeral=True)

        @friend.command(name="list", description="Everyone the watcher follows")
        async def friend_list(inter: discord.Interaction):
            people = bot.store.people()
            if not people:
                await inter.response.send_message("Nobody yet. Add people with /friend add or /friend import.", ephemeral=True)
                return
            lines = []
            for g in GROUPS:
                rows = [p for p in people if p["grp"] == g]
                if rows:
                    lines.append(f"**{'FlaVR' if g == 'flavr' else 'Friends'}** ({len(rows)})")
                    for p in rows:
                        accs = ", ".join(f"{a['platform']}:{a['handle']}" + ("" if a["platform_id"] else " (not found yet)")
                                         for a in p["accounts"])
                        lines.append(f"• {p['name']}{' ⭐' if p['is_owner'] else ''}: {accs}")
            await inter.response.send_message("\n".join(lines)[:1900], ephemeral=True)

        @friend.command(name="import", description="Add many: one line each, 'Name, platform, handle, group'")
        async def friend_import(inter: discord.Interaction, lines: str):
            if not await managers_only(inter):
                return
            # Discord's text box keeps line breaks out of slash options, so ";" also separates lines
            rows = parse_import(lines.replace(";", "\n"))
            added = 0
            for name, plat, handle, group in rows:
                pid = bot.store.add_person(name, group, False, str(inter.user))
                added += bot.store.add_account(pid, plat, handle)
            await inter.response.send_message(f"Added {added} channel(s) from {len(rows)} line(s). "
                                              "Example line: `Lulu, youtube, @luluteala, flavr` (separate lines with ;).",
                                              ephemeral=True)

        tree.add_command(friend)

        setup = app_commands.Group(name="setup", description="Bot settings (owner or Manage Server)")

        @setup.command(name="live-channel", description="Where go-live posts for a group go")
        @app_commands.choices(group=group_choices)
        async def setup_live(inter: discord.Interaction, group: app_commands.Choice[str], channel: discord.TextChannel):
            if not await managers_only(inter):
                return
            bot.store.put(f"live_channel.{group.value}", str(channel.id))
            await inter.response.send_message(f"{group.name} go-live posts go to {channel.mention}.", ephemeral=True)

        tree.add_command(setup)
