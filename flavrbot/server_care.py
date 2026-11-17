"""
Server care, starter pack: role buttons, the mod log, scam link blocking and a daily backup of
the server's layout. Plan doc: "Discord bot: next features plan" (server care menu, approved by
the owner 2026-11-17; these four were the suggested first ones).

  Role buttons   /roles add | remove | list | post: one panel of buttons; a click gives or takes
                 that role. Buttons carry the custom id ``flavr-role:<role id>`` and are answered
                 in ``ServerCare.on_interaction``, so they keep working after a bot restart.
  Mod log        /setup mod-log channel: deleted and edited messages, joins (with account age),
                 leaves, kicks, bans, unbans, timeouts (from the audit log, so it says who did it
                 and why), scam blocks, and what changed in the server layout since the last backup.
  Scam links     every server message with a link is checked (``scam_reason``): fake Discord /
                 Steam / Nitro lookalike sites, masked links that pretend to go somewhere else, the
                 same link pasted in 3+ channels within 2 minutes (a hacked account), plus the
                 owner's own block list (/mod block). The message is deleted, the sender gets a
                 timeout of SCAM_TIMEOUT_MIN minutes (0 = none) and a DM saying why, and the mod
                 log gets a copy. People with Manage Messages, the owner and bots are never checked.
  Backup         every day at BACKUP_HOUR (TZ) the server's roles, categories, channels, their
                 permissions, emoji names and who has which role go to
                 /data/backups/<server id>/server-YYYY-MM-DD.json (the last BACKUP_KEEP days are
                 kept). /backup now sends a fresh copy to you. Putting a backup back is NOT built
                 yet: it would be its own careful round.

What it needs from Discord (``/setup check`` lists what's missing):
  * developer portal → your app → Bot → "Message Content Intent" (scam check, deleted message text)
    and "Server Members Intent" (joins / leaves, who has which role). ``gateway_intents`` reads the
    two switches before the bot connects, so a switch left off never stops the bot from starting.
  * the bot's role: Manage Roles (and sitting above the button roles), Manage Messages, Moderate
    Members (timeouts), View Audit Log (who kicked / banned / timed out).

Who calls it: flavrbot/bot.py ``FlavrBot`` makes one ``ServerCare`` and forwards Discord events to
it; flavrbot/__main__.py runs ``ServerCare.run_backups`` and calls ``gateway_intents`` first.
Settings: SCAM_TIMEOUT_MIN, BACKUP_HOUR, BACKUP_KEEP (flavrbot/config.py, .env.example).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import discord

log = logging.getLogger("flavrbot.care")
NO_PINGS = discord.AllowedMentions.none()

ROLE_PREFIX = "flavr-role:"            # custom id of a role button; the role id follows
MAX_ROLE_BUTTONS = 25                  # Discord: 5 rows of 5 buttons per message
NEW_ACCOUNT_DAYS = 7                   # joins from accounts younger than this get a ⚠️ in the mod log
REPEAT_WINDOW = 120.0                  # seconds: the same link in REPEAT_CHANNELS channels inside this = spam
REPEAT_CHANNELS = 3
LOG_COLOURS = {"delete": 0xE5534B, "edit": 0xF8CF33, "join": 0x2AE0D4, "leave": 0x5B5F66,
               "mod": 0xF72CA2, "scam": 0xFF0000, "layout": 0xB7EF33}

# Real sites the lookalike check must never flag (lower case, without "www.").
REAL_DOMAINS = {
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.media", "discord.new",
    "discord.gift", "discord.dev", "discordstatus.com", "dis.gd", "discord.co",
    "steamcommunity.com", "steampowered.com", "steamstatic.com", "steamchina.com", "steam.tv",
    "twitch.tv", "youtube.com", "youtu.be", "kick.com", "x.com", "twitter.com", "tenor.com", "giphy.com",
}
# Brand words scam sites copy. A domain holding one (after look-alike letters are undone) that isn't
# a real one above is a fake.
BRANDS = ("discord", "nitro", "steamcommunity", "steampowered", "steam")
# Typical bait in the message text; only counts together with a link to an unknown site.
BAIT = re.compile(r"(@everyone|@here|free\s+nitro|nitro\s+(for\s+)?free|steam\s+gift|gift\s+for\s+you|"
                  r"airdrop|crypto\s+giveaway|claim\s+(your|it|now)|\$\d+\s*gift)", re.I)
URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
MASKED = re.compile(r"\[([^\]]{1,200})\]\(\s*<?(https?://[^\s)>]+)>?\s*\)", re.I)
# letters scammers swap in to fool the eye ("dlscord", "d1scord", "disc0rd", "dіscord" with a Cyrillic і)
LOOKALIKE = str.maketrans({"0": "o", "1": "i", "l": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a",
                           "і": "i", "о": "o", "а": "a", "е": "e", "с": "c", "ѕ": "s", "ԁ": "d", "т": "t"})


# ── scam links (pure functions, tested in tests/test_server_care.py) ─────────
def host_of(url: str) -> str:
    """"https://www.Dlscord-Gift.com/x" → "dlscord-gift.com" (lower case, no www., no port)."""
    try:
        h = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _registered(host: str) -> str:
    """The part someone bought: "a.b.discord.com" → "discord.com" (good enough for .com / .gg / .gift)."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_real(host: str, allowed: Optional[set] = None) -> bool:
    reg = _registered(host)
    ok = REAL_DOMAINS | (allowed or set())
    return host in ok or reg in ok


