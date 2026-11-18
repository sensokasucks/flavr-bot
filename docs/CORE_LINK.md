# Stream Core → FlaVR Bot messages

Sender: `fridge-stream-core/core/companion_link.py` in flavr-leftovers. Receiver:
`flavrbot/core_link.py`. Keep all three in sync.

Every message is `POST /core/push` with `Authorization: Bearer <FLAVR_CORE_KEY>` and a JSON body:

```json
{"type": "heartbeat", "data": {...}, "at": 1763406000.0, "core": "stream-core"}
```

The bot answers `{"ok": true, "chat_since": 1234}` (200; `chat_since` = the highest Core chat
row id the bot has, 0 when it has none), 401 for a wrong password, 400 for a broken body. Core keeps
unsent stream events and alerts and tries again (5 s, doubling to 2 minutes).

| type | when | data |
|---|---|---|
| `hello` | link starts, settings saved, **Test** | `{}` or `{"test": true}` |
| `heartbeat` | every 30 s (`companion.heartbeat_sec`) | `stream` (state `offline` / `live` / `dropped`, `live`, `platforms`, `started_at`, `length_sec`, `ends_in`, `session`, `restarts`), `viewers`, `viewers_by_platform`, `cpm`, `credits`, `chat_connected` |
| `stream` | stream start / drop / resume / end | `event` (`started` / `dropped` / `resumed` / `ended`), `live`, `platforms`, `started_at`, `ended_at`, `length_sec`, `session`, `restarts` |
| `alert` | a real alert (never tests or follows) | `kind` (Core's alert kinds: `raid`, `subscribe`, `resub`, `gift`, `member_gift`, `bits`, `superchat`, `donation`, `kicks`, `powerup`, ...), `platform`, `username`, `display_name`, `amount`, `currency`, `qty`, `months`, `viewers`, `item`, `level`, `headline`, `message` (≤200 chars) |
| `chat` | every `CHAT_SEC` (60 s) while there are new saved lines, at once again while a full batch went (catching up); only after the bot has answered once (Core needs `chat_since`) and while `companion.send_chat` is on | `lines`: up to 1000 `[{id, platform, username, display_name, message, at}]`, oldest first: Core's saved chat (`chat_messages`, Chat history) with `id` > the bot's `chat_since`. Lines Core doesn't save (chat saving off) never reach the bot. |
| `chat_cleared` | after Core deletes saved chat (Chat history → Clear, or the `chat_log.keep_days` trim) | `before`: unix s for an age-only trim (the bot deletes its lines older than that); missing for any other clear (the bot deletes its whole copy, answers `chat_since` 0, and Core sends what it still has again) |
| `snapshot` | every 5 minutes and after a stream ends | `points`: top 500 `[{name, points, accounts}]` (accounts like `twitch:amy, kick:amy`); `queue`: `{now, up_next: [{id, video_id, title, by, channel, length, thumb}], pending}`. A part is missing when its switch is off in Core; the bot keeps its older copy. |

Rules in the bot: no heartbeat for `HEARTBEAT_LATE_SEC` (90) while the last stream state was
`live` / `dropped` → urgent "Stream Core stopped answering" ping; the next heartbeat → "answering again".
The chat copy is for `/said` (flavrbot/said.py, moderators only).
Unknown types are ignored (a newer Core can add some).
