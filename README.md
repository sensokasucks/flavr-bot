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
| **Weekly calendar** | One picture board per group (FlaVR, Friends and any you add), Sunday to Saturday: past streams with their VOD picture and length, who's live now, and what's scheduled (Twitch schedules, YouTube waiting rooms, times set with `/friend schedule`). Posted Sunday 10 AM and edited all week; a **week in review** recap Saturday 11 PM with real start and end times and a LATE NOTICE tag for streams scheduled under an hour ahead. A text list under the picture has every link. `/calendar view` shows any week (or, with `when`, just today / tomorrow / the weekend / a weekday, like `/calendar focus`; `/calendar post when:` posts that as an extra post); YouTube streams scheduled weeks ahead are found from each channel's 50 newest videos every 6 hours, with an optional deeper search (`/setup youtube-check`: off, daily, 48h or weekly; 100 quota units per channel a run); the owner gets a DM when a board changes (one per group, with that group's picture). Weeks the bot wasn't running for are filled in from the channels' past streams (Twitch past broadcasts, kept 7 days, 60 for partners; YouTube VODs; Kick has no such list), also by hand with `/calendar refresh`. `/calendar share` (and automatically after the Sunday post and the recap, `/pings` kind `share`) DMs you copy-ready text: a Discord version with time stamps for other servers, and an X version (schedule post + links reply, one long post, a 280-character thread, or an **X Article**: a page on the bot's web server, `/calendar/article`, with a Copy button that pastes headings, bold and links into X's Article editor: `/setup share-style`; X handles with `/friend x`, tagged only with `/setup share-style tag_people`; any @ in names and titles is defused so pasted text never tags or pings anyone). | `/setup calendar-channel`, platform keys |
| **Day focus posts** | The calendar for just **today**, **tomorrow**, **the weekend** (Friday to Sunday) or a weekday: big tiles for those days and the list with links under it. `/calendar focus` shows it to you (or posts it in the channel with `post`); in a talent channel it shows only that person. Optional automatic posts per group, each at a time you type ("10am", "8pm") or "off": a **Today** post (default 10 AM), an evening **Tomorrow** preview (default off) and a **This weekend** post on the day you pick (default Friday 10 AM); only when something's on, and edited through the day as people go live and end. `/calendar share when:` gives the copy-ready Discord and X text for just those days, and with `/pings` kind `share` on it follows each automatic post in your DMs. | `/setup day-posts` (off until a channel is picked) |
| **Community calendar** | For a self-promo channel: members with a role you pick add their own stream posts to a separate **Community** board (violet, its own Sunday post and Saturday week in review, next to FlaVR and Friends). Long-press (phone) or right-click (PC) a post → **Apps → Add to community calendar** (a Twitch / Kick channel link asks "When?", e.g. "sat 8pm"; empty = live now), or `/community schedule link [when]`. Only YouTube stream, Twitch VOD and Twitch / Kick channel links count, each looked up through the official APIs (never opened); the bot then follows it from planned to live to finished (Twitch gets its VOD; Kick has no VOD API, so it links the channel's videos page). Opt-in per post: the bot never reads the channel by itself. Safety: your own posts only (moderators can add or remove anyone's), `PROMO_WEEKLY_LIMIT` posts a week each (7), duplicates merge, a deleted post drops off, **Remove from community calendar** or `/community remove`, and nothing pings anyone. `/community list` shows a week with numbers. | `/community roles add`, `/community report-channel` |
| **Role buttons** | A panel of buttons in a channel; a click gives or takes that role (`/roles add`, `/roles post`). Roles with moderator permissions can't be buttons. | Manage Roles, bot's role above those roles |
| **/said** | Moderators search past stream chat from Discord: words (any order), who, platform, last N days, 10 lines a page, newest first, only you see the answer. The lines are a copy of Stream Core's saved chat (Chat history) that Core sends over the companion link, catching up on its own; clearing chat in Core clears the copy too. | Core: Companion bot → **Send chat history**, and Core's chat saving on |
| **After-stream recap** | A few minutes after Stream Core says the stream ended (`RECAP_DELAY_MIN`), one message: how long, platforms, most viewers, chat lines and chatters, top chatters, raids, subs, gifts, tips, bits and KICKs, and the **chat moments** (times chat suddenly got much busier) with a link to that point in the VOD (Twitch and YouTube VODs open at the right time) and a Twitch clip when Core made one. Optional: a few sentences on what happened in chat from a local AI (Ollama, `OLLAMA_URL`; nothing leaves your home network). Streams under 5 minutes get none. Goes to `/setup recap-channel` and your DMs (`/pings` kind `recap`); each moment can also ping you as it happens (`/pings` kind `chat_moments`, off by default). | Core: Companion bot link; for chat counts, **Send chat history**; for clips, **Make a Twitch clip when chat suddenly gets busy** (clips are public on your channel) and Reconnect Twitch once |
| **Talent channels** | One channel per person: `/talent channel Lulu #lulu-teala`. Their go-live card goes there AND in their group's channel (either can be switched off per person with `/talent options`), a pinned text card shows only their week (edited as it changes, a new one each Sunday 10 AM), and their own recap posts Saturday 11 PM. `/live`, `/schedule`, `/calendar view` and `/calendar share` used in their channel show only them. `/talent link` connects their Discord account so they can manage their own entry with `/me` (weekly times, main platform, X handle, hide for a vacation, where their cards go, their own share text); nobody else's. | Pin Messages (or Manage Messages) in their channel to pin the card |
| **Groups** | FlaVR, Friends and Community are built in; `/group add Art Pals #ff8800` makes your own, each with its own go-live channel, calendar board, recap and share text. `/friend group` moves someone, `/group edit` renames or recolours (the built-in three too; FlaVR's board keeps the logo colours), `/group remove` deletes one and moves its people to Friends, `/group list` shows people and channels. | owner or Manage Server |
| **Mod log** | Into a private channel: deleted and edited messages (with their text), joins (⚠️ for accounts under 7 days old), leaves, kicks, bans, unbans and timeouts with who did it and why, scam blocks, and channel / role changes since the last backup | `/setup mod-log`, View Audit Log, the two intents below |
| **Scam links** | Deletes fake Discord / Steam / Nitro sites, links whose text lies about where they go, giveaway bait with unknown links, the same link pasted in 3 channels within 2 minutes (a hacked account), and your own `/mod block` list. Times the sender out (`SCAM_TIMEOUT_MIN`, default 60) and DMs them why. Mods and the owner are never checked; `/mod allow` fixes a wrong catch. | Manage Messages, Timeout Members, Message Content Intent |
| **Server backup** | Every day at 4 AM (`BACKUP_HOUR`): roles, categories, channels, permissions, emoji names and who has which role, as JSON in `/data/backups` (30 days kept). `/backup now` sends you a copy. Putting a backup back is a later round. | — |
| **Welcome** | A welcome post (and/or DM) for each new member with the rules and calendar channels linked (`/welcome setup`, `{member} {name} {server} {count}` filled in); `/welcome rules-button` posts an "I read the rules" button that gives a starter role | Manage Roles, Server Members Intent |
| **Raid lockdown** | `RAID_JOINS` joins within `RAID_WINDOW_SEC` (8 in 60 s) → 30 s slowmode on every open channel, invites paused, the owner DMed, the mod log told. `/lockdown off` puts it all back; `/lockdown on` by hand | Manage Channels, Manage Server |
| **Warnings** | `/warn member reason` DMs them and logs it; every 3rd warning in 30 days is a 24 h timeout (`WARN_LIMIT`, `WARN_DAYS`, `WARN_TIMEOUT_MIN`); `/warnings member [clear]` | Timeout Members |
| **Modmail** | Members DM the bot → a thread in your mods channel (`/setup modmail`); what mods write there goes back as a DM (lines starting with `//` stay private); `/modmail close` | Create Public Threads, Message Content Intent |
| **Live mode** | While you're live (watcher or Core): slowmode on the channels from `/live-mode add`, a voice channel renamed "🔴 LIVE: game" (`/live-mode voice`); put back when the stream ends | Manage Channels |
| **Starboard** | `/setup starboard channel [emoji] [count]`: messages with 5 ⭐ (or your pick) reposted once, count kept up to date | Message Content Intent |
| **Member moments** | `/setup moments channel [role]`: server anniversaries and opt-in birthdays (`/birthday set`) posted at `MOMENTS_HOUR`, with a role for the day | Server Members Intent, Manage Roles |
| **Growth** | Joins and leaves per week; last week's numbers DMed Sunday (`/pings` kind `growth`); `/growth` shows 4 weeks | Server Members Intent |
| **Calendar page + feed** | `http://<this PC>:3870/calendar` (live page, also an OBS Browser Source, `?group=friends` or a custom group's name, `?week=next`), `/calendar.png`, and `/calendar.ics` to add to Google / Apple Calendar on the home network | — |

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
