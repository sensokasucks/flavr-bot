"""
Start FlaVR Bot: ``python -m flavrbot`` (the Docker image's command).

Builds, in order: settings (environment), the database, the Stream Core link (web listener),
phone pings, the link to FlaVR Promo (flavrbot/promo_link.py, optional), the after-stream recap
(flavrbot/recap.py), the Discord client (after reading which privileged intents the developer
portal allows) with server care, and runs them all on one event loop until the container stops.

The go-live watcher, the weekly calendar and the community calendar moved to FlaVR Promo
(sensokasucks/flavr-promo, its own stack and Discord app) on 2026-11-20. FlaVR Bot still asks it
whether the owner is live (PROMO_URL) for the Core alarm, live mode and the recap's VOD link.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from zoneinfo import ZoneInfo
from pathlib import Path

import aiohttp
import discord

from . import config
from .bot import FlavrBot
from .core_link import CoreLink
from .pings import Pinger
from .promo_link import PromoLink
from .recap import Recapper
from .server_care import gateway_intents
from .store import Store

log = logging.getLogger("flavrbot")


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load()
    issues = config.problems(s)
    for p in issues:
        log.error("%s", p)
    if not s.discord_token or not s.owner_id:
        return 2                # nothing works without these; the log says what to set
    # FlaVR Promo copies this file once on its first start (flavr-promo IMPORT_DIR); keep the name
    store = Store(Path(s.data_dir) / "flavr_bot.db")
    link = CoreLink(store, s.core_key, late_sec=s.heartbeat_late_sec)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        # FlaVR Promo's /live.json, read every minute (PROMO_URL; empty = never asked)
        promo = PromoLink(http, s.promo_url)
        bot: FlavrBot

        async def send(text: str) -> None:
            await bot.dm_owner(text)

        pinger = Pinger(store, send, tz=s.timezone, quiet=s.quiet_hours, big_tip=s.big_tip, big_gift=s.big_gift,
                        live=link.stream_live)
        pinger.load_quiet()         # quiet hours changed with /pings quiet win over QUIET_HOURS
        # which privileged Discord switches are on (asking for one that's off stops the connection)
        gateway = await gateway_intents(http, s.discord_token)
        for key, label in (("content", "Message Content"), ("members", "Server Members")):
            if not gateway[key]:
                log.warning("%s Intent is off in the developer portal: server care runs with less (see /setup check)", label)
        bot = FlavrBot(s, store, link, pinger, promo, gateway=gateway)
        async def on_core_stream(change):
            # Stream Core's own start / end: the phone ping, then live mode (flavrbot/community.py)
            await pinger.on_stream(change)
            await recapper.on_stream(change)
            ev = change.get("event")
            if ev in ("started", "resumed", "ended"):
                await bot.community.live_mode(ev != "ended", str(change.get("game") or ""))

        # after-stream recap + chat moments (flavrbot/recap.py): keeps alerts / moments, posts RECAP_DELAY_MIN
        # after Core says the stream ended, to /setup recap-channel and the owner's DMs (/pings recap)
        # the recap's VOD link comes from FlaVR Promo's owner_streams (none without PROMO_URL)
        recapper = Recapper(store, bot.post_text, pinger.ping, lambda: link.heartbeat, delay_min=s.recap_delay_min,
                            ollama_url=s.ollama_url, ollama_model=s.ollama_model, tz=ZoneInfo(s.timezone),
                            streams=lambda: promo.owner_streams)

        async def on_core_alert(a):
            await pinger.on_alert(a)
            await recapper.on_alert(a)

        link.on_stream = on_core_stream
        link.on_alert = on_core_alert
        link.on_moment = recapper.on_moment
        link.on_core_lost = pinger.core_lost
        link.owner_live = promo.owner_live      # offline everywhere = the stream ended; Core closing isn't a crash
        link.on_core_back = pinger.core_back

        # FlaVR Promo saw the owner go live / end / change game: live mode follows, even with Core off
        async def on_owner(live: bool, game: str) -> None:
            await bot.community.live_mode(live, game)

        promo.on_owner = on_owner

        await link.start(s.listen_host, s.listen_port)
        async def after_ready(job):
            await bot.ready_event.wait()        # posts need the Discord channels loaded
            await job()

        tasks = [asyncio.create_task(pinger.run(), name="pings"),
                 asyncio.create_task(after_ready(promo.run), name="promo-link"),
                 asyncio.create_task(after_ready(bot.care.run_backups), name="backups"),
                 asyncio.create_task(after_ready(bot.community.run_daily), name="moments"),
                 asyncio.create_task(after_ready(recapper.run), name="recap")]
        try:
            await bot.start(s.discord_token)
        except discord.PrivilegedIntentsRequired:
            log.error("Discord refused a privileged intent. Developer portal → your app → Bot: turn on Message Content "
                      "and Server Members Intent (or both off), then redeploy.")
            return 4
        except discord.LoginFailure:
            log.error("Discord refused DISCORD_TOKEN. Copy a fresh one: developer portal → your app → Bot → Reset Token.")
            return 3
        finally:
            for t in tasks:
                t.cancel()
            await link.stop()
            await bot.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
