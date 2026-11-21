"""
Server care, round 2: the rest of the server care menu the owner approved on 2026-11-17 (plan doc
"Discord bot: next features plan"). Round 1 (role buttons, mod log, scam links, backups) is
flavrbot/server_care.py; this file adds:

  Welcome          /welcome setup channel [message] [starter_role] [dm]: a welcome post (or DM) for
                   each new member with the rules channel and calendar linked; /welcome rules-button
                   posts an "I read the rules" button that hands out the starter role (custom id
                   ``flavr-rules``, answered in ``on_interaction`` so it survives restarts).
  Raid protection  RAID_JOINS joins within RAID_WINDOW_SEC → lockdown: 30 s slowmode on every channel
                   everyone can type in, server invites paused, the owner DMed and the mod log told.
                   /lockdown off puts every channel's old slowmode back and opens invites again;
                   /lockdown on does it by hand.
  Warnings         /warn member reason (DMs them, logs it); /warnings member [clear]. Every
                   WARN_LIMIT-th warning inside WARN_DAYS days times them out for WARN_TIMEOUT_MIN.
  Modmail          a member DMs the bot → a thread in the modmail channel (/setup modmail). What mods
                   write in that thread goes back to the member as a DM from the bot (lines starting
                   with // stay among the mods); /modmail close ends it.
  Live mode        while the owner is live (Stream Core, or FlaVR Promo's go-live watcher through
                   PROMO_URL): slowmode on the channels picked with /live-mode add, and a voice channel renamed "🔴 LIVE: game"
                   (/live-mode voice). Everything goes back when the stream ends.
  Starboard        /setup starboard channel [emoji] [count]: a message that gets ``count`` of that emoji
                   is reposted there once, and its count kept up to date.
  Member moments   /setup moments channel [role]: server anniversaries and opt-in birthdays
                   (/birthday set month day) posted at MOMENTS_HOUR, with ``role`` for the day.
  Growth stats     joins and leaves counted per week (Sunday start); the owner gets last week's numbers
                   on Sunday at MOMENTS_HOUR (/pings kind ``growth``); /growth shows the last 4 weeks.

Not here yet: AutoMod rules from Core's blocked words (needs a new Core message, which would change
docs/CORE_LINK.md, flavrbot/core_link.py and Core's companion_link.py together) and putting a backup
back.

Every post uses allowed_mentions none, so a welcome or anniversary shows the member's name as a
mention without pinging anyone.

Who calls it: flavrbot/bot.py makes one ``Community`` (``bot.community``), forwards Discord events to
it and calls ``register`` for its slash commands; flavrbot/__main__.py runs ``run_daily`` and calls
``live_mode`` when the owner's stream starts or ends. Settings: RAID_JOINS, RAID_WINDOW_SEC,
WARN_LIMIT, WARN_DAYS, WARN_TIMEOUT_MIN, MOMENTS_HOUR (flavrbot/config.py, .env.example).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

log = logging.getLogger("flavrbot.community")
NO_PINGS = discord.AllowedMentions.none()

RULES_BUTTON = "flavr-rules"           # custom id of the "I read the rules" button
LOCK_SLOWMODE = 30                     # seconds of slowmode a lockdown puts on open channels
DEFAULT_WELCOME = "Welcome {member} to **{server}**! You're member #{count}."
MAX_STARRED = 3000                     # starboard: message ids remembered (oldest dropped)
COLOUR = 0x2AE0D4


# ── pure helpers (tested in tests/test_community.py) ─────────────────────────
def welcome_text(template: str, member_mention: str, name: str, server: str, count: int,
                 rules: str = "", calendar: str = "") -> str:
    """The welcome post: {member} {name} {server} {count} filled in, then a line with the rules
    channel and the calendar channel when those are known (channel mentions, ``<#id>``)."""
    text = (template or DEFAULT_WELCOME)
    for k, v in (("{member}", member_mention), ("{name}", name), ("{server}", server), ("{count}", str(count))):
        text = text.replace(k, v)
    extra = [x for x in (f"📜 Rules: {rules}" if rules else "", f"🗓️ Stream calendar: {calendar}" if calendar else "") if x]
    return (text + ("\n" + " · ".join(extra) if extra else ""))[:2000]


class RaidWatch:
    """Joins per server; ``join`` says True once ``limit`` joins land inside ``window`` seconds."""

    def __init__(self, limit: int = 8, window: float = 60.0) -> None:
        self.limit, self.window = max(2, limit), max(5.0, window)
        self._joins: Dict[int, List[float]] = {}

    def join(self, guild_id: int, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        rows = [t for t in self._joins.get(guild_id, []) if now - t < self.window] + [now]
        self._joins[guild_id] = rows
        if len(rows) >= self.limit:
            self._joins[guild_id] = []          # one alarm per burst
            return True
        return False


def active_warnings(warns: List[Dict[str, Any]], now: float, days: int) -> List[Dict[str, Any]]:
    """Warnings from the last ``days`` days (older ones stay in the history but don't count)."""
    return [w for w in warns if now - float(w.get("at", 0)) < days * 86400]


def warn_triggers_timeout(active: int, limit: int) -> bool:
    """Every ``limit``-th active warning (3rd, 6th, ...) brings a timeout."""
    return limit > 0 and active > 0 and active % limit == 0


def week_key(d: date) -> str:
    """The Sunday that starts ``d``'s week (the calendar's week, Sunday to Saturday), as YYYY-MM-DD."""
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def birthday_today(md: str, today: date) -> bool:
    """"MM-DD" is today; a Feb 29 birthday counts on Feb 28 in other years."""
    if md == today.strftime("%m-%d"):
        return True
    leap = today.year % 4 == 0 and (today.year % 100 != 0 or today.year % 400 == 0)
    return md == "02-29" and not leap and today.strftime("%m-%d") == "02-28"


def anniversaries(members: List[Tuple[int, Optional[datetime]]], today: date) -> List[Tuple[int, int]]:
    """[(member id, years)] for members who joined on this day in an earlier year."""
    out = []
    for mid, joined in members:
        if joined is None:
            continue
        j = joined.date()
        years = today.year - j.year
        if years >= 1 and birthday_today(f"{j.month:02d}-{j.day:02d}", today):      # Feb 29 joins: Feb 28
            out.append((mid, years))
    return out


def parse_birthday(month: int, day: int) -> str:
    """"MM-DD" or "" when the date can't exist (Feb 29 is allowed)."""
    try:
        date(2024, month, day)
    except ValueError:
        return ""
    return f"{month:02d}-{day:02d}"


# ── the live part ────────────────────────────────────────────────────────────
class Community:
    """Round 2 server care for every server the bot is in. Per-server settings in the kv store:
    ``welcome.<g>`` {channel, text, role, dm}, ``lockdown.<g>`` {on, since, slowmodes, invites},
    ``warns.<g>`` {user: [{at, by, reason}]}, ``modmail.<g>`` {channel, threads: {user: thread}},
    ``live_mode.<g>`` {channels, slowmode, voice, voice_name, on}, ``starboard.<g>`` {channel, emoji,
    count, posts: {message: star post}}, ``moments.<g>`` {channel, role, done, holders},
    ``birthdays.<g>`` {user: "MM-DD"}, ``growth.<g>`` {week: {joined, left}}."""

    def __init__(self, bot: Any, store: Any, tz: str = "America/New_York", raid_joins: int = 8,
                 raid_window: int = 60, warn_limit: int = 3, warn_days: int = 30, warn_timeout_min: int = 1440,
                 moments_hour: int = 10, ping: Optional[Callable[[str, str], Awaitable[None]]] = None):
        self.bot = bot
        self.store = store
        self.tz = ZoneInfo(tz)
        self.raids = RaidWatch(raid_joins, raid_window)
        self.warn_limit, self.warn_days, self.warn_timeout_min = warn_limit, warn_days, warn_timeout_min
        self.moments_hour = moments_hour
        self.ping = ping                    # flavrbot/pings.py Pinger.ping (kind, text): the weekly growth DM

    # settings
    def get(self, name: str, guild_id: int) -> Dict[str, Any]:
        return dict(self.store.get(f"{name}.{guild_id}") or {})

    def put(self, name: str, guild_id: int, value: Dict[str, Any]) -> None:
        self.store.put(f"{name}.{guild_id}", value)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    async def log(self, guild: Any, title: str, lines: List[str], user: Any = None) -> None:
        """Into the round 1 mod log (flavrbot/server_care.py), when one is set."""
        await self.bot.care.post(guild, "mod", title, lines, user=user)

    # ── joins and leaves ──
    async def on_member_join(self, member: discord.Member) -> None:
        g = member.guild
        self._count(g.id, "joined")
        if self.raids.join(g.id) and not self.get("lockdown", g.id).get("on"):
            await self.lockdown(g, True, why=f"{self.raids.limit} joins within {int(self.raids.window)} s")
        await self.welcome(member)

    async def on_member_remove(self, member: discord.Member) -> None:
        self._count(member.guild.id, "left")

    def _count(self, guild_id: int, what: str) -> None:
        grow = self.get("growth", guild_id)
        wk = week_key(self.today())
        row = dict(grow.get(wk) or {"joined": 0, "left": 0})
        row[what] = int(row.get(what, 0)) + 1
        grow[wk] = row
        for old in sorted(grow)[:-12]:          # keep 12 weeks
            grow.pop(old)
        self.put("growth", guild_id, grow)

    def growth_lines(self, guild: Any, weeks: int = 4) -> List[str]:
        grow = self.get("growth", guild.id)
        out = []
        start = date.fromisoformat(week_key(self.today()))
        for i in range(weeks):
            wk = (start - timedelta(days=7 * i)).isoformat()
            row = grow.get(wk) or {"joined": 0, "left": 0}
            label = "This week" if i == 0 else f"Week of {date.fromisoformat(wk):%b} {date.fromisoformat(wk).day}"
            out.append(f"{label}: +{row['joined']} joined, -{row['left']} left "
                       f"({row['joined'] - row['left']:+d})")
        return out

    # ── welcome ──
    async def welcome(self, member: discord.Member) -> None:
        w = self.get("welcome", member.guild.id)
        if not w.get("channel") and not w.get("dm"):
            return
        g = member.guild
        rules = f"<#{w['rules_channel']}>" if w.get("rules_channel") else \
            (g.rules_channel.mention if getattr(g, "rules_channel", None) else "")
        # the FlaVR calendar channel as it was picked before the split (kv calendar_channel.flavr); the
        # calendar itself is FlaVR Promo's now, so a channel picked there later isn't seen here
        cal = self.store.get("calendar_channel.flavr")
        text = welcome_text(w.get("text") or "", member.mention, member.display_name, g.name, g.member_count or 0,
                            rules, f"<#{cal}>" if cal else "")
        if w.get("dm"):
            try:
                await member.send(text, allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass                            # their DMs are closed; the channel post still goes
        ch = g.get_channel(int(w["channel"])) if w.get("channel") else None
        if ch is not None:
            try:
                await ch.send(text, allowed_mentions=NO_PINGS)
            except discord.HTTPException as e:
                log.warning("welcome post failed: %s", e)

    async def post_rules_button(self, channel: discord.TextChannel, text: str) -> str:
        w = self.get("welcome", channel.guild.id)
        if not w.get("role"):
            return "Pick the starter role first: /welcome setup ... starter_role."
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="I read the rules", emoji="✅", style=discord.ButtonStyle.success,
                                        custom_id=RULES_BUTTON))
        e = discord.Embed(description=text or "Read the rules above, then press the button to unlock the server.",
                          colour=COLOUR)
        try:
            await channel.send(embed=e, view=view, allowed_mentions=NO_PINGS)
        except discord.HTTPException as ex:
            return f"Couldn't post in {channel.mention}: {ex.text}"
        w["rules_channel"] = str(channel.id)
        self.put("welcome", channel.guild.id, w)
        return ""

    async def on_interaction(self, inter: discord.Interaction) -> None:
        cid = (inter.data or {}).get("custom_id", "") if inter.type == discord.InteractionType.component else ""
        if cid != RULES_BUTTON or inter.guild is None:
            return
        role_id = self.get("welcome", inter.guild.id).get("role")
        role = inter.guild.get_role(int(role_id)) if role_id else None
        member = inter.user if isinstance(inter.user, discord.Member) else inter.guild.get_member(inter.user.id)
        if role is None or member is None:
            await inter.response.send_message("The starter role isn't set up any more. Ask a moderator.", ephemeral=True)
            return
        if role in member.roles:
            await inter.response.send_message("You're already in. 👍", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Read the rules")
            text = f"Welcome in! You now have **{role.name}**."
        except discord.HTTPException:
            text = "I'm not allowed to hand out that role. A moderator needs to move my role above it."
        await inter.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ── raid protection ──
    async def lockdown(self, guild: Any, on: bool, why: str = "", by: str = "") -> List[str]:
        """Switch lockdown on or off; returns plain lines of what was done (or couldn't be)."""
        state = self.get("lockdown", guild.id)
        done: List[str] = []
        if on:
            if state.get("on"):
                return ["Already locked down."]
            slow: Dict[str, int] = {}
            everyone = guild.default_role
            for ch in guild.text_channels:
                if not ch.permissions_for(everyone).send_messages or (ch.slowmode_delay or 0) >= LOCK_SLOWMODE:
                    continue
                before = ch.slowmode_delay or 0          # read first: put back by /lockdown off
                try:
                    await ch.edit(slowmode_delay=LOCK_SLOWMODE, reason=f"FlaVR Bot lockdown: {why or by}")
                    slow[str(ch.id)] = before
                except discord.HTTPException:
                    pass
            done.append(f"Slowmode {LOCK_SLOWMODE} s on {len(slow)} channel(s)")
            paused_before = "INVITES_DISABLED" in getattr(guild, "features", [])
            if not paused_before:
                try:
                    await guild.edit(invites_disabled=True, reason="FlaVR Bot lockdown")
                    done.append("Invites paused")
                except discord.HTTPException:
                    done.append("Couldn't pause invites (the bot needs Manage Server)")
            self.put("lockdown", guild.id, {"on": True, "since": time.time(), "slowmodes": slow,
                                            "invites_were_paused": paused_before, "why": why or by})
            await self.log(guild, "🔒 Lockdown on", [f"**Why:** {why or 'by hand, ' + by}"] + done +
                           ["Undo with /lockdown off."])
            if why:                             # automatic: tell the owner at once (urgent, skips quiet hours)
                try:
                    await self.bot.dm_owner(f"🔒 **{guild.name}** locked down: {why}. " + "; ".join(done) +
                                            ". /lockdown off when it's over.")
                except discord.HTTPException:
                    pass
            return done
        if not state.get("on"):
            return ["There's no lockdown on."]
        back = 0
        for cid, old in (state.get("slowmodes") or {}).items():
            ch = guild.get_channel(int(cid))
            if ch is None:
                continue
            try:
                await ch.edit(slowmode_delay=int(old), reason="FlaVR Bot lockdown off")
                back += 1
            except discord.HTTPException:
                pass
        done.append(f"Slowmode back to normal on {back} channel(s)")
        if not state.get("invites_were_paused"):
            try:
                await guild.edit(invites_disabled=False, reason="FlaVR Bot lockdown off")
                done.append("Invites open again")
            except discord.HTTPException:
                done.append("Couldn't reopen invites (Server Settings → Invites → resume them by hand)")
        self.put("lockdown", guild.id, {"on": False})
        await self.log(guild, "🔓 Lockdown off", done + ([f"By {by}"] if by else []))
        return done

    # ── warnings ──
    def warnings(self, guild_id: int, user_id: int) -> List[Dict[str, Any]]:
        return list(self.get("warns", guild_id).get(str(user_id)) or [])

    async def warn(self, guild: Any, member: discord.Member, by: Any, reason: str) -> str:
        all_ = self.get("warns", guild.id)
        rows = list(all_.get(str(member.id)) or [])
        now = time.time()
        rows.append({"at": now, "by": str(by.id), "by_name": str(by), "reason": reason[:300]})
        all_[str(member.id)] = rows[-50:]
        self.put("warns", guild.id, all_)
        active = len(active_warnings(rows, now, self.warn_days))
        timed = ""
        if warn_triggers_timeout(active, self.warn_limit) and self.warn_timeout_min > 0:
            try:
                await member.timeout(timedelta(minutes=self.warn_timeout_min),
                                     reason=f"FlaVR Bot: {active} warnings in {self.warn_days} days")
                timed = f" and a {self.warn_timeout_min // 60 or 1} h timeout" if self.warn_timeout_min >= 60 else \
                    f" and a {self.warn_timeout_min} min timeout"
            except discord.HTTPException:
                timed = " (the timeout failed: the bot needs Timeout Members, above their role)"
        try:
            await member.send(f"⚠️ You got a warning in **{guild.name}**: {reason}" +
                              (f"\nThat's {active} in {self.warn_days} days{timed}." if active > 1 else ""),
                              allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass
        await self.log(guild, "Warning", [f"{member.mention} warned by {by.mention}: {reason}",
                                          f"Active warnings: {active} (last {self.warn_days} days){timed}"], user=member)
        return f"Warned {member.display_name} ({active} active){timed}."

    # ── modmail ──
    async def on_message(self, m: discord.Message) -> None:
        if m.author.bot:
            return
        if m.guild is None:
            await self._mail_in(m)
        elif isinstance(m.channel, discord.Thread):
            await self._mail_out(m)

    def _mail_guild(self, user: discord.abc.User) -> Any:
        """The server whose modmail this person's DM goes to (the first one that has modmail and them)."""
        for g in self.bot.guilds:
            if self.get("modmail", g.id).get("channel") and g.get_member(user.id) is not None:
                return g
        return None

    async def _mail_in(self, m: discord.Message) -> None:
        if m.author.id == self.bot.settings.owner_id:
            return                              # the owner's DMs with the bot are for pings, not modmail
        g = self._mail_guild(m.author)
        if g is None:
            return
        mm = self.get("modmail", g.id)
        threads = dict(mm.get("threads") or {})
        thread = g.get_thread(int(threads[str(m.author.id)])) if threads.get(str(m.author.id)) else None
        if thread is None and threads.get(str(m.author.id)):
            try:
                thread = await g.fetch_channel(int(threads[str(m.author.id)]))
            except discord.HTTPException:
                thread = None
        first = thread is None
        if first:
            ch = g.get_channel(int(mm["channel"]))
            if ch is None:
                return
            try:
                thread = await ch.create_thread(name=f"✉️ {m.author.name}"[:90], type=discord.ChannelType.public_thread,
                                                auto_archive_duration=10080, reason="Modmail")
            except discord.HTTPException as e:
                log.warning("modmail thread failed: %s", e)
                return
            threads[str(m.author.id)] = str(thread.id)
            mm["threads"] = threads
            self.put("modmail", g.id, mm)
            member = g.get_member(m.author.id)
            await thread.send(f"New modmail from {m.author.mention} ({m.author}, {m.author.id})"
                              + (f", joined <t:{int(member.joined_at.timestamp())}:R>" if member and member.joined_at else "")
                              + ". Write here to answer them; lines starting with // stay among the mods. "
                                "/modmail close ends it.", allowed_mentions=NO_PINGS)
        files = "\n".join(a.url for a in m.attachments)
        await thread.send(f"**{m.author.display_name}:** {m.content}"[:1900] + (f"\n{files}" if files else ""),
                          allowed_mentions=NO_PINGS)
        if first:
            try:
                await m.channel.send(f"📨 Your message went to the mods of **{g.name}**. They'll answer here.",
                                     allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass

    def _mail_user(self, thread: discord.Thread) -> Optional[int]:
        threads = self.get("modmail", thread.guild.id).get("threads") or {}
        for uid, tid in threads.items():
            if tid == str(thread.id):
                return int(uid)
        return None

    async def _mail_out(self, m: discord.Message) -> None:
        uid = self._mail_user(m.channel)
        if uid is None or (m.content or "").startswith("//"):
            return
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            files = "\n".join(a.url for a in m.attachments)
            await user.send(f"**Mods of {m.guild.name}:** {m.content}"[:1900] + (f"\n{files}" if files else ""),
                            allowed_mentions=NO_PINGS)
            await m.add_reaction("📨")
        except discord.HTTPException:
            await m.channel.send("Couldn't DM them (they left, or their DMs are closed).", allowed_mentions=NO_PINGS)

    async def close_mail(self, thread: discord.Thread) -> bool:
        uid = self._mail_user(thread)
        if uid is None:
            return False
        mm = self.get("modmail", thread.guild.id)
        threads = dict(mm.get("threads") or {})
        threads.pop(str(uid), None)
        mm["threads"] = threads
        self.put("modmail", thread.guild.id, mm)
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(f"The mods of **{thread.guild.name}** closed this conversation. DM me again any time.",
                            allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass
        return True

    # ── live mode ──
    async def live_mode(self, live: bool, game: str = "") -> None:
        """The owner's stream started (``live``) or ended. Called from flavrbot/__main__.py for FlaVR Promo's
        go-live watcher (flavrbot/promo_link.py on_owner) and for Stream Core's stream messages; doing it twice is harmless."""
        for g in list(self.bot.guilds):
            lm = self.get("live_mode", g.id)
            if not lm.get("channels") and not lm.get("voice"):
                continue
            if live == bool(lm.get("on")) and not (live and game and game != lm.get("game")):
                continue
            if live and not lm.get("on"):
                old: Dict[str, int] = {}
                for cid in lm.get("channels") or []:
                    ch = g.get_channel(int(cid))
                    if ch is None:
                        continue
                    old[cid] = ch.slowmode_delay or 0
                    try:
                        await ch.edit(slowmode_delay=int(lm.get("slowmode") or 10), reason="FlaVR Bot: stream started")
                    except discord.HTTPException:
                        pass
                lm["old"] = old
            vc = g.get_channel(int(lm["voice"])) if lm.get("voice") else None
            if vc is not None:
                if live:
                    if not lm.get("on"):
                        lm["voice_name"] = vc.name          # put back when the stream ends
                    name = (f"🔴 LIVE: {game}" if game else "🔴 LIVE")[:100]
                    if vc.name != name:
                        try:
                            await vc.edit(name=name, reason="FlaVR Bot: stream live")
                        except discord.HTTPException:
                            pass
                elif lm.get("voice_name") and vc.name != lm["voice_name"]:
                    try:
                        await vc.edit(name=lm["voice_name"], reason="FlaVR Bot: stream ended")
                    except discord.HTTPException:
                        pass
            if not live:
                for cid, slow in (lm.get("old") or {}).items():
                    ch = g.get_channel(int(cid))
                    if ch is not None:
                        try:
                            await ch.edit(slowmode_delay=int(slow), reason="FlaVR Bot: stream ended")
                        except discord.HTTPException:
                            pass
                lm.pop("old", None)
                lm.pop("voice_name", None)
            lm["on"], lm["game"] = live, game if live else ""
            self.put("live_mode", g.id, lm)

    # ── starboard ──
    async def on_raw_reaction_add(self, p: discord.RawReactionActionEvent) -> None:
        if p.guild_id is None:
            return
        sb = self.get("starboard", p.guild_id)
        if not sb.get("channel") or str(p.emoji) != (sb.get("emoji") or "⭐") or str(p.channel_id) == sb["channel"]:
            return
        g = self.bot.get_guild(p.guild_id)
        ch = g.get_channel_or_thread(p.channel_id) if g else None
        board = g.get_channel(int(sb["channel"])) if g else None
        if ch is None or board is None:
            return
        try:
            msg = await ch.fetch_message(p.message_id)
        except discord.HTTPException:
            return
        n = next((r.count for r in msg.reactions if str(r.emoji) == str(p.emoji)), 0)
        if n < int(sb.get("count") or 5):
            return
        posts = dict(sb.get("posts") or {})
        head = f"{sb.get('emoji') or '⭐'} **{n}** · {ch.mention}"
        if str(msg.id) in posts:
            try:
                await board.get_partial_message(int(posts[str(msg.id)])).edit(content=head, allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass
            return
        e = discord.Embed(description=(msg.content or "")[:3500] + f"\n\n[Jump to the message]({msg.jump_url})",
                          colour=0xF8CF33, timestamp=msg.created_at)
        e.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
        pic = next((a.url for a in msg.attachments if (a.content_type or "").startswith("image/")), None)
        if pic:
            e.set_image(url=pic)
        try:
            star = await board.send(head, embed=e, allowed_mentions=NO_PINGS)
        except discord.HTTPException as ex:
            log.warning("starboard post failed: %s", ex)
            return
        posts[str(msg.id)] = str(star.id)
        if len(posts) > MAX_STARRED:
            posts = dict(list(posts.items())[-MAX_STARRED:])
        sb["posts"] = posts
        self.put("starboard", p.guild_id, sb)

    # ── member moments + weekly growth ──
    async def moments(self, guild: Any, today: date) -> None:
        mo = self.get("moments", guild.id)
        ch = guild.get_channel(int(mo["channel"])) if mo.get("channel") else None
        if ch is None or mo.get("done") == today.isoformat():
            return
        role = guild.get_role(int(mo["role"])) if mo.get("role") else None
        for uid in mo.get("holders") or []:     # yesterday's birthday role comes off
            m = guild.get_member(int(uid))
            if m is not None and role is not None and role in m.roles:
                try:
                    await m.remove_roles(role, reason="Birthday over")
                except discord.HTTPException:
                    pass
        holders = []
        bdays = self.get("birthdays", guild.id)
        for uid, md in bdays.items():
            m = guild.get_member(int(uid))
            if m is None or not birthday_today(md, today):
                continue
            try:
                await ch.send(f"🎂 Happy birthday, {m.mention}!", allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass
            if role is not None:
                try:
                    await m.add_roles(role, reason="Birthday")
                    holders.append(uid)
                except discord.HTTPException:
                    pass
        for uid, years in anniversaries([(m.id, m.joined_at) for m in guild.members if not m.bot], today):
            m = guild.get_member(uid)
            try:
                await ch.send(f"🎉 {m.mention} joined **{guild.name}** {years} year{'s' if years > 1 else ''} ago today!",
                              allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass
        mo.update(done=today.isoformat(), holders=holders)
        self.put("moments", guild.id, mo)

    async def growth_ping(self, guild: Any, today: date) -> None:
        """Sunday: last week's joins and leaves to the owner (pings kind "growth"), once."""
        key = f"growth_sent.{guild.id}"
        if today.weekday() != 6 or self.store.get(key) == today.isoformat() or self.ping is None:
            return
        last = (date.fromisoformat(week_key(today)) - timedelta(days=7)).isoformat()
        row = self.get("growth", guild.id).get(last) or {"joined": 0, "left": 0}
        self.store.put(key, today.isoformat())
        await self.ping("growth", f"📈 **{guild.name}** last week: +{row['joined']} joined, -{row['left']} left "
                                  f"({row['joined'] - row['left']:+d}); {guild.member_count} members now.")

    async def run_daily(self) -> None:
        """Every 10 minutes: at or after MOMENTS_HOUR (TZ), each day's moments and Sunday's growth DM, once."""
        while True:
            now = datetime.now(self.tz)
            if now.hour >= self.moments_hour:
                for g in list(self.bot.guilds):
                    try:
                        await self.moments(g, now.date())
                        await self.growth_ping(g, now.date())
                    except Exception:               # noqa: BLE001  one bad day mustn't stop tomorrow's
                        log.exception("member moments failed")
            await asyncio.sleep(600)


def missing_more(guild: Any, me: Any, gateway: Dict[str, bool], community: Community) -> List[str]:
    """``/setup check``: what the round 2 parts that are switched on still need."""
    out = []
    p = me.guild_permissions
    if not p.manage_channels:
        out.append("Bot's role: allow **Manage Channels** (lockdown slowmode, live mode, voice channel name).")
    if not p.manage_guild:
        out.append("Bot's role: allow **Manage Server** (pausing invites in a lockdown).")
    mm = community.get("modmail", guild.id)
    if mm.get("channel"):
        ch = guild.get_channel(int(mm["channel"]))
        if ch is None:
            out.append("The modmail channel is gone: /setup modmail again.")
        else:
            cp = ch.permissions_for(me)
            if not (cp.create_public_threads and cp.send_messages_in_threads):
                out.append(f"In #{ch.name} allow the bot **Create Public Threads** and **Send Messages in Threads**.")
        if not gateway.get("content"):
            out.append("Modmail needs **Message Content Intent** to read the mods' answers.")
    if community.get("moments", guild.id).get("channel") and not gateway.get("members"):
        out.append("Anniversaries and birthdays need **Server Members Intent**.")
    return out


# ── slash commands ───────────────────────────────────────────────────────────
def register(tree: app_commands.CommandTree, bot: Any, managers_only: Callable[[discord.Interaction], Awaitable[bool]]
             ) -> None:
    """Adds /welcome, /lockdown, /warn, /warnings, /modmail, /live-mode, /birthday, /growth and the
    /setup modmail | starboard | moments subcommands (the /setup group comes from flavrbot/bot.py)."""
    c: Community = bot.community

    welcome = app_commands.Group(name="welcome", description="Welcome posts and the rules button (owner or Manage Server)")

    @welcome.command(name="setup", description="Where new members are welcomed, and what it says")
    @app_commands.describe(message="{member} {name} {server} {count} are filled in", starter_role="Given by the rules button",
                           dm="Also DM the welcome to them")
    async def welcome_setup(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                            message: Optional[str] = None, starter_role: Optional[discord.Role] = None,
                            dm: Optional[bool] = None):
        if not await managers_only(inter) or inter.guild is None:
            return
        w = c.get("welcome", inter.guild.id)
        if channel is not None:
            w["channel"] = str(channel.id)
        if message is not None:
            w["text"] = "" if message.strip() in ("-", "") else message.replace("\\n", "\n")[:1500]
        if starter_role is not None:
            if starter_role.is_default() or starter_role.managed or starter_role.permissions.administrator \
                    or starter_role.permissions.manage_guild or starter_role.permissions.manage_roles:
                await inter.response.send_message("That role is too powerful to hand out with a button.", ephemeral=True)
                return
            w["role"] = str(starter_role.id)
        if dm is not None:
            w["dm"] = dm
        c.put("welcome", inter.guild.id, w)
        sample = welcome_text(w.get("text") or "", inter.user.mention, inter.user.display_name, inter.guild.name,
                              inter.guild.member_count or 0)
        await inter.response.send_message(
            (f"New members are welcomed in <#{w['channel']}>" if w.get("channel") else "No welcome channel yet")
            + (" and by DM" if w.get("dm") else "") + f". It reads:\n\n{sample}"
            + ("\n\nNext: /welcome rules-button in your rules channel." if w.get("role") and not w.get("rules_channel")
               else ""), ephemeral=True, allowed_mentions=NO_PINGS)

    @welcome.command(name="rules-button", description="Post the 'I read the rules' button (gives the starter role)")
    async def welcome_rules(inter: discord.Interaction, channel: discord.TextChannel, text: Optional[str] = None):
        if not await managers_only(inter) or inter.guild is None:
            return
        problem = await c.post_rules_button(channel, text or "")
        await inter.response.send_message(problem or f"Posted in {channel.mention}.", ephemeral=True)

    @welcome.command(name="off", description="Stop welcoming new members")
    async def welcome_off(inter: discord.Interaction):
        if not await managers_only(inter) or inter.guild is None:
            return
        w = c.get("welcome", inter.guild.id)
        w.pop("channel", None)
        w["dm"] = False
        c.put("welcome", inter.guild.id, w)
        await inter.response.send_message("Welcome posts are off (the rules button still works).", ephemeral=True)

    tree.add_command(welcome)

    @tree.command(name="lockdown", description="Raid lockdown: slowmode everywhere and invites paused (or undo it)")
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
    async def lockdown(inter: discord.Interaction, state: app_commands.Choice[str]):
        if not await managers_only(inter) or inter.guild is None:
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        done = await c.lockdown(inter.guild, state.value == "on", by=str(inter.user))
        await inter.followup.send("\n".join(done), ephemeral=True)

    async def mods_only(inter: discord.Interaction) -> bool:
        perms = getattr(inter.user, "guild_permissions", None)
        if bot.is_owner_id(inter.user) or (perms and (perms.moderate_members or perms.manage_guild)):
            return True
        await inter.response.send_message("Only moderators can use this.", ephemeral=True)
        return False

    @tree.command(name="warn", description="Warn someone (they get a DM; every 3rd warning = a timeout)")
    async def warn(inter: discord.Interaction, member: discord.Member, reason: str):
        if not await mods_only(inter) or inter.guild is None:
            return
        if member.bot or member.id == inter.user.id:
            await inter.response.send_message("Can't warn that one.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        await inter.followup.send(await c.warn(inter.guild, member, inter.user, reason), ephemeral=True)

    @tree.command(name="warnings", description="Someone's warnings (moderators)")
    @app_commands.describe(clear="Wipe their warnings")
    async def warnings(inter: discord.Interaction, member: discord.Member, clear: bool = False):
        if not await mods_only(inter) or inter.guild is None:
            return
        if clear:
            all_ = c.get("warns", inter.guild.id)
            all_.pop(str(member.id), None)
            c.put("warns", inter.guild.id, all_)
            await c.log(inter.guild, "Warnings cleared", [f"{member.mention}'s warnings cleared by {inter.user.mention}"])
            await inter.response.send_message(f"Cleared {member.display_name}'s warnings.", ephemeral=True)
            return
        rows = c.warnings(inter.guild.id, member.id)
        if not rows:
            await inter.response.send_message(f"{member.display_name} has no warnings.", ephemeral=True)
            return
        active = len(active_warnings(rows, time.time(), c.warn_days))
        lines = [f"**{member.display_name}**: {len(rows)} warning(s), {active} in the last {c.warn_days} days"]
        lines += [f"• <t:{int(w['at'])}:d> by {w.get('by_name', '?')}: {w.get('reason', '')}" for w in rows[-15:]]
        await inter.response.send_message("\n".join(lines)[:1900], ephemeral=True, allowed_mentions=NO_PINGS)

    modmail = app_commands.Group(name="modmail", description="Members' DMs to the mods")

    @modmail.command(name="close", description="End this modmail conversation (use it inside its thread)")
    async def modmail_close(inter: discord.Interaction):
        if not await mods_only(inter):
            return
        if not isinstance(inter.channel, discord.Thread) or not await c.close_mail(inter.channel):
            await inter.response.send_message("Use this inside a modmail thread.", ephemeral=True)
            return
        await inter.response.send_message("Closed. They were told; a new DM opens a new thread.")
        try:
            await inter.channel.edit(archived=True)
        except discord.HTTPException:
            pass

    tree.add_command(modmail)

    live = app_commands.Group(name="live-mode", description="Server changes while you're live (owner or Manage Server)")

    @live.command(name="add", description="Slowmode this channel while you're live")
    async def live_add(inter: discord.Interaction, channel: discord.TextChannel):
        if not await managers_only(inter) or inter.guild is None:
            return
        lm = c.get("live_mode", inter.guild.id)
        lm["channels"] = sorted(set(lm.get("channels") or []) | {str(channel.id)})
        c.put("live_mode", inter.guild.id, lm)
        await inter.response.send_message(f"{channel.mention} gets {lm.get('slowmode') or 10} s slowmode while you're live.",
                                          ephemeral=True)

    @live.command(name="remove", description="Stop slowing this channel while you're live")
    async def live_remove(inter: discord.Interaction, channel: discord.TextChannel):
        if not await managers_only(inter) or inter.guild is None:
            return
        lm = c.get("live_mode", inter.guild.id)
        lm["channels"] = [x for x in lm.get("channels") or [] if x != str(channel.id)]
        c.put("live_mode", inter.guild.id, lm)
        await inter.response.send_message(f"{channel.mention} is left alone now.", ephemeral=True)

    @live.command(name="slowmode", description="How slow those channels get while you're live (seconds)")
    async def live_slow(inter: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        if not await managers_only(inter) or inter.guild is None:
            return
        lm = c.get("live_mode", inter.guild.id)
        lm["slowmode"] = seconds
        c.put("live_mode", inter.guild.id, lm)
        await inter.response.send_message(f"Live slowmode: {seconds} s.", ephemeral=True)

    @live.command(name="voice", description="Rename this voice channel to '🔴 LIVE: game' while you're live")
    @app_commands.describe(channel="Leave empty to stop renaming")
    async def live_voice(inter: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
        if not await managers_only(inter) or inter.guild is None:
            return
        lm = c.get("live_mode", inter.guild.id)
        lm["voice"] = str(channel.id) if channel else ""
        c.put("live_mode", inter.guild.id, lm)
        await inter.response.send_message(f"{channel.mention} shows the game while you're live (Discord allows two "
                                          "renames per 10 minutes)." if channel else "No voice channel is renamed now.",
                                          ephemeral=True)

    @live.command(name="show", description="What live mode does")
    async def live_show(inter: discord.Interaction):
        if inter.guild is None:
            return
        lm = c.get("live_mode", inter.guild.id)
        chans = ", ".join(f"<#{x}>" for x in lm.get("channels") or []) or "none"
        await inter.response.send_message(f"While you're live: {lm.get('slowmode') or 10} s slowmode on {chans}; voice "
                                          f"channel: {('<#' + lm['voice'] + '>') if lm.get('voice') else 'none'}. "
                                          f"Now: {'on' if lm.get('on') else 'off'}.", ephemeral=True)

    tree.add_command(live)

    birthday = app_commands.Group(name="birthday", description="Your birthday, posted on the day (opt-in)")

    @birthday.command(name="set", description="Your birthday (no year), posted in the server on the day")
    async def birthday_set(inter: discord.Interaction, month: app_commands.Range[int, 1, 12],
                           day: app_commands.Range[int, 1, 31]):
        if inter.guild is None:
            return
        md = parse_birthday(month, day)
        if not md:
            await inter.response.send_message("That date doesn't exist.", ephemeral=True)
            return
        b = c.get("birthdays", inter.guild.id)
        b[str(inter.user.id)] = md
        c.put("birthdays", inter.guild.id, b)
        await inter.response.send_message(f"Saved: {date(2024, month, day):%B} {day}. /birthday remove takes it off.",
                                          ephemeral=True)

    @birthday.command(name="remove", description="Take your birthday off")
    async def birthday_remove(inter: discord.Interaction):
        if inter.guild is None:
            return
        b = c.get("birthdays", inter.guild.id)
        gone = b.pop(str(inter.user.id), None)
        c.put("birthdays", inter.guild.id, b)
        await inter.response.send_message("Removed." if gone else "You hadn't set one.", ephemeral=True)

    tree.add_command(birthday)

    @tree.command(name="growth", description="Members joined and left, the last 4 weeks (owner or Manage Server)")
    async def growth(inter: discord.Interaction):
        if not await managers_only(inter) or inter.guild is None:
            return
        lines = c.growth_lines(inter.guild) + [f"Members now: {inter.guild.member_count}",
                                                "(counted since this version of the bot started)"]
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    setup = next(cmd for cmd in tree.get_commands() if cmd.name == "setup")

    @setup.command(name="modmail", description="Where members' DMs to the bot open a thread (a mods-only channel)")
    async def setup_modmail(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not await managers_only(inter) or inter.guild is None:
            return
        mm = c.get("modmail", inter.guild.id)
        mm["channel"] = str(channel.id) if channel else ""
        c.put("modmail", inter.guild.id, mm)
        await inter.response.send_message(f"Modmail opens threads in {channel.mention}. Keep it private to mods."
                                          if channel else "Modmail is off.", ephemeral=True)

    @setup.command(name="starboard", description="Repost messages that get enough of one emoji")
    @app_commands.describe(emoji="Default ⭐", count="How many reactions (default 5)")
    async def setup_starboard(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                              emoji: Optional[str] = None, count: Optional[app_commands.Range[int, 2, 100]] = None):
        if not await managers_only(inter) or inter.guild is None:
            return
        sb = c.get("starboard", inter.guild.id)
        sb["channel"] = str(channel.id) if channel else ""
        if emoji:
            sb["emoji"] = emoji.strip()
        if count:
            sb["count"] = count
        c.put("starboard", inter.guild.id, sb)
        await inter.response.send_message(f"Messages with {sb.get('count') or 5} {sb.get('emoji') or '⭐'} go to "
                                          f"{channel.mention}." if channel else "Starboard is off.", ephemeral=True)

    @setup.command(name="moments", description="Where anniversaries and birthdays are posted, and a role for the day")
    async def setup_moments(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                            role: Optional[discord.Role] = None):
        if not await managers_only(inter) or inter.guild is None:
            return
        mo = c.get("moments", inter.guild.id)
        mo["channel"] = str(channel.id) if channel else ""
        if role is not None:
            mo["role"] = str(role.id)
        c.put("moments", inter.guild.id, mo)
        await inter.response.send_message(
            f"Anniversaries and birthdays go to {channel.mention} at {c.moments_hour}:00"
            + (f", with <@&{mo['role']}> for the day" if mo.get("role") else "")
            + ". Members add theirs with /birthday set." if channel else "Member moments are off.",
            ephemeral=True, allowed_mentions=NO_PINGS)


__all__ = ["Community", "register", "missing_more", "welcome_text", "RaidWatch", "active_warnings",
           "warn_triggers_timeout", "week_key", "birthday_today", "anniversaries", "parse_birthday"]
