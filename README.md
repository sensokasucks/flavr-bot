# FlaVR Bot

An always-on Discord bot for a second computer at home (a Linux box running Docker / Portainer).
It sits next to [Stream Core](https://github.com/sensokasucks/flavr-leftovers) (which runs on the
stream PC and has its own Discord bot for video requests) and covers the time the stream PC is off.

What it does (rounds 1 and 2):

| Feature | What it does | Needs |
|---|---|---|
| **Phone pings** | DMs you (so the Discord app on your phone buzzes): stream started / ended, stream dropped mid-stream, Stream Core stopped answering during a stream, raids, big tips and gifts, friends going live. Quiet hours hold the ordinary ones. `/pings` switches them. | Stream Core's Companion bot link |
| **/status** | Stream Core answering?, the stream, viewers, this PC's uptime and disk, the watcher | — |
| **/wake** | Turns the stream PC on (Wake-on-LAN) | `STREAM_PC_MAC`, [docs/WAKE_ON_LAN.md](docs/WAKE_ON_LAN.md) |
| **/points, /queue** | Points and video requests from Stream Core's last copy, also while the stream PC is off | Companion bot link |
| **Go-live watcher** | Follows you, your FlaVR group and your other friends on YouTube, Twitch and Kick. Posts a card in each group's channel when someone goes live (one card even when they multistream; a quick restart edits the same card), and turns it into "was live for 2 h" with the Twitch VOD at the end. `/live` lists who's live. | Platform keys (each optional) |
| **/schedule** | Upcoming streams from Twitch schedules and YouTube scheduled streams | Twitch / YouTube keys |
| **Weekly calendar** | One picture board per group (FlaVR, Friends), Sunday to Saturday: past streams with their VOD picture and length, who's live now, and what's scheduled (Twitch schedules, YouTube waiting rooms, times set with `/friend schedule`). Posted Sunday 10 AM and edited all week; a **week in review** recap Saturday 11 PM with real start and end times and a LATE NOTICE tag for streams scheduled under an hour ahead. A text list under the picture has every link. `/calendar view` shows any week; the owner gets a DM when the board changes. Weeks the bot wasn't running for are filled in from the channels' past streams (Twitch past broadcasts, kept 7 days, 60 for partners; YouTube VODs; Kick has no such list), also by hand with `/calendar refresh`. `/calendar share` (and automatically after the Sunday post and the recap, `/pings` kind `share`) DMs you copy-ready text: a Discord version with time stamps for other servers, and an X version (schedule post + links reply, one long post, or a 280-character thread: `/setup share-style`; X handles with `/friend x`, tagged only with `/setup share-style tag_people`; any @ in names and titles is defused so pasted text never tags or pings anyone). | `/setup calendar-channel`, platform keys |
| **Role buttons** | A panel of buttons in a channel; a click gives or takes that role (`/roles add`, `/roles post`). Roles with moderator permissions can't be buttons. | Manage Roles, bot's role above those roles |
| **Mod log** | Into a private channel: deleted and edited messages (with their text), joins (⚠️ for accounts under 7 days old), leaves, kicks, bans, unbans and timeouts with who did it and why, scam blocks, and channel / role changes since the last backup | `/setup mod-log`, View Audit Log, the two intents below |
| **Scam links** | Deletes fake Discord / Steam / Nitro sites, links whose text lies about where they go, giveaway bait with unknown links, the same link pasted in 3 channels within 2 minutes (a hacked account), and your own `/mod block` list. Times the sender out (`SCAM_TIMEOUT_MIN`, default 60) and DMs them why. Mods and the owner are never checked; `/mod allow` fixes a wrong catch. | Manage Messages, Timeout Members, Message Content Intent |
| **Server backup** | Every day at 4 AM (`BACKUP_HOUR`): roles, categories, channels, permissions, emoji names and who has which role, as JSON in `/data/backups` (30 days kept). `/backup now` sends you a copy. Putting a backup back is a later round. | — |
| **Calendar page + feed** | `http://<this PC>:3870/calendar` (live page, also an OBS Browser Source, `?group=friends`, `?week=next`), `/calendar.png`, and `/calendar.ics` to add to Google / Apple Calendar on the home network | — |

Only official, documented platform APIs are used, at a polite pace (Twitch every 2 minutes in one
call for up to 100 people, YouTube RSS + one API call every 10 minutes, Kick every 3 minutes).

Later rounds (planned): calendar crossposts (Bluesky, Mastodon), chat history search, Stonks
between streams, auto clips, transcripts, the rest of the server care menu (welcome, AutoMod sync,
warnings, modmail, starboard, restore from a backup).

## Set up

1. **Discord app.** [Discord developer portal](https://discord.com/developers/applications) → New
   Application (name it FlaVR Bot, add the logo) → **Bot** → Reset Token, copy it. For server
   care turn on **Message Content Intent** and **Server Members Intent** on the same page (the bot
   reads these switches at start, so it still runs with them off, just with less; `/setup check`
   says what's missing). Invite it: OAuth2 → URL Generator → scopes `bot` + `applications.commands`,
   permissions *Send Messages*, *Embed Links*, *Read Message History* (server care adds *Attach
   Files*, *Manage Roles*, *Manage Messages*, *Moderate Members*, *View Audit Log*; already invited?
   tick them on the bot's role in Server Settings → Roles instead) → open the link, pick your server.
2. **Run it** with Portainer: [docs/PORTAINER.md](docs/PORTAINER.md). The settings are environment
   variables, all listed in [.env.example](.env.example).
3. **Stream Core:** Setup → Discord & video requests → **Companion bot**: tick *Send to the companion
   bot*, type `http://<this PC's address>:3870` and the same password as `FLAVR_CORE_KEY`, Save,
   **Test**. Message list: [docs/CORE_LINK.md](docs/CORE_LINK.md).
4. **In Discord:** `/setup live-channel` for each group, then `/friend add` (or `/friend import`) the
   people to follow. Add yourself with `owner: True` so `/schedule` knows who you are. For the
   weekly calendar, `/setup calendar-channel` for each group (the first post comes within a minute).
   Server care: `/setup mod-log` (a channel only mods can see), `/roles add` + `/roles post`, then
   `/setup check`.

Port 3870 is for the home network only. Never forward it on your router.

## Commands

See the top of [flavrbot/bot.py](flavrbot/bot.py). Owner-only answers are private ("only you can see this").

## Develop

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Code map: `flavrbot/__main__.py` (start-up and wiring), `config.py` (environment), `store.py`
(SQLite in `/data`), `core_link.py` (Stream Core's pushes), `pings.py`, `watcher.py` +
`platforms/` (go-live), `bot.py` (Discord), `wol.py`, `sysinfo.py`; the calendar is `calendar.py`
(which streams go on which day), `render.py` (the HTML board and its picture, Playwright Chromium),
`calendar_post.py` (Discord posts, recap, pings) and `web.py` (page, picture, .ics). The calendar
picture needs `playwright install chromium` outside Docker (or `CHROMIUM_PATH`); without it posts
go out as text only.

## License

GPL-3.0 (see [LICENSE](LICENSE)). discord.py is MIT, tzdata is Apache-2.0.
