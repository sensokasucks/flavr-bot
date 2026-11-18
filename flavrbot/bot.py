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
    /calendar view [group] [week]   the weekly calendar picture (last week = the recap)
  Owner (OWNER_ID) or Manage Server
    /friend add name platform handle [group] [owner]   follow a channel (adds the person if new)
    /friend remove name    /friend main name platform   /friend import list
    /friend schedule name day time [title]   a regular weekly time by hand (Kick, no-schedule people)
    /friend unschedule name    /friend hide name hidden   (keep someone off the calendar)
    /setup live-channel group channel                   where go-live posts go for a group
    /setup calendar-channel group channel               where the weekly calendar + recap go
    /calendar post [group]  a fresh calendar post now
    /calendar refresh [week]  fill a week in again from the channels' past streams (VODs)
    /calendar share [group] [week] [style]   DMs you copy-ready text for other Discords and X (flavrbot/share.py)
    /setup share-style style [bold_days] [hashtags]   the X text's default layout (post + links reply /
                        one post / 280 thread), bold-looking day headings, a hashtag line
    /friend x name handle   their X handle ("-" removes it); tagged only with /setup share-style tag_people
  Server care (flavrbot/server_care.py; owner or Manage Server)
    /setup mod-log channel    where the mod log goes      /setup check   what the bot still needs
    /roles add role [label] [emoji] | remove role | list | post channel [title] [text]   role buttons
    /mod block domain | unblock domain | allow domain | list    the scam link lists
    /backup now               a fresh backup of the server's layout, sent to you
  Owner only (answers only you can see)
    /status             Stream Core, the stream, this PC, the watcher
    /wake               Wake-on-LAN the stream PC
    /pings view | set kind on | quiet start end          phone ping switches and quiet hours

Every message sets allowed_mentions to nothing, so a stream title can't ping @everyone.
Commands answer "only you can see this" (ephemeral) unless they are meant for the channel.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands

from . import sysinfo, wol
from .calendar import DAY_NAMES
from .config import Settings
from .server_care import MAX_ROLE_BUTTONS, ServerCare, missing
from .core_link import CoreLink
from .pings import KINDS, Pinger, length
from .share import X_STYLES
from .store import GROUPS, PLATFORMS, Store
from .watcher import Watcher, main_platform

log = logging.getLogger("flavrbot.bot")
NO_PINGS = discord.AllowedMentions.none()
# FlaVR brand colours (the logo's pink and teal); the friends group gets teal
GROUP_COLOURS = {"flavr": 0xF72CA2, "friends": 0x2AE0D4}
ENDED_COLOUR = 0x5B5F66
PLATFORM_ICONS = {"youtube": "▶️ YouTube", "twitch": "🟣 Twitch", "kick": "🟢 Kick"}


