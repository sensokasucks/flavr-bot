"""
FlaVR Bot settings, read once at start from environment variables.

In Docker these come from the stack's environment (Portainer: Stacks → flavr-bot → Environment
variables, or a .env file next to docker-compose.yml). Every name and its meaning is listed in
.env.example; keep the two in sync. Secrets (tokens, keys, the Core password) only ever live in
the environment, never in the database or the logs.

Who reads it: flavrbot/__main__.py builds one Settings and hands it to every part.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(str(env.get(key, "")).strip() or default)
    except ValueError:
        return default


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = str(env.get(key, "")).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _hours(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    """"3-11" → (3, 11): quiet hours from 3 AM to 11 AM; "" or "off" → (0, 0) = no quiet hours."""
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    if raw in ("off", "none", "0"):
        return (0, 0)
    try:
        a, b = raw.split("-", 1)
        return (int(a) % 24, int(b) % 24)
    except ValueError:
        return default


@dataclass
class Settings:
    # Discord
    discord_token: str = ""
    owner_id: int = 0                   # the streamer's Discord user id: owner-only commands + DM pings
    guild_id: int = 0                   # optional: register slash commands on this server at once
    # Core link (Stream Core → this bot; core/companion_link.py in flavr-leftovers)
    core_key: str = ""                  # shared password; Core sends it as "Authorization: Bearer ..."
    listen_host: str = "0.0.0.0"        # home network only: never forward this port on the router
    listen_port: int = 3870
    heartbeat_late_sec: int = 90        # no heartbeat this long during a stream = "Core stopped answering"
    # Wake-on-LAN (/wake)
    stream_pc_mac: str = ""
    wol_broadcast: str = "255.255.255.255"
    # FlaVR Promo (sensokasucks/flavr-promo; the go-live watcher and calendar moved there 2026-11-20):
    # its address, e.g. http://127.0.0.1:3871. flavrbot/promo_link.py reads its /live.json every
    # minute: is the owner live (the Core alarm stays quiet after a stream, live mode follows it while
    # Core is off) and the owner's VOD for the recap. "" = not asked; Core alone drives those.
    promo_url: str = ""
    # Phone pings
    timezone: str = "America/New_York"
    quiet_hours: tuple[int, int] = (3, 11)
    big_tip: float = 20.0               # tips / Super Chats at least this much (in their own currency)
    big_gift: int = 10                  # gifted subs at least this many at once
    # Server care (flavrbot/server_care.py)
    scam_timeout_min: int = 60          # timeout for someone who posts a scam link (minutes; 0 = none)
    backup_hour: int = 4                # daily server layout backup at this hour (TZ, 0-23)
    backup_keep: int = 30               # backups kept (one per day)
    # Server care round 2 (flavrbot/community.py)
    raid_joins: int = 8                 # this many joins ...
    raid_window_sec: int = 60           # ... within this many seconds = a raid → lockdown
    warn_limit: int = 3                 # every this-many active warnings = a timeout (0 = never)
    warn_days: int = 30                 # warnings older than this stop counting
    warn_timeout_min: int = 1440        # that timeout, minutes (Discord's cap: 28 days)
    moments_hour: int = 10              # anniversaries / birthdays / Sunday growth DM at this hour (TZ)
    # after-stream recap (flavrbot/recap.py): minutes after Core's "stream ended" before it is written
    # (the last chat lines arrive), and an optional local AI (Ollama on this PC / home network) for a
    # few sentences on what happened in chat; empty OLLAMA_URL = no AI, the recap is numbers + moments
    recap_delay_min: int = 3
    ollama_url: str = ""                # e.g. http://127.0.0.1:11434
    ollama_model: str = "llama3.2:3b"
    # Storage
    data_dir: str = "/data"
    extra: Dict[str, str] = field(default_factory=dict)


def load(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Settings from ``env`` (default: os.environ)."""
    env = os.environ if env is None else env
    g = lambda k, d="": str(env.get(k, d) or d).strip()  # noqa: E731
    return Settings(
        discord_token=g("DISCORD_TOKEN"),
        owner_id=_int(env, "OWNER_ID", 0),
        guild_id=_int(env, "GUILD_ID", 0),
        core_key=g("FLAVR_CORE_KEY"),
        listen_host=g("LISTEN_HOST", "0.0.0.0"),
        listen_port=_int(env, "LISTEN_PORT", 3870),
        heartbeat_late_sec=max(45, _int(env, "HEARTBEAT_LATE_SEC", 90)),
        stream_pc_mac=g("STREAM_PC_MAC"),
        wol_broadcast=g("WOL_BROADCAST", "255.255.255.255"),
        promo_url=g("PROMO_URL").rstrip("/"),
        timezone=g("TZ", "America/New_York"),
        quiet_hours=_hours(g("QUIET_HOURS"), (3, 11)),
        big_tip=float(g("BIG_TIP", "20") or 20),
        big_gift=max(1, _int(env, "BIG_GIFT", 10)),
        scam_timeout_min=max(0, min(40320, _int(env, "SCAM_TIMEOUT_MIN", 60))),     # Discord's cap: 28 days
        backup_hour=_int(env, "BACKUP_HOUR", 4) % 24,
        backup_keep=max(1, _int(env, "BACKUP_KEEP", 30)),
        raid_joins=max(2, _int(env, "RAID_JOINS", 8)),
        raid_window_sec=max(5, _int(env, "RAID_WINDOW_SEC", 60)),
        warn_limit=max(0, _int(env, "WARN_LIMIT", 3)),
        warn_days=max(1, _int(env, "WARN_DAYS", 30)),
        warn_timeout_min=max(0, min(40320, _int(env, "WARN_TIMEOUT_MIN", 1440))),
        moments_hour=_int(env, "MOMENTS_HOUR", 10) % 24,
        recap_delay_min=max(1, min(60, _int(env, "RECAP_DELAY_MIN", 3))),
        ollama_url=g("OLLAMA_URL").rstrip("/"),
        ollama_model=g("OLLAMA_MODEL", "llama3.2:3b"),
        data_dir=g("DATA_DIR", "/data"),
    )


def problems(s: Settings) -> list[str]:
    """Plain-words setup problems; the bot refuses to start while the first one is there."""
    out = []
    if not s.discord_token:
        out.append("DISCORD_TOKEN is empty: paste the bot token from the Discord developer portal.")
    if not s.owner_id:
        out.append("OWNER_ID is empty: your own Discord user id (Discord → Settings → Advanced → "
                   "Developer Mode, then right-click your name → Copy User ID).")
    if not s.core_key:
        out.append("FLAVR_CORE_KEY is empty: pick a password and type the same one in Stream Core "
                   "(Setup → Discord & video requests → Companion bot).")
    return out
