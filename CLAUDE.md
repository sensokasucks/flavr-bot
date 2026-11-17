# FlaVR Bot — notes for Claude

Always-on Discord companion to Stream Core (repo sensokasucks/flavr-leftovers). Python 3.12,
discord.py, aiohttp, SQLite. Runs in Docker on the owner's Linux box via Portainer.

Rules

- Every code change carries human-readable comments: what a file is for, who calls a function,
  which setting drives it, units, and what must stay in sync. Fix comments a change makes wrong.
- The Stream Core message list lives in three places: `docs/CORE_LINK.md`, `flavrbot/core_link.py`
  and flavr-leftovers `fridge-stream-core/core/companion_link.py`. Change them together.
- Settings are environment variables: `flavrbot/config.py` and `.env.example` stay in step.
- Platforms: official, documented APIs only, batched and slow (no page scraping). The owner
  doesn't want to risk a ban.
- Every Discord message uses `allowed_mentions` none.
- Tests: `python -m pytest` (no network). CI runs them on every push.
- `.bat` files (if any) stay ASCII + CRLF (`.gitattributes`).
