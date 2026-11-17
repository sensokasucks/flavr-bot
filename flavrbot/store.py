"""
The bot's own database: one SQLite file in DATA_DIR (/data in Docker, a named volume so it
survives redeploys). Small on purpose; nothing here is a secret.

Tables
  kv        key → JSON value. Core's last heartbeat / snapshot ("core.*"), the ping switches
            ("pings"), the live-post channel per group ("live_channel.<group>"), held pings.
  people    who the go-live watcher follows: one row per person, with a group (flavr / friends)
            and the owner flag (the streamer themself, for /schedule).
  accounts  a person's channels: platform (twitch / youtube / kick), handle as typed, the
            platform's own id once looked up, and which one is their main platform.
  sessions  each live stream the watcher saw: when, which platforms, title, the Discord post
            it made (so it can edit it when the stream ends) and the VOD link if known.

All calls are synchronous and quick; async code runs them with ``asyncio.to_thread`` where it
matters (the watcher), or directly for single-row reads (commands).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PLATFORMS = ("youtube", "twitch", "kick")      # also the main-platform fallback order
GROUPS = ("flavr", "friends")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  grp TEXT NOT NULL DEFAULT 'flavr',
  is_owner INTEGER NOT NULL DEFAULT 0,
  main_platform TEXT NOT NULL DEFAULT '',
  added_by TEXT NOT NULL DEFAULT '',
  added_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS people_name ON people(lower(name));
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  handle TEXT NOT NULL,
  platform_id TEXT NOT NULL DEFAULT '',
  display TEXT NOT NULL DEFAULT '',
  avatar TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS accounts_handle ON accounts(platform, lower(handle));
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  started_at REAL NOT NULL,
  ended_at REAL,
  last_seen REAL NOT NULL,
  platforms TEXT NOT NULL DEFAULT '[]',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  thumb TEXT NOT NULL DEFAULT '',
  game TEXT NOT NULL DEFAULT '',
  viewers INTEGER NOT NULL DEFAULT 0,
  restarts INTEGER NOT NULL DEFAULT 0,
  post_channel TEXT NOT NULL DEFAULT '',
  post_message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_person ON sessions(person_id, started_at);
"""


class Store:
    """One per bot (``flavrbot/__main__.py``). Thread-safe: one connection behind a lock."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._db.commit()

    # ── kv ─────────────────────────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, json.dumps(value)))
            self._db.commit()

    # ── people + accounts ──────────────────────────────────────────────
    def add_person(self, name: str, group: str = "flavr", is_owner: bool = False, added_by: str = "") -> int:
        """Add (or find) a person by name; returns their id. Group / owner flag are updated."""
        group = group if group in GROUPS else "flavr"
        with self._lock:
            row = self._db.execute("SELECT id FROM people WHERE lower(name)=lower(?)", (name,)).fetchone()
            if row:
                self._db.execute("UPDATE people SET grp=?, is_owner=MAX(is_owner, ?) WHERE id=?",
                                 (group, int(is_owner), row["id"]))
                self._db.commit()
                return int(row["id"])
            cur = self._db.execute("INSERT INTO people(name, grp, is_owner, added_by, added_at) VALUES(?,?,?,?,?)",
                                   (name, group, int(is_owner), added_by, time.time()))
            self._db.commit()
            return int(cur.lastrowid)

    def add_account(self, person_id: int, platform: str, handle: str) -> bool:
        """Link a channel to a person. False when that channel already belongs to someone."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO accounts(person_id, platform, handle) VALUES(?,?,?)",
                                 (person_id, platform, handle))
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def set_account_info(self, account_id: int, platform_id: str = "", display: str = "", avatar: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE accounts SET platform_id=COALESCE(NULLIF(?, ''), platform_id), "
                             "display=COALESCE(NULLIF(?, ''), display), avatar=COALESCE(NULLIF(?, ''), avatar) WHERE id=?",
                             (platform_id, display, avatar, account_id))
            self._db.commit()

    def set_main(self, person_id: int, platform: str) -> None:
        with self._lock:
            self._db.execute("UPDATE people SET main_platform=? WHERE id=?", (platform, person_id))
            self._db.commit()

    def remove_person(self, name: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM people WHERE lower(name)=lower(?)", (name,))
            self._db.commit()
            return cur.rowcount > 0

    def people(self, group: Optional[str] = None) -> List[Dict[str, Any]]:
        """Everyone (or one group), each with an ``accounts`` list, sorted by name."""
        with self._lock:
            q = "SELECT * FROM people" + (" WHERE grp=?" if group else "") + " ORDER BY lower(name)"
            rows = [dict(r) for r in self._db.execute(q, (group,) if group else ())]
            accs = [dict(r) for r in self._db.execute("SELECT * FROM accounts ORDER BY platform")]
        by: Dict[int, List[Dict[str, Any]]] = {}
        for a in accs:
            by.setdefault(a["person_id"], []).append(a)
        for r in rows:
            r["accounts"] = by.get(r["id"], [])
        return rows

    def person(self, name: str) -> Optional[Dict[str, Any]]:
        for p in self.people():
            if p["name"].lower() == name.lower():
                return p
        return None

    def accounts(self, platform: str) -> List[Dict[str, Any]]:
        """Every account on one platform with its person's name / group (watcher input)."""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT a.*, p.name, p.grp, p.is_owner FROM accounts a JOIN people p ON p.id=a.person_id "
                "WHERE a.platform=?", (platform,))]

    # ── sessions ───────────────────────────────────────────────────────
    def open_session(self, person_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM sessions WHERE person_id=? AND ended_at IS NULL "
                                   "ORDER BY started_at DESC LIMIT 1", (person_id,)).fetchone()
        return dict(row) if row else None

    def last_session(self, person_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM sessions WHERE person_id=? ORDER BY started_at DESC LIMIT 1",
                                   (person_id,)).fetchone()
        return dict(row) if row else None

    def start_session(self, person_id: int, now: float, **fields: Any) -> int:
        cols = {"person_id": person_id, "started_at": now, "last_seen": now, **fields}
        if isinstance(cols.get("platforms"), list):
            cols["platforms"] = json.dumps(cols["platforms"])
        with self._lock:
            cur = self._db.execute(f"INSERT INTO sessions({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                                   tuple(cols.values()))
            self._db.commit()
            return int(cur.lastrowid)

    def update_session(self, sid: int, **fields: Any) -> None:
        if not fields:
            return
        if isinstance(fields.get("platforms"), list):
            fields["platforms"] = json.dumps(fields["platforms"])
        with self._lock:
            self._db.execute(f"UPDATE sessions SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                             (*fields.values(), sid))
            self._db.commit()

    def live_now(self) -> List[Dict[str, Any]]:
        """Open sessions with the person's name / group, newest first (/live)."""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT s.*, p.name, p.grp FROM sessions s JOIN people p ON p.id=s.person_id "
                "WHERE s.ended_at IS NULL ORDER BY s.started_at DESC")]