def _flatten(text: str) -> str:
    """Undo look-alike letters and drop separators: "d1scord-n1tro" → "discordnitro"."""
    t = unicodedata.normalize("NFKC", text.lower()).translate(LOOKALIKE)
    t = t.replace("rn", "m").replace("vv", "w")
    return re.sub(r"[^a-z]", "", t)


def _near(a: str, b: str) -> bool:
    """True when ``a`` is ``b`` with at most one letter changed, added or dropped ("discorb", "dicsord")."""
    if a == b or abs(len(a) - len(b)) > 1:
        return a == b
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _unicode(host: str) -> str:
    """Punycode back to letters: "xn--dscord-..." → the look-alike text a person would see."""
    try:
        return host.encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        return host


def lookalike(host: str) -> bool:
    """A fake Discord / Steam / Nitro site: the brand is in the name (maybe spelled with look-alike
    letters or one letter off) but it isn't the real site. Kept narrow on purpose, because a hit means
    delete + timeout: "steamdb.info" or "nitrotype.com" are NOT flagged, "steamcommunlty.com",
    "dlscord-gift.com", "discord-nitro.ru" and "free-nitro-gift.xyz" are."""
    if not host or is_real(host):
        return False
    shown = _unicode(host)
    flat = _flatten(shown)
    if "discord" in flat or "steamcommun" in flat or "steampowered" in flat:
        return True
    if "nitro" in flat and any(w in flat for w in ("gift", "free", "claim", "drop")):
        return True
    words = [_flatten(w) for w in re.split(r"[.\-]", shown)]
    return any(_near(w, b) for w in words if len(w) >= 5
               for b in ("discord", "discordapp", "discordgift", "steamcommunity", "steampowered"))


def scam_reason(text: str, blocked: Optional[set] = None, allowed: Optional[set] = None) -> str:
    """Why this message text is a scam ("" = it isn't). ``blocked`` / ``allowed`` = /mod block / allow."""
    blocked, allowed = blocked or set(), allowed or set()
    for shown, target in MASKED.findall(text or ""):
        real = host_of(target)
        # the domain the link text claims ("https://discord.com/gift" or just "discord.com")
        said = URL.search(shown) or re.search(r"([a-z0-9-]+\.)+[a-z]{2,}", shown.lower())
        said_host = ""
        if said:
            said_host = host_of(said.group(0) if said.group(0).startswith("http") else "http://" + said.group(0))
        if said_host and real and _registered(said_host) != _registered(real) and not is_real(real, allowed):
            return f"a link that says {said_host} but goes to {real}"
    hosts = [host_of(u) for u in URL.findall(text or "")] + [host_of(t) for _, t in MASKED.findall(text or "")]
    for h in hosts:
        if not h or is_real(h, allowed):
            continue
        if h in blocked or _registered(h) in blocked:
            return f"a blocked site ({h})"
        if lookalike(h):
            return f"a fake look-alike site ({h})"
    unknown = [h for h in hosts if h and not is_real(h, allowed)]
    if unknown and BAIT.search(text or ""):
        return f"a giveaway-style message linking to {unknown[0]}"
    return ""


