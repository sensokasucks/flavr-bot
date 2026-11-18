"""
Start FlaVR Bot: ``python -m flavrbot`` (the Docker image's command).

Builds, in order: settings (environment), the database, the Stream Core link (web listener),
phone pings, the go-live watcher with its platform checkers, the Discord client (after reading which
privileged intents the developer portal allows) with server care, then the weekly
calendar (its posts, and its pages on the same web listener), and runs them all on one event
loop until the container stops.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import aiohttp
import discord

from . import config, groups
from .bot import FlavrBot
from .calendar_post import CalendarPoster
from .core_link import CoreLink
from .pings import Pinger
from .platforms.kick import Kick
from .platforms.twitch import Twitch
from .platforms.youtube import YouTube
from .render import ImageCache, Screenshotter
from .server_care import gateway_intents
from .store import Store
from .watcher import Watcher
from .web import add_routes

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
    groups.init(store)                      # custom groups (/group add) live in this database
    folded = store.merge_overlaps()         # streams counted twice by an older version (or a long outage)
    if folded:
        log.info("Merged %d stream(s) that had been counted twice", folded)
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
        # which privileged Discord switches are on (asking for one that's off stops the connection)
        gateway = await gateway_intents(http, s.discord_token)
        for key, label in (("content", "Message Content"), ("members", "Server Members")):
            if not gateway[key]:
                log.warning("%s Intent is off in the developer portal: server care runs with less (see /setup check)", label)
        bot = FlavrBot(s, store, link, pinger, watcher, gateway=gateway)
        async def on_core_stream(change):
            # Stream Core's own start / end: the phone ping, then live mode (flavrbot/community.py)
            await pinger.on_stream(change)
            ev = change.get("event")
            if ev in ("started", "resumed", "ended"):
                await bot.community.live_mode(ev != "ended", str(change.get("game") or ""))

        link.on_stream = on_core_stream
        link.on_alert = pinger.on_alert
        link.on_core_lost = pinger.core_lost
        link.on_core_back = pinger.core_back
        shooter = Screenshotter(s.chromium_path)
        poster = CalendarPoster(store, bot, ImageCache(Path(s.data_dir) / "cache" / "img", http), shooter,
                                tz=s.timezone, preview_hour=s.calendar_preview_hour, recap_hour=s.calendar_recap_hour,
                                ping=lambda: pinger.switches().get("calendar", False), quiet=pinger.quiet_now,
                                hold=lambda text: pinger.ping("calendar", text), backfill=watcher.backfill,
                                share_on=lambda: pinger.switches().get("share", False))
        bot.calendar = poster
        poster.web_url = s.web_address or web_address(s.listen_port)    # the X Article link in share DMs
        link.extra_routes.append(lambda app: add_routes(app, poster))

        # go-live posts first, then a nudge so the calendar redraws within seconds
        async def on_start(person, session, live_on):
            out = await bot.post_start(person, session, live_on)
            poster.poke()
            if person.get("is_owner"):          # the watcher saw the owner go live (Core may be off): live mode
                await bot.community.live_mode(True, session.get("game") or "")
            return out

        async def on_end(person, session):
            await bot.post_end(person, session)
            poster.poke()
            if person.get("is_owner"):
                await bot.community.live_mode(False)

        watcher.on_start = on_start
        async def on_change(person, session, live_on):
            await bot.post_change(person, session, live_on)
            if person.get("is_owner") and session.get("game"):     # new game: the voice channel name follows
                await bot.community.live_mode(True, session["game"])

        watcher.on_change = on_change
        watcher.on_end = on_end
        watcher.on_scheduled = poster.on_scheduled

        await link.start(s.listen_host, s.listen_port)
        async def watch(platform: str) -> None:
            await bot.ready_event.wait()        # posts need the Discord channels loaded
            await watcher.run_platform(platform)

        async def after_ready(job):
            await bot.ready_event.wait()        # posts need the Discord channels loaded
            await job()

        async def fill_in() -> None:
            # the bot only knows streams it saw live: fill last week and this week from the platforms
            # (after the first checks have found everyone's channel ids)
            await asyncio.sleep(120)
            for off in (-1, 0):
                await poster.ensure_backfill(poster.week_of(off), force=True)

        tasks = [asyncio.create_task(pinger.run(), name="pings"),
                 asyncio.create_task(after_ready(fill_in), name="backfill"),
                 asyncio.create_task(after_ready(poster.run), name="calendar"),
                 asyncio.create_task(after_ready(watcher.run_schedules), name="schedules"),
                 asyncio.create_task(after_ready(bot.care.run_backups), name="backups"),
                 asyncio.create_task(after_ready(bot.community.run_daily), name="moments")]
        for platform, checker in checkers.items():
            if checker.ready:
                tasks.append(asyncio.create_task(watch(platform), name=f"watch-{platform}"))
            else:
                log.info("%s watcher off: its keys aren't set (.env.example)", platform.title())
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
            await shooter.close()
            await bot.close()
    return 0


def web_address(port: int) -> str:
    """This PC's home network address as "http://192.168.0.20:3870". A UDP "connect" sends nothing; it
    only asks the system which address it would use (the stack uses host networking, so this is the
    Linux PC's own). Falls back to a placeholder the owner can read; WEB_ADDRESS overrides it."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("10.255.255.255", 1))
            return f"http://{so.getsockname()[0]}:{port}"
    except OSError:
        return f"http://<this PC>:{port}"


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
