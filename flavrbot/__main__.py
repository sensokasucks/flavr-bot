"""
Start FlaVR Bot: ``python -m flavrbot`` (the Docker image's command).

Builds, in order: settings (environment), the database, the Stream Core link (web listener),
phone pings, the go-live watcher with its platform checkers, then the Discord client, and runs
them all on one event loop until the container stops.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import aiohttp
import discord

from . import config
from .bot import FlavrBot
from .core_link import CoreLink
from .pings import Pinger
from .platforms.kick import Kick
from .platforms.twitch import Twitch
from .platforms.youtube import YouTube
from .store import Store
from .watcher import Watcher

log = logging.getLogger("flavrbot")


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load()
    issues = config.problems(s)
    for p in issues:
        log.error("%s", p)
    if not s.discord_token or not s.owner_id:
        return 2                # nothing works without these; the log says what to set
    store = Store(Path(s.data_dir) / "flavr_bot.db")
    link = CoreLink(store, s.core_key, late_sec=s.heartbeat_late_sec)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        checkers = {
            "twitch": Twitch(http, s.twitch_client_id, s.twitch_client_secret),
            "youtube": YouTube(http, s.youtube_api_key),
            "kick": Kick(http, s.kick_client_id, s.kick_client_secret),
        }
        watcher = Watcher(store, checkers, {"twitch": s.twitch_every, "youtube": s.youtube_every, "kick": s.kick_every})
        bot: FlavrBot

        async def send(text: str) -> None:
            await bot.dm_owner(text)

        pinger = Pinger(store, send, tz=s.timezone, quiet=s.quiet_hours, big_tip=s.big_tip, big_gift=s.big_gift,
                        live=link.stream_live)
        pinger.load_quiet()         # quiet hours changed with /pings quiet win over QUIET_HOURS
        bot = FlavrBot(s, store, link, pinger, watcher)
        link.on_stream = pinger.on_stream
        link.on_alert = pinger.on_alert
        link.on_core_lost = pinger.core_lost
        link.on_core_back = pinger.core_back
        watcher.on_start = bot.post_start
        watcher.on_change = bot.post_change
        watcher.on_end = bot.post_end

        await link.start(s.listen_host, s.listen_port)
        async def watch(platform: str) -> None:
            await bot.ready_event.wait()        # posts need the Discord channels loaded
            await watcher.run_platform(platform)

        tasks = [asyncio.create_task(pinger.run(), name="pings")]
        for platform, checker in checkers.items():
            if checker.ready:
                tasks.append(asyncio.create_task(watch(platform), name=f"watch-{platform}"))
            else:
                log.info("%s watcher off: its keys aren't set (.env.example)", platform.title())
        try:
            await bot.start(s.discord_token)
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
