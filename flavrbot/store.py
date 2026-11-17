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
  sessions  each live stream the watcher saw: when, which platforms (and each one's link), title,
            the Discord post it made (so it can edit it when the stream ends) and the VOD link
            and picture once the stream is over (the calendar's "past" tiles).
  scheduled upcoming streams the platforms told us about (Twitch schedule, YouTube waiting
            rooms), one row per platform item. first_seen is when the bot first saw it, which the
            recap's LATE NOTICE tag needs. Past rows are kept for that; future rows are replaced
            on each refresh (flavrbot/watcher.py refresh_schedules / YouTube checks).
  manual_schedule  regular weekly times set by hand with /friend schedule (Kick has no schedule
            API; anyone else who doesn't use their platform's schedule).

people.hidden (set with /friend hide) keeps someone off the calendar and recap; the go-live
watcher still follows them.

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
CREATE TABLE IF NOT EXISTS scheduled (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  item_key TEXT NOT NULL,
  at REAL NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  game TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  thumb TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduled_key ON scheduled(platform, item_key);
CREATE INDEX IF NOT EXISTS scheduled_at ON scheduled(at);
CREATE TABLE IF NOT EXISTS manual_schedule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  weekday INTEGER NOT NULL,
  minute INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT ''
);
"""

# Columns added after round 1. A database made by round 1 gets them on start (Store.__init__);
# a new database gets them the same way, so SCHEMA above stays the round-1 shape.
#   sessions.links      JSON {platform: url} of every platform the stream was on
#   sessions.vod_url    the VOD (Twitch archive, the YouTube stream page) once it ended
#   sessions.vod_thumb  its picture (Twitch VOD thumbnail, YouTube hqdefault)
#   people.hidden       1 = left off the calendar and recap (/friend hide)
ADDED_COLUMNS = (
    ("sessions", "links", "TEXT NOT NULL DEFAULT '{}'"),
    ("sessions", "vod_url", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "vod_thumb", "TEXT NOT NULL DEFAULT ''"),
    ("people", "hidden", "INTEGER NOT NULL DEFAULT 0"),
    # "" = the watcher saw it live; "vod" = filled in afterwards from the platform's past broadcasts
    # (Watcher.backfill), so it never had a schedule to compare with (no LATE NOTICE tag)
    ("sessions", "source", "TEXT NOT NULL DEFAULT ''"),
)


def _jsonable(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Lists / dicts (sessions.platforms, sessions.links) are stored as JSON text."""
    return {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in fields.items()}


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
        for table, col, decl in ADDED_COLUMNS:
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
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

    def set_hidden(self, person_id: int, hidden: bool) -> None:
        """/friend hide: off the calendar and recap (still watched for go-live posts)."""
        with self._lock:
            self._db.execute("UPDATE people SET hidden=? WHERE id=?", (int(hidden), person_id))
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
        cols = {"person_id": person_id, "started_at": now, "last_seen": now, **_jsonable(fields)}
        with self._lock:
            cur = self._db.execute(f"INSERT INTO sessions({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                                   tuple(cols.values()))
            self._db.commit()
            return int(cur.lastrowid)

    def update_session(self, sid: int, **fields: Any) -> None:
        if not fields:
            return
        fields = _jsonable(fields)
        with self._lock:
            self._db.execute(f"UPDATE sessions SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                             (*fields.values(), sid))
            self._db.commit()

    def sessions_between(self, start: float, end: float, group: Optional[str] = None) -> List[Dict[str, Any]]:
        """Streams that STARTED in [start, end) (unix s), with name / group / hidden; the calendar's
        past and live tiles. A stream past midnight stays on the day it started."""
        with self._lock:
            q = ("SELECT s.*, p.name, p.grp, p.hidden, p.main_platform FROM sessions s JOIN people p ON p.id=s.person_id "
                 "WHERE s.started_at >= ? AND s.started_at < ?" + (" AND p.grp=?" if group else "") +
                 " ORDER BY s.started_at")
            return [dict(r) for r in self._db.execute(q, (start, end, group) if group else (start, end))]

    def merge_overlaps(self) -> int:
        """Fold together sessions of one person that overlap in time (a stream counted twice:
        the watcher ended it while a platform hadn't been checked, then saw it again with its
        real, earlier start). The LATER row is kept (its Discord post is the current one) and
        takes the earlier start; the earlier row is deleted. Called once at start-up
        (flavrbot/__main__.py). Returns how many rows were folded."""
        folded = 0
        with self._lock:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM sessions ORDER BY person_id, started_at, id")]
            for a, b in zip(rows, rows[1:]):
                if a["person_id"] != b["person_id"] or a["ended_at"] is None or b["started_at"] >= a["ended_at"]:
                    continue
                self._db.execute("UPDATE sessions SET started_at=?, restarts=MAX(restarts, ?), "
                                 "ended_at=CASE WHEN ended_at IS NULL THEN NULL ELSE MAX(ended_at, ?) END WHERE id=?",
                                 (min(a["started_at"], b["started_at"]), a["restarts"], a["ended_at"], b["id"]))
                self._db.execute("DELETE FROM sessions WHERE id=?", (a["id"],))
                b["started_at"] = min(a["started_at"], b["started_at"])
                folded += 1
            self._db.commit()
        return folded

    def live_now(self) -> List[Dict[str, Any]]:
        """Open sessions with the person's name / group, newest first (/live)."""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT s.*, p.name, p.grp FROM sessions s JOIN people p ON p.id=s.person_id "
                "WHERE s.ended_at IS NULL ORDER BY s.started_at DESC")]

    # ── scheduled streams (calendar) ───────────────────────────────────
    def replace_scheduled(self, person_id: int, platform: str, items: List[Dict[str, Any]],
                          now: Optional[float] = None) -> List[Dict[str, Any]]:
        """The platform's current list of upcoming streams for one person.

        ``items``: [{key, at, title, game, url, thumb}]. Future rows of this person + platform
        that aren't in ``items`` any more are deleted (cancelled); past rows stay (the recap's
        LATE NOTICE check needs them). Returns the rows that are NEW (for "Lulu scheduled Thu
        7 PM" pings). first_seen keeps the time the bot first saw each one."""
        now = time.time() if now is None else now
        new: List[Dict[str, Any]] = []
        with self._lock:
            keys = [str(i["key"]) for i in items]
            old = {r["item_key"] for r in self._db.execute(
                "SELECT item_key FROM scheduled WHERE person_id=? AND platform=?", (person_id, platform))}
            marks = ", ".join("?" * len(keys)) or "''"       # NOT IN () is a syntax error; '' matches nothing
            self._db.execute(f"DELETE FROM scheduled WHERE person_id=? AND platform=? AND at > ? "
                             f"AND item_key NOT IN ({marks})", (person_id, platform, now, *keys))
            for i in items:
                k = str(i["key"])
                self._db.execute(
                    "INSERT INTO scheduled(person_id, platform, item_key, at, title, game, url, thumb, first_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(platform, item_key) DO UPDATE SET at=excluded.at, "
                    "title=excluded.title, game=excluded.game, url=excluded.url, thumb=excluded.thumb",
                    (person_id, platform, k, float(i["at"]), i.get("title") or "", i.get("game") or "",
                     i.get("url") or "", i.get("thumb") or "", now))
                if k not in old:
                    new.append(dict(i))
            self._db.commit()
        return new

    def scheduled_between(self, start: float, end: float, group: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            q = ("SELECT s.*, p.name, p.grp, p.hidden, p.main_platform FROM scheduled s "
                 "JOIN people p ON p.id=s.person_id WHERE s.at >= ? AND s.at < ?" +
                 (" AND p.grp=?" if group else "") + " ORDER BY s.at")
            return [dict(r) for r in self._db.execute(q, (start, end, group) if group else (start, end))]

    def add_manual(self, person_id: int, weekday: int, minute: int, title: str = "", platform: str = "") -> int:
        """/friend schedule: every week on ``weekday`` (0 = Sunday ... 6 = Saturday) at ``minute``
        after local midnight (TZ)."""
        with self._lock:
            cur = self._db.execute("INSERT INTO manual_schedule(person_id, weekday, minute, title, platform) "
                                   "VALUES(?,?,?,?,?)", (person_id, weekday % 7, minute % 1440, title, platform))
            self._db.commit()
            return int(cur.lastrowid)

    def clear_manual(self, person_id: int) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM manual_schedule WHERE person_id=?", (person_id,))
            self._db.commit()
            return cur.rowcount

    def manual(self, group: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            q = ("SELECT m.*, p.name, p.grp, p.hidden, p.main_platform FROM manual_schedule m "
                 "JOIN people p ON p.id=m.person_id" + (" WHERE p.grp=?" if group else "") +
                 " ORDER BY m.weekday, m.minute")
            return [dict(r) for r in self._db.execute(q, (group,) if group else ())]
