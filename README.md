# FlaVR Bot

An always-on Discord bot for a second computer at home (a Linux box running Docker / Portainer).
It sits next to [Stream Core](https://github.com/sensokasucks/flavr-leftovers) (which runs on the
stream PC and has its own Discord bot for video requests) and covers the time the stream PC is off.

The go-live posts, the weekly stream calendar, reviews, day posts, talent channels, groups and the
community calendar moved to **[FlaVR Promo](https://github.com/sensokasucks/flavr-promo)** on
2026-11-20: its own bot and stack, so any streamer group can run it. The two run side by side. With
`PROMO_URL` set, FlaVR Bot asks FlaVR Promo whether you're live (its `/live.json`), so the "Stream Core
stopped answering" alarm stays quiet after a stream, live mode follows your stream even with Core off,
and the recap links your VOD. Moving over: "Moving over from FlaVR Bot" in FlaVR Promo's
docs/PORTAINER.md (its first start copies the people, past streams and calendar settings from this
bot's data folder), then update this stack.

What it does:

| Feature | What it does | Needs |
|---|---|---|
| **Phone pings** | DMs you (so the Discord app on your phone buzzes): stream started / ended, stream dropped mid-stream, Stream Core stopped answering during a stream (`/core done` says the stream is over if Core closed without saying so), raids, big tips and gifts. (Friends going live and calendar DMs come from FlaVR Promo.) Quiet hours hold the ordinary ones. `/pings` switches them. | Stream Core's Companion bot link |
| **/status** | Stream Core answering?, the stream, viewers, this PC's uptime and disk, FlaVR Promo answering? | — |
| **/wake** | Turns the stream PC on (Wake-on-LAN) | `STREAM_PC_MAC`, [docs/WAKE_ON_LAN.md](docs/WAKE_ON_LAN.md) |
| **/points, /queue** | Points and video requests from Stream Core's last copy, also while the stream PC is off | Companion bot link |
| **Role buttons** | A panel of buttons in a channel; a click gives or takes that role (`/roles add`, `/roles post`). Roles with moderator permissions can't be buttons. | Manage Roles, bot's role above those roles |
| **/said** | Moderators search past stream chat from Discord: words (any order), who, platform, last N days, 10 lines a page, newest first, only you see the answer. The lines are a copy of Stream Core's saved chat (Chat history) that Core sends over the companion link, catching up on its own; clearing chat in Core clears the copy too. | Core: Companion bot → **Send chat history**, and Core's chat saving on |
| **After-stream recap** | A few minutes after Stream Core says the stream ended (`RECAP_DELAY_MIN`), one message: how long, platforms, most viewers, chat lines and chatters, top chatters, raids, subs, gifts, tips, bits and KICKs, and the **chat moments** (times chat suddenly got much busier) with a link to that point in the VOD (from FlaVR Promo, `PROMO_URL`; Twitch and YouTube VODs open at the right time) and a Twitch clip when Core made one. Optional: a few sentences on what happened in chat from a local AI (Ollama, `OLLAMA_URL`; nothing leaves your home network). Streams under 5 minutes get none. Goes to `/setup recap-channel` and your DMs (`/pings` kind `recap`); each moment can also ping you as it happens (`/pings` kind `chat_moments`, off by default). | Core: Companion bot link; for chat counts, **Send chat history**; for clips, **Make a Twitch clip when chat suddenly gets busy** (clips are public on your channel) and Reconnect Twitch once |
| **Mod log** | Into a private channel: deleted and edited messages (with their text), joins (⚠️ for accounts under 7 days old), leaves, kicks, bans, unbans and timeouts with who did it and why, scam blocks, and channel / role changes since the last backup | `/setup mod-log`, View Audit Log, the two intents below |
| **Scam links** | Deletes fake Discord / Steam / Nitro sites, links whose text lies about where they go, giveaway bait with unknown links, the same link pasted in 3 channels within 2 minutes (a hacked account), and your own `/mod block` list. Times the sender out (`SCAM_TIMEOUT_MIN`, default 60) and DMs them why. Mods and the owner are never checked; `/mod allow` fixes a wrong catch. | Manage Messages, Timeout Members, Message Content Intent |
| **Server backup** | Every day at 4 AM (`BACKUP_HOUR`): roles, categories, channels, permissions, emoji names and who has which role, as JSON in `/data/backups` (30 days kept). `/backup now` sends you a copy. Putting a backup back is a later round. | — |
| **Welcome** | A welcome post (and/or DM) for each new member with the rules and calendar channels linked (the FlaVR calendar channel as picked before the split) (`/welcome setup`, `{member} {name} {server} {count}` filled in); `/welcome rules-button` posts an "I read the rules" button that gives a starter role | Manage Roles, Server Members Intent |
| **Raid lockdown** | `RAID_JOINS` joins within `RAID_WINDOW_SEC` (8 in 60 s) → 30 s slowmode on every open channel, invites paused, the owner DMed, the mod log told. `/lockdown off` puts it all back; `/lockdown on` by hand | Manage Channels, Manage Server |
| **Warnings** | `/warn member reason` DMs them and logs it; every 3rd warning in 30 days is a 24 h timeout (`WARN_LIMIT`, `WARN_DAYS`, `WARN_TIMEOUT_MIN`); `/warnings member [clear]` | Timeout Members |
| **Modmail** | Members DM the bot → a thread in your mods channel (`/setup modmail`); what mods write there goes back as a DM (lines starting with `//` stay private); `/modmail close` | Create Public Threads, Message Content Intent |
| **Live mode** | While you're live (Stream Core, or FlaVR Promo with `PROMO_URL`): slowmode on the channels from `/live-mode add`, a voice channel renamed "🔴 LIVE: game" (`/live-mode voice`); put back when the stream ends | Manage Channels |
| **Starboard** | `/setup starboard channel [emoji] [count]`: messages with 5 ⭐ (or your pick) reposted once, count kept up to date | Message Content Intent |
| **Member moments** | `/setup moments channel [role]`: server anniversaries and opt-in birthdays (`/birthday set`) posted at `MOMENTS_HOUR`, with a role for the day | Server Members Intent, Manage Roles |
| **Growth** | Joins and leaves per week; last week's numbers DMed Sunday (`/pings` kind `growth`); `/growth` shows 4 weeks | Server Members Intent |

Later rounds (planned): AutoMod sync, restoring a server backup, Stonks between streams.

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
4. **FlaVR Promo** (optional, for go-live posts and the calendar): set it up from its own README, then
   set `PROMO_URL` here (same PC: `http://127.0.0.1:3871`).
5. **In Discord:** server care: `/setup mod-log` (a channel only mods can see), `/roles add` + `/roles post`, then
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
(SQLite in `/data`), `core_link.py` (Stream Core's pushes), `pings.py`, `promo_link.py` (FlaVR
Promo's `/live.json`), `recap.py`, `said.py`, `server_care.py` + `community.py` (server care),
`bot.py` (Discord), `wol.py`, `sysinfo.py`.

## License

GPL-3.0 (see [LICENSE](LICENSE)). discord.py is MIT, tzdata is Apache-2.0.