def parse_time(text: str) -> Optional[int]:
    """"7 PM", "7:30pm", "19:30", "9" → minutes after midnight (None when unreadable)."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*", (text or "").lower())
    if not m:
        return None
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "")[:1]
    if mins > 59 or h > 23 or (ampm and not 1 <= h <= 12):
        return None
    if ampm == "p" and h != 12:
        h += 12
    elif ampm == "a" and h == 12:
        h = 0
    return h * 60 + mins


def make_embed(data: Dict[str, Any], picture: str = "") -> discord.Embed:
    """flavrbot/calendar_post.py ``embed()`` data → a discord.Embed (with the attached picture)."""
    e = discord.Embed(title=data["title"][:256], colour=data.get("colour") or GROUP_COLOURS["flavr"])
    for name, value in data.get("fields") or []:
        e.add_field(name=name[:256], value=value[:1024], inline=False)
    if data.get("empty"):
        e.description = "Nothing on the calendar yet this week."
    if data.get("footer"):
        e.set_footer(text=data["footer"][:2048])
    if picture:
        e.set_image(url=f"attachment://{picture}")
    return e


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
    def __init__(self, settings: Settings, store: Store, link: CoreLink, pinger: Pinger, watcher: Watcher,
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
        self.watcher = watcher
        self.tree = app_commands.CommandTree(self)
        # the weekly calendar (flavrbot/calendar_post.py); set by flavrbot/__main__.py
        self.calendar: Any = None
        # set once Discord says we're in; the go-live watcher waits for it so its first posts don't get lost
        self.ready_event = asyncio.Event()
        # role buttons, mod log, scam links, backups (settings: SCAM_TIMEOUT_MIN, BACKUP_HOUR, BACKUP_KEEP)
        self.care = ServerCare(self, store, settings.data_dir, tz=settings.timezone,
                               scam_timeout_min=settings.scam_timeout_min, backup_hour=settings.backup_hour,
                               backup_keep=settings.backup_keep, gateway=gateway)
        self._register()

    # ── helpers ────────────────────────────────────────────────────────
    async def dm_owner(self, text: str, png: Optional[bytes] = None) -> None:
        """flavrbot/pings.py's ``send`` (and the calendar pings, with the picture): one DM to OWNER_ID."""
        user = self.get_user(self.settings.owner_id) or await self.fetch_user(self.settings.owner_id)
        files = [discord.File(io.BytesIO(png), filename="calendar.png")] if png else []
        await user.send(text, files=files, allowed_mentions=NO_PINGS)

    async def dm_share(self, messages: List[str], png: Optional[bytes] = None) -> None:
        """flavrbot/calendar_post.py's share package: the picture with the first message, then one DM
        per text so each copies on its own. No link previews (they'd hide the text on a phone)."""
        user = self.get_user(self.settings.owner_id) or await self.fetch_user(self.settings.owner_id)
        for i, text in enumerate(messages):
            files = [discord.File(io.BytesIO(png), filename="calendar.png")] if png and i == 0 else []
            await user.send(text[:2000], files=files, allowed_mentions=NO_PINGS, suppress_embeds=True)

    # ── calendar posts (flavrbot/calendar_post.py calls these) ─────────
    async def cal_send(self, channel_id: str, data: Dict[str, Any], png: Optional[bytes], name: str) -> str:
        """A new calendar / recap post; returns its message id ("" when it couldn't)."""
        channel = self.get_channel(int(channel_id))
        if channel is None:
            return ""
        files = [discord.File(io.BytesIO(png), filename=name)] if png else []
        try:
            msg = await channel.send(embed=make_embed(data, name if png else ""), files=files,
                                     allowed_mentions=NO_PINGS)
        except discord.HTTPException as e:
            log.warning("calendar post failed: %s", e)
            return ""
        return str(msg.id)

    async def cal_edit(self, channel_id: str, message_id: str, data: Dict[str, Any], png: Optional[bytes],
                       name: str) -> bool:
        """Edit a calendar post in place (new picture too). False when the post is gone."""
        channel = self.get_channel(int(channel_id))
        if channel is None:
            return False
        try:
            msg = channel.get_partial_message(int(message_id))
            kw: Dict[str, Any] = {"embed": make_embed(data, name if png else ""), "allowed_mentions": NO_PINGS}
            if png:
                kw["attachments"] = [discord.File(io.BytesIO(png), filename=name)]
            await msg.edit(**kw)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException as e:
            log.warning("calendar edit failed: %s", e)
            return True         # a hiccup, not a deleted post: keep it and try again next change

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

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        await self.care.on_message_edit(before, after)

    async def on_raw_message_delete(self, p: discord.RawMessageDeleteEvent) -> None:
        await self.care.on_raw_message_delete(p)

    async def on_member_join(self, member: discord.Member) -> None:
        await self.care.on_member_join(member)

    async def on_member_remove(self, member: discord.Member) -> None:
        await self.care.on_member_remove(member)

    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        await self.care.on_audit_log_entry_create(entry)

    async def on_interaction(self, inter: discord.Interaction) -> None:
        await self.care.on_interaction(inter)       # role buttons; slash commands go through self.tree

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
        # the VOD the watcher found (flavrbot/watcher.py find_vod: Twitch archive / YouTube stream page)
        if session.get("vod_url"):
            e.add_field(name="VOD", value=f"[Watch the VOD]({session['vod_url']})", inline=False)
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

        day_choices = [app_commands.Choice(name=d.title(), value=i) for i, d in enumerate(DAY_NAMES)]

        @friend.command(name="schedule", description="A regular weekly stream time by hand (for the calendar)")
        @app_commands.choices(day=day_choices)
        @app_commands.describe(time="Like 7 PM or 19:30, in the bot's time zone (TZ)", title="Optional, e.g. the game")
        async def friend_schedule(inter: discord.Interaction, name: str, day: app_commands.Choice[int], time: str,
                                  title: Optional[str] = None):
            if not await managers_only(inter):
                return
            p = bot.store.person(name)
            minute = parse_time(time)
            if not p or minute is None:
                await inter.response.send_message(f"No one called {name}." if not p else
                                                  "Couldn't read that time. Try 7 PM or 19:30.", ephemeral=True)
                return
            bot.store.add_manual(p["id"], day.value, minute, (title or "").strip()[:100])
            if bot.calendar:
                bot.calendar.poke()
            await inter.response.send_message(f"{p['name']} is on the calendar every {day.name} at {time.strip()} "
                                              f"({bot.settings.timezone}).", ephemeral=True)

        @friend.command(name="unschedule", description="Remove someone's hand-set weekly times")
        async def friend_unschedule(inter: discord.Interaction, name: str):
            if not await managers_only(inter):
                return
            p = bot.store.person(name)
            n = bot.store.clear_manual(p["id"]) if p else 0
            if bot.calendar:
                bot.calendar.poke()
            await inter.response.send_message(f"Removed {n} weekly time(s) for {name}." if p else f"No one called {name}.",
                                              ephemeral=True)

        @friend.command(name="hide", description="Keep someone off the calendar and recap (still watched)")
        async def friend_hide(inter: discord.Interaction, name: str, hidden: bool = True):
            if not await managers_only(inter):
                return
            p = bot.store.person(name)
            if not p:
                await inter.response.send_message(f"No one called {name}.", ephemeral=True)
                return
            bot.store.set_hidden(p["id"], hidden)
            if bot.calendar:
                bot.calendar.poke()
            await inter.response.send_message(f"{p['name']} is {'off' if hidden else 'back on'} the calendar.",
                                              ephemeral=True)

        @friend.command(name="x", description="Their X / Twitter handle, so the copy-ready X text tags them")
        @app_commands.describe(handle="Like @luluteala; - removes it")
        async def friend_x(inter: discord.Interaction, name: str, handle: str):
            if not await managers_only(inter):
                return
            p = bot.store.person(name)
            if not p:
                await inter.response.send_message(f"No one called {name}.", ephemeral=True)
                return
            h = "" if handle.strip() in ("-", "none", "") else handle.strip().lstrip("@")
            if h and not re.fullmatch(r"\w{1,15}", h):
                await inter.response.send_message("That isn't an X handle (letters, numbers and _ only, 15 at most).",
                                                  ephemeral=True)
                return
            bot.store.set_x(p["id"], h)
            await inter.response.send_message(f"{p['name']} is tagged as @{h} in the X text." if h else
                                              f"{p['name']} is written by name in the X text.", ephemeral=True)

        tree.add_command(friend)

        calendar = app_commands.Group(name="calendar", description="The weekly stream calendar")
        style_choices = [app_commands.Choice(name=label, value=k) for k, (label, _) in X_STYLES.items()]
        week_choices = [app_commands.Choice(name="This week", value=0), app_commands.Choice(name="Next week", value=1),
                        app_commands.Choice(name="Last week (recap)", value=-1)]

        @calendar.command(name="view", description="The calendar picture and list (only you see it)")
        @app_commands.choices(group=group_choices, week=week_choices)
        async def calendar_view(inter: discord.Interaction, group: Optional[app_commands.Choice[str]] = None,
                                week: Optional[app_commands.Choice[int]] = None):
            if not bot.calendar:
                await inter.response.send_message("The calendar isn't running.", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            from .calendar_post import embed as cal_embed
            w, png = await bot.calendar.view_png(group.value if group else "flavr", week.value if week else 0)
            name = "calendar.png"
            files = [discord.File(io.BytesIO(png), filename=name)] if png else []
            await inter.followup.send(embed=make_embed(cal_embed(w), name if png else ""), files=files,
                                      ephemeral=True, allowed_mentions=NO_PINGS)

        @calendar.command(name="post", description="Post a fresh calendar now (owner or Manage Server)")
        @app_commands.choices(group=group_choices)
        async def calendar_post_now(inter: discord.Interaction, group: Optional[app_commands.Choice[str]] = None):
            if not await managers_only(inter):
                return
            g = group.value if group else "flavr"
            if not bot.calendar or not bot.store.get(f"calendar_channel.{g}"):
                await inter.response.send_message("Pick its channel first: /setup calendar-channel.", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            async with bot.calendar._lock:
                await bot.calendar.refresh(g, force_new=True)
            await inter.followup.send("Posted.", ephemeral=True)

        @calendar.command(name="refresh", description="Fill a week in from the channels' past streams (VODs)")
        @app_commands.choices(week=week_choices[::2])
        async def calendar_refresh(inter: discord.Interaction, week: Optional[app_commands.Choice[int]] = None):
            if not await managers_only(inter):
                return
            if not bot.calendar:
                await inter.response.send_message("The calendar isn't running.", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            off = week.value if week else -1
            added = await bot.calendar.ensure_backfill(bot.calendar.week_of(off), force=True)
            await inter.followup.send(f"Found {added} stream(s) the bot hadn't seen. Twitch keeps past broadcasts "
                                      "7 days (60 for partners); Kick has no list of past streams.", ephemeral=True)

        @calendar.command(name="share", description="DMs you copy-ready text for other Discords and X")
        @app_commands.choices(group=group_choices, week=week_choices, style=style_choices)
        @app_commands.describe(style="X layout this time (default: /setup share-style)")
        async def calendar_share(inter: discord.Interaction, group: Optional[app_commands.Choice[str]] = None,
                                 week: Optional[app_commands.Choice[int]] = None,
                                 style: Optional[app_commands.Choice[str]] = None):
            if not await owner_only(inter):
                return
            if not bot.calendar:
                await inter.response.send_message("The calendar isn't running.", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            ok = await bot.calendar.share(group.value if group else "flavr", week.value if week else 0,
                                          style.value if style else "")
            await inter.followup.send("Sent to your DMs." if ok else "Couldn't DM you. Discord → this server → Privacy "
                                      "Settings → allow direct messages.", ephemeral=True)

        tree.add_command(calendar)

        setup = app_commands.Group(name="setup", description="Bot settings (owner or Manage Server)")

        @setup.command(name="live-channel", description="Where go-live posts for a group go")
        @app_commands.choices(group=group_choices)
        async def setup_live(inter: discord.Interaction, group: app_commands.Choice[str], channel: discord.TextChannel):
            if not await managers_only(inter):
                return
            bot.store.put(f"live_channel.{group.value}", str(channel.id))
            await inter.response.send_message(f"{group.name} go-live posts go to {channel.mention}.", ephemeral=True)

        @setup.command(name="calendar-channel", description="Where the weekly calendar and recap go")
        @app_commands.choices(group=group_choices)
        async def setup_calendar(inter: discord.Interaction, group: app_commands.Choice[str], channel: discord.TextChannel):
            if not await managers_only(inter):
                return
            bot.store.put(f"calendar_channel.{group.value}", str(channel.id))
            bot.store.put(f"calendar_post.{group.value}", {})       # a fresh post in the new channel
            if bot.calendar:
                bot.calendar.poke()
            await inter.response.send_message(f"The {group.name} calendar goes to {channel.mention}; the first post "
                                              "comes within a minute.", ephemeral=True)

        @setup.command(name="share-style", description="Default layout of the copy-ready X text")
        @app_commands.choices(style=[app_commands.Choice(name=label, value=k) for k, (label, _) in X_STYLES.items()])
        @app_commands.describe(bold_days="Day headings in bold-looking letters (screen readers skip them)",
                               hashtags="A line at the end, like #FlaVR #vtuber (- removes it)",
                               tag_people="Turn the X handles from /friend x into real tags (default off)")
        async def setup_share_style(inter: discord.Interaction, style: app_commands.Choice[str],
                                    bold_days: Optional[bool] = None, hashtags: Optional[str] = None,
                                    tag_people: Optional[bool] = None):
            if not await owner_only(inter):
                return
            bot.store.put("share_x_style", style.value)
            if bold_days is not None:
                bot.store.put("share_x_bold", bold_days)
            if tag_people is not None:
                bot.store.put("share_x_tag", tag_people)
            if hashtags is not None:
                tags = " ".join(t if t.startswith("#") else "#" + t for t in hashtags.replace(",", " ").split()
                                if t not in ("-", "none"))
                bot.store.put("share_x_tags", tags[:200])
            extra = []
            if bot.store.get("share_x_bold"):
                extra.append("bold day headings")
            if bot.store.get("share_x_tags"):
                extra.append(f"ending with {bot.store.get('share_x_tags')}")
            extra.append("tagging people's X handles" if bot.store.get("share_x_tag") else "nobody tagged")
            await inter.response.send_message(f"X text: {X_STYLES[style.value][1]}" + (f"; {', '.join(extra)}" if extra
                                              else "") + ".", ephemeral=True)

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
