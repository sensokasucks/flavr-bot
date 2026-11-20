"""
The bot's own database: one SQLite file in DATA_DIR (/data in Docker, a named volume so it
survives redeploys). Small on purpose; nothing here is a secret.

Tables
  kv        key → JSON value. Core's last heartbeat / snapshot ("core.*"), the ping switches
            ("pings"), held pings, server care and recap settings.
  chat      a copy of Stream Core's saved chat history (Core's chat_messages, sent in "chat"
            messages by fridge-stream-core/core/companion_link.py), for /said (flavrbot/said.py).
            core_id is Core's own row id: the bot answers each push with the highest one it has
            (``chat_since``) and Core sends the lines after it, so a fresh or wiped copy fills
            itself in. chat_fts is its word index (SQLite FTS5) when the SQLite build has FTS5.
  moments   chat moments Core spotted (fridge-stream-core/core/chat_moments.py "moment"): when,
            how busy, the usual, and a Twitch clip link when Core made one. For the recap.
  alerts    every alert Core sent (raids, subs, gifts, tips) as JSON, for the after-stream recap
            (flavrbot/recap.py). Both are pruned after KEEP_EVENTS_DAYS.

The go-live watcher's and calendar's tables (people, accounts, sessions, scheduled, manual_schedule,
promo, uploads) moved to FlaVR Promo with the split on 2026-11-20; it copied them from this file on
its first start (flavr-promo flavrpromo/import_old.py). An older database still holds them, unread,
so FlaVR Promo can copy them again if its own database is ever lost.

All calls are synchronous and quick; async code runs them with ``asyncio.to_thread`` where it
matters (the chat copy), or directly for single-row reads (commands).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat (
  core_id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT '',
  username TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chat_at ON chat(at);
CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  cpm REAL NOT NULL DEFAULT 0,
  baseline REAL NOT NULL DEFAULT 0,
  clip TEXT NOT NULL DEFAULT '',
  clip_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS moments_at ON moments(at);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS alerts_at ON alerts(at);
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
        self.fts = self._make_fts()

    def _make_fts(self) -> bool:
        """The chat word index (FTS5, external content = the chat table). False when this SQLite
        has no FTS5: /said then searches with LIKE (slower, same results for plain words)."""
        try:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(message, "
                             "content='chat', content_rowid='core_id')")
            self._db.commit()
            return True
        except sqlite3.OperationalError:
            return False

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

    def delete(self, key: str) -> None:
        """Forget a kv key (/group remove clears that group's channels and post)."""
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key=?", (key,))
            self._db.commit()

    # ── chat copy (/said, flavrbot/said.py) ────────────────────────────
    def chat_since(self) -> int:
        """The highest Core chat row id the bot has (0 = none); sent back to Core with every push."""
        with self._lock:
            row = self._db.execute("SELECT MAX(core_id) AS m FROM chat").fetchone()
        return int(row["m"] or 0)

    def add_chat(self, lines: List[Dict[str, Any]]) -> int:
        """Core's "chat" message: [{id, platform, username, display_name, message, at}]. Rows the bot
        already has are skipped. Returns how many were new."""
        added = 0
        with self._lock:
            for ln in lines:
                try:
                    cid = int(ln.get("id"))
                except (TypeError, ValueError):
                    continue
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO chat(core_id, platform, username, display_name, message, at) "
                    "VALUES(?,?,?,?,?,?)", (cid, str(ln.get("platform") or ""), str(ln.get("username") or ""),
                                            str(ln.get("display_name") or ""), str(ln.get("message") or "")[:2000],
                                            float(ln.get("at") or 0)))
                if cur.rowcount:
                    added += 1
                    if self.fts:
                        self._db.execute("INSERT INTO chat_fts(rowid, message) VALUES(?, ?)",
                                         (cid, str(ln.get("message") or "")[:2000]))
            self._db.commit()
        return added

    def clear_chat(self, before: Optional[float] = None) -> int:
        """Core's "chat_cleared": lines older than ``before`` (Core's keep-days trim), or all of them
        (a clear by hand in Core's Chat history; Core then sends what it still has again)."""
        with self._lock:
            if before is None:
                n = self._db.execute("DELETE FROM chat").rowcount
                if self.fts:
                    self._db.execute("INSERT INTO chat_fts(chat_fts) VALUES('delete-all')")
            else:
                if self.fts:
                    self._db.execute("INSERT INTO chat_fts(chat_fts, rowid, message) "
                                     "SELECT 'delete', core_id, message FROM chat WHERE at < ?", (before,))
                n = self._db.execute("DELETE FROM chat WHERE at < ?", (before,)).rowcount
            self._db.commit()
        return n

    def search_chat(self, words: str = "", who: str = "", platform: str = "", since: float = 0,
                    limit: int = 10, offset: int = 0) -> Dict[str, Any]:
        """/said: lines matching all ``words`` (any order), from ``who`` (username or display name,
        any case), on ``platform``, newer than ``since``; newest first. {"rows": [...], "total": n}."""
        clauses, args = [], []
        terms = [t for t in re.findall(r"\w+", words or "")][:8]
        if terms and self.fts:
            clauses.append("c.core_id IN (SELECT rowid FROM chat_fts WHERE chat_fts MATCH ?)")
            args.append(" ".join(f'"{t}"' for t in terms))
        else:
            for t in terms:
                clauses.append("lower(c.message) LIKE ?")
                args.append(f"%{t.lower()}%")
        if who.strip():
            w = who.strip().lstrip("@").lower()
            clauses.append("(lower(c.username)=? OR lower(c.display_name)=?)")
            args += [w, w]
        if platform:
            clauses.append("c.platform=?")
            args.append(platform)
        if since:
            clauses.append("c.at>=?")
            args.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) AS n FROM chat c {where}", args).fetchone()["n"]
            rows = [dict(r) for r in self._db.execute(
                f"SELECT c.* FROM chat c {where} ORDER BY c.at DESC, c.core_id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset))]
        return {"rows": rows, "total": int(total)}

    def chat_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) AS n FROM chat").fetchone()["n"])

    # ── moments + alerts (after-stream recap, flavrbot/recap.py) ───────
    KEEP_EVENTS_DAYS = 120

    def add_moment(self, m: Dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("INSERT INTO moments(at, cpm, baseline, clip, clip_error) VALUES(?,?,?,?,?)",
                             (float(m.get("at") or time.time()), float(m.get("cpm") or 0), float(m.get("baseline") or 0),
                              str(m.get("clip") or ""), str(m.get("clip_error") or "")[:300]))
            self._db.commit()

    def add_alert(self, a: Dict[str, Any], at: Optional[float] = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO alerts(at, data) VALUES(?, ?)", (at or time.time(), json.dumps(a)))
            self._db.execute("DELETE FROM alerts WHERE at < ?", (time.time() - self.KEEP_EVENTS_DAYS * 86400,))
            self._db.execute("DELETE FROM moments WHERE at < ?", (time.time() - self.KEEP_EVENTS_DAYS * 86400,))
            self._db.commit()

    def moments_between(self, a: float, b: float) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM moments WHERE at >= ? AND at < ? ORDER BY at", (a, b))]

    def alerts_between(self, a: float, b: float) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM alerts WHERE at >= ? AND at < ? ORDER BY at", (a, b))]
        out = []
        for r in rows:
            try:
                out.append({**json.loads(r["data"]), "at": r["at"]})
            except ValueError:
                continue
        return out

    def chat_between(self, a: float, b: float, limit: int = 0) -> List[Dict[str, Any]]:
        """Chat copy lines in [a, b), oldest first (the recap's counts, top chatters, summary)."""
        q = "SELECT * FROM chat WHERE at >= ? AND at < ? ORDER BY at" + (" LIMIT ?" if limit else "")
        with self._lock:
            return [dict(r) for r in self._db.execute(q, (a, b, limit) if limit else (a, b))]