class RepeatWatch:
    """The same unknown link from one person in REPEAT_CHANNELS channels within REPEAT_WINDOW seconds:
    the usual sign of a hacked account spraying a server. ``seen`` returns the earlier message ids
    (channel id, message id) to delete too, or None while it's still below the limit."""

    def __init__(self) -> None:
        self._hits: Dict[Tuple[int, str], List[Tuple[float, int, int]]] = {}

    def seen(self, user_id: int, host: str, channel_id: int, message_id: int,
             now: Optional[float] = None) -> Optional[List[Tuple[int, int]]]:
        now = time.time() if now is None else now
        rows = [r for r in self._hits.get((user_id, host), []) if now - r[0] < REPEAT_WINDOW]
        rows.append((now, channel_id, message_id))
        self._hits[(user_id, host)] = rows
        if len({r[1] for r in rows}) >= REPEAT_CHANNELS:
            self._hits.pop((user_id, host), None)
            return [(c, m) for _, c, m in rows]
        if len(self._hits) > 5000:          # never grows without end
            self._hits = {k: v for k, v in self._hits.items() if now - v[-1][0] < REPEAT_WINDOW}
        return None


# ── backups (pure: a guild object in, plain data out) ────────────────────────
def _overwrites(ch: Any) -> List[Dict[str, Any]]:
    out = []
    for target, ow in (getattr(ch, "overwrites", None) or {}).items():
        allow, deny = ow.pair()
        out.append({"id": str(target.id), "type": "role" if isinstance(target, discord.Role) else "member",
                    "name": getattr(target, "name", ""), "allow": allow.value, "deny": deny.value})
    return out


def snapshot(guild: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """The server's layout as plain JSON-able data: what a person would need to rebuild it by hand
    (and what a later "restore" round would read). Message history is not included."""
    roles = [{"id": str(r.id), "name": r.name, "colour": r.colour.value, "permissions": r.permissions.value,
              "position": r.position, "hoist": r.hoist, "mentionable": r.mentionable, "managed": r.managed,
              "members": [str(m.id) for m in getattr(r, "members", [])] if not r.is_default() else []}
             for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)]
    chans = []
    for c in sorted(guild.channels, key=lambda c: (getattr(c, "position", 0), c.id)):
        chans.append({"id": str(c.id), "name": c.name, "type": str(getattr(c, "type", "")),
                      "category": str(c.category_id) if getattr(c, "category_id", None) else "",
                      "position": getattr(c, "position", 0), "topic": getattr(c, "topic", None) or "",
                      "nsfw": bool(getattr(c, "nsfw", False)), "slowmode": getattr(c, "slowmode_delay", 0) or 0,
                      "bitrate": getattr(c, "bitrate", None), "user_limit": getattr(c, "user_limit", None),
                      "overwrites": _overwrites(c)})
    return {"server": {"id": str(guild.id), "name": guild.name,
                       "verification": str(getattr(guild, "verification_level", "")),
                       "content_filter": str(getattr(guild, "explicit_content_filter", "")),
                       "afk_channel": str(guild.afk_channel.id) if getattr(guild, "afk_channel", None) else "",
                       "afk_timeout": getattr(guild, "afk_timeout", 0),
                       "system_channel": str(guild.system_channel.id) if getattr(guild, "system_channel", None) else "",
                       "rules_channel": str(guild.rules_channel.id) if getattr(guild, "rules_channel", None) else ""},
            "roles": roles, "channels": chans,
            "emojis": [{"id": str(e.id), "name": e.name, "animated": e.animated} for e in getattr(guild, "emojis", [])],
            "saved_at": datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat(timespec="seconds"),
            "format": 1}


def layout_changes(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> List[str]:
    """Plain lines for the mod log: channels and roles added, removed or renamed since the last backup.
    (Permission changes are in the backup files; listing every bit here would be noise.)"""
    if not old:
        return []
    out = []
    for kind, label, mark in (("channels", "channel", "#"), ("roles", "role", "@")):
        a = {x["id"]: x["name"] for x in old.get(kind, [])}
        b = {x["id"]: x["name"] for x in new.get(kind, [])}
        out += [f"➕ {label} {mark}{b[i]}" for i in b if i not in a]
        out += [f"➖ {label} {mark}{a[i]}" for i in a if i not in b]
        out += [f"✏️ {label} {mark}{a[i]} → {mark}{b[i]}" for i in b if i in a and a[i] != b[i]]
    return out


def save_backup(folder: Path, data: Dict[str, Any], day: str, keep: int) -> Path:
    """Write ``server-<day>.json`` (one per day; a second backup the same day replaces it) and drop
    the oldest files beyond ``keep``."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"server-{day}.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    files = sorted(folder.glob("server-*.json"))
    for old in files[:-max(1, keep)]:
        old.unlink(missing_ok=True)
    return path


def last_backup(folder: Path) -> Optional[Dict[str, Any]]:
    files = sorted(folder.glob("server-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── what Discord lets the bot see ────────────────────────────────────────────
# Application flags (discord.com/developers/docs/resources/application#application-object-application-flags):
# the full flag is for verified bots in 100+ servers, the _LIMITED one is the developer-portal switch.
FLAG_MEMBERS = (1 << 14) | (1 << 15)
FLAG_CONTENT = (1 << 18) | (1 << 19)


async def gateway_intents(http: aiohttp.ClientSession, token: str) -> Dict[str, bool]:
    """Which privileged switches are on in the developer portal, read before connecting (asking for one
    that's off makes Discord refuse the whole connection). On any error: both off, the bot still runs."""
    try:
        async with http.get("https://discord.com/api/v10/applications/@me",
                            headers={"Authorization": f"Bot {token}"}) as r:
            if r.status != 200:
                return {"members": False, "content": False}
            flags = int((await r.json()).get("flags") or 0)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return {"members": False, "content": False}
    return {"members": bool(flags & FLAG_MEMBERS), "content": bool(flags & FLAG_CONTENT)}


def missing(guild: Any, me: Any, gateway: Dict[str, bool], mod_log: Any = None, panel: Any = None) -> List[str]:
    """``/setup check``: plain lines for each thing the starter pack needs and doesn't have."""
    out = []
    if not gateway.get("content"):
        out.append("Developer portal → Bot → turn on **Message Content Intent** (scam check, deleted message text), "
                   "then redeploy.")
    if not gateway.get("members"):
        out.append("Developer portal → Bot → turn on **Server Members Intent** (joins, leaves, who has which role), "
                   "then redeploy.")
    p = me.guild_permissions
    for ok, what, why in ((p.manage_roles, "Manage Roles", "role buttons"),
                          (p.manage_messages, "Manage Messages", "deleting scam links"),
                          (p.moderate_members, "Timeout Members", "scam timeouts"),
                          (p.view_audit_log, "View Audit Log", "who kicked, banned or timed out")):
        if not ok:
            out.append(f"Server Settings → Roles → the bot's role: allow **{what}** ({why}).")
    if mod_log is not None:
        cp = mod_log.permissions_for(me)
        if not (cp.send_messages and cp.embed_links and cp.attach_files):
            out.append(f"The bot can't post in #{mod_log.name}: allow Send Messages, Embed Links and Attach Files there.")
    for r in panel or []:
        if not r.is_assignable():
            out.append(f"Drag the bot's role above **{r.name}** (Server Settings → Roles) so its button works.")
    return out


# ── the live part ────────────────────────────────────────────────────────────
class ServerCare:
    """Owns the four starter-pack features for every server the bot is in. Settings kept in the bot's
    kv store, per server: ``mod_log.<guild>`` (channel id), ``role_panel.<guild>`` ({roles: [{id, label,
    emoji}], channel, message, title, text}), ``scam_blocked`` / ``scam_allowed`` (domain lists)."""

    def __init__(self, bot: Any, store: Any, data_dir: str, tz: str = "America/New_York",
                 scam_timeout_min: int = 60, backup_hour: int = 4, backup_keep: int = 30,
                 gateway: Optional[Dict[str, bool]] = None):
        self.bot = bot
        self.store = store
        self.folder = Path(data_dir) / "backups"
        self.tz = ZoneInfo(tz)
        self.scam_timeout_min = scam_timeout_min
        self.backup_hour = backup_hour
        self.backup_keep = backup_keep
        self.gateway = gateway or {"members": False, "content": False}
        self.repeats = RepeatWatch()
        self._removed: set = set()          # message ids the scam check deleted (not "deleted" in the mod log)

    # settings
    def domains(self, kind: str) -> set:
        return set(self.store.get(f"scam_{kind}", []) or [])

    def set_domain(self, kind: str, domain: str, on: bool) -> str:
        d = host_of(domain if "://" in domain else "http://" + domain.strip())
        if not d:
            return ""
        s = self.domains(kind)
        (s.add if on else s.discard)(d)
        self.store.put(f"scam_{kind}", sorted(s))
        return d

    def panel(self, guild_id: int) -> Dict[str, Any]:
        p = self.store.get(f"role_panel.{guild_id}") or {}
        p.setdefault("roles", [])
        return p

    def save_panel(self, guild_id: int, p: Dict[str, Any]) -> None:
        self.store.put(f"role_panel.{guild_id}", p)

    # mod log
    def log_channel(self, guild: Any) -> Any:
        cid = self.store.get(f"mod_log.{guild.id}") if guild else None
        return guild.get_channel(int(cid)) if cid else None

    async def post(self, guild: Any, kind: str, title: str, lines: List[str], user: Any = None,
                   file: Optional[Tuple[str, bytes]] = None) -> None:
        ch = self.log_channel(guild)
        if ch is None:
            return
        e = discord.Embed(title=title[:256], description="\n".join(lines)[:4000], colour=LOG_COLOURS.get(kind, 0),
                          timestamp=datetime.now(timezone.utc))
        if user is not None:
            e.set_author(name=f"{user} ({user.id})"[:256], icon_url=getattr(getattr(user, "display_avatar", None), "url", None))
        files = [discord.File(io.BytesIO(file[1]), filename=file[0])] if file else []
        try:
            await ch.send(embed=e, files=files, allowed_mentions=NO_PINGS)
        except discord.HTTPException as ex:
            log.warning("mod log post failed: %s", ex)

    # ── events (flavrbot/bot.py forwards these) ──
    async def on_message(self, m: discord.Message) -> None:
        if not m.guild or m.author.bot or not isinstance(m.author, discord.Member):
            return
        if m.author.id == self.bot.settings.owner_id or m.author.guild_permissions.manage_messages:
            return
        text = m.content or ""
        if not URL.search(text):
            return
        reason = scam_reason(text, self.domains("blocked"), self.domains("allowed"))
        extra: List[Tuple[int, int]] = []
        if not reason:
            for h in {host_of(u) for u in URL.findall(text)}:
                if h and not is_real(h, self.domains("allowed")):
                    hit = self.repeats.seen(m.author.id, h, m.channel.id, m.id)
                    if hit:
                        reason, extra = f"the same link ({h}) in {REPEAT_CHANNELS} channels within 2 minutes", hit
                        break
        if reason:
            await self.block(m, reason, extra)

    async def block(self, m: discord.Message, reason: str, extra: List[Tuple[int, int]]) -> None:
        """Delete the scam (and its copies), time the sender out, tell them, log it."""
        gone = 0
        for cid, mid in [(m.channel.id, m.id)] + [x for x in extra if x[1] != m.id]:
            ch = m.guild.get_channel(cid)
            if ch is None:
                continue
            self._removed.add(mid)
            try:
                await ch.get_partial_message(mid).delete()
                gone += 1
            except discord.HTTPException:
                pass
        timed = False
        if self.scam_timeout_min > 0:
            try:
                await m.author.timeout(timedelta(minutes=self.scam_timeout_min), reason=f"FlaVR Bot: {reason}")
                timed = True
            except discord.HTTPException:
                pass
        try:
            await m.author.send(f"Your message in **{m.guild.name}** was removed: it looked like {reason}. "
                                "If you didn't send it, your Discord account may be hacked: change your password "
                                "and turn on two-factor sign-in.", allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass
        await self.post(m.guild, "scam", "Scam link removed",
                        [f"**Why:** {reason}", f"**Where:** {m.channel.mention}",
                         f"**Removed:** {gone} message(s)" + (f" · timed out {self.scam_timeout_min} min" if timed else ""),
                         "", f"```{(m.content or '')[:1500].replace('`', 'ˋ')}```"], user=m.author)

    async def on_raw_message_delete(self, p: discord.RawMessageDeleteEvent) -> None:
        if p.message_id in self._removed:
            self._removed.discard(p.message_id)
            return
        guild = self.bot.get_guild(p.guild_id) if p.guild_id else None
        if guild is None or self.log_channel(guild) is None or p.channel_id == getattr(self.log_channel(guild), "id", 0):
            return
        m = p.cached_message
        if m is not None and m.author.bot:
            return
        if m is None:
            await self.post(guild, "delete", "Message deleted", [f"In <#{p.channel_id}> (sent before the bot's last "
                                                                 "restart, so its text wasn't kept)."])
            return
        lines = [f"In {m.channel.mention}, sent <t:{int(m.created_at.timestamp())}:R>"]
        lines.append(f"```{m.content[:1500].replace('`', 'ˋ')}```" if m.content else
                     ("(no text)" if self.gateway.get("content") else "(text hidden: Message Content Intent is off)"))
        if m.attachments:
            lines.append("Files: " + ", ".join(a.filename for a in m.attachments))
        await self.post(guild, "delete", "Message deleted", lines, user=m.author)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        if after.channel.id == getattr(self.log_channel(after.guild), "id", 0):
            return
        await self.post(after.guild, "edit", "Message edited",
                        [f"In {after.channel.mention} · [jump]({after.jump_url})",
                         f"**Before:** {before.content[:1700] or '(no text)'}",
                         f"**After:** {after.content[:1700] or '(no text)'}"], user=after.author)
        await self.on_message(after)        # editing a link in later is a common trick

    async def on_member_join(self, member: discord.Member) -> None:
        age = (datetime.now(timezone.utc) - member.created_at).days
        lines = [f"{member.mention}, account made <t:{int(member.created_at.timestamp())}:R>",
                 f"Members now: {member.guild.member_count}"]
        if age < NEW_ACCOUNT_DAYS:
            lines.insert(0, f"⚠️ New account ({age} day{'s' if age != 1 else ''} old)")
        await self.post(member.guild, "join", "Joined", lines, user=member)

    async def on_member_remove(self, member: discord.Member) -> None:
        roles = [r.name for r in member.roles if not r.is_default()]
        lines = [f"Was here since <t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Left"]
        if roles:
            lines.append("Roles: " + ", ".join(roles))
        await self.post(member.guild, "leave", "Left", lines, user=member)

    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """Kicks, bans, unbans and timeouts, with who did it and the reason (needs View Audit Log)."""
        if entry.user_id == getattr(self.bot.user, "id", None):
            return                          # the bot's own scam timeouts are already in the log
        A = discord.AuditLogAction
        who = f"<@{entry.user_id}>" if entry.user_id else "someone"
        target = entry.target
        name = f"{target} ({target.id})" if target is not None and hasattr(target, "id") else "someone"
        why = f"\n**Reason:** {entry.reason}" if entry.reason else ""
        if entry.action == A.kick:
            title, line = "Kicked", f"{name} kicked by {who}"
        elif entry.action == A.ban:
            title, line = "Banned", f"{name} banned by {who}"
        elif entry.action == A.unban:
            title, line = "Unbanned", f"{name} unbanned by {who}"
        elif entry.action == A.member_update:
            before = getattr(entry.before, "timed_out_until", None)
            after = getattr(entry.after, "timed_out_until", None)
            if before == after:
                return
            if after:
                title, line = "Timed out", f"{name} timed out by {who} until <t:{int(after.timestamp())}:f>"
            else:
                title, line = "Timeout lifted", f"{name}'s timeout lifted by {who}"
        else:
            return
        await self.post(entry.guild, "mod", title, [line + why])

    # ── role buttons ──
    def panel_view(self, p: Dict[str, Any]) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for r in p["roles"][:MAX_ROLE_BUTTONS]:
            view.add_item(discord.ui.Button(label=r.get("label") or "role", emoji=r.get("emoji") or None,
                                            style=discord.ButtonStyle.secondary, custom_id=f"{ROLE_PREFIX}{r['id']}"))
        return view

    def panel_embed(self, p: Dict[str, Any]) -> discord.Embed:
        e = discord.Embed(title=p.get("title") or "Pick your roles",
                          description=p.get("text") or "Click a button to get that role. Click it again to remove it.",
                          colour=LOG_COLOURS["mod"])
        return e

    async def post_panel(self, channel: discord.TextChannel) -> str:
        """Post the role button panel to ``channel``, or edit the panel already there. Returns a plain
        problem ("" when it worked)."""
        p = self.panel(channel.guild.id)
        if not p["roles"]:
            return "Add a role first with /roles add."
        kw = {"embed": self.panel_embed(p), "view": self.panel_view(p), "allowed_mentions": NO_PINGS}
        if p.get("channel") == str(channel.id) and p.get("message"):
            try:
                await channel.get_partial_message(int(p["message"])).edit(**kw)
                return ""
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                return f"Couldn't update the panel: {e.text}"
        try:
            msg = await channel.send(**kw)
        except discord.HTTPException as e:
            return f"Couldn't post in {channel.mention}: {e.text}"
        p.update(channel=str(channel.id), message=str(msg.id))
        self.save_panel(channel.guild.id, p)
        return ""

    async def on_interaction(self, inter: discord.Interaction) -> None:
        cid = (inter.data or {}).get("custom_id", "") if inter.type == discord.InteractionType.component else ""
        if not cid.startswith(ROLE_PREFIX) or inter.guild is None:
            return
        try:
            role_id = int(cid[len(ROLE_PREFIX):])
        except ValueError:
            return
        listed = {int(r["id"]) for r in self.panel(inter.guild.id)["roles"]}
        role = inter.guild.get_role(role_id)
        if role is None or role_id not in listed:
            await inter.response.send_message("That role isn't on offer any more.", ephemeral=True)
            return
        member = inter.user if isinstance(inter.user, discord.Member) else inter.guild.get_member(inter.user.id)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role button")
                text = f"Removed **{role.name}**."
            else:
                await member.add_roles(role, reason="Role button")
                text = f"You now have **{role.name}**."
        except discord.HTTPException:
            text = "I'm not allowed to hand out that role. A moderator needs to move my role above it."
        await inter.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ── backups ──
    async def backup(self, guild: Any, post_changes: bool = True) -> Path:
        folder = self.folder / str(guild.id)
        before = last_backup(folder)
        data = snapshot(guild)
        day = datetime.now(self.tz).strftime("%Y-%m-%d")
        path = save_backup(folder, data, day, self.backup_keep)
        changes = layout_changes(before, data)
        if changes and post_changes:
            await self.post(guild, "layout", "Server layout changed since the last backup", changes[:40])
        return path

    async def run_backups(self) -> None:
        """Daily at BACKUP_HOUR (TZ), every server the bot is in. Runs for the bot's whole life
        (flavrbot/__main__.py starts it once Discord is ready). A missed day (bot off) is made up at start."""
        while True:
            now = datetime.now(self.tz)
            today = now.strftime("%Y-%m-%d")
            for g in list(self.bot.guilds):
                if now.hour >= self.backup_hour and not (self.folder / str(g.id) / f"server-{today}.json").exists():
                    try:
                        await self.backup(g)
                    except Exception:         # noqa: BLE001  one bad day mustn't stop tomorrow's
                        log.exception("server backup failed")
            await asyncio.sleep(600)


__all__ = ["ServerCare", "scam_reason", "lookalike", "snapshot", "layout_changes", "gateway_intents", "missing"]
