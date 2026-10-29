"""Local SQLite record of what MailSift has done (so it can spot senders who
ignore an unsubscribe). Nothing here is sent anywhere."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    address TEXT NOT NULL,
    name TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unsubscribed (address TEXT PRIMARY KEY, at TEXT NOT NULL, method TEXT);
CREATE TABLE IF NOT EXISTS blocked (address TEXT PRIMARY KEY, at TEXT NOT NULL, rule_id TEXT);
CREATE TABLE IF NOT EXISTS protected (address TEXT PRIMARY KEY, at TEXT NOT NULL);
-- undo: one row per clean-up, plus what it moved (and from where) and blocked
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS undo_moves (run_id INTEGER NOT NULL, message_id TEXT NOT NULL, folder TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS undo_blocks (run_id INTEGER NOT NULL, address TEXT NOT NULL);
-- A big clean-up writes one undo_moves row per email, so every lookup by run
-- would otherwise scan the whole table. Added on open, so older files get them.
CREATE INDEX IF NOT EXISTS undo_moves_by_run ON undo_moves (run_id);
CREATE INDEX IF NOT EXISTS undo_blocks_by_run ON undo_blocks (run_id);
CREATE INDEX IF NOT EXISTS runs_undone ON runs (undone, id DESC);
-- One row per finished scan, so "what have I looked at before?" has an answer.
-- Counts only: no addresses, nothing about who sends what.
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    scope TEXT NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0,
    found INTEGER NOT NULL DEFAULT 0,
    other_senders INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def log(self, address, name, action, status, detail="", dry_run=False):
        with self._lock:
            self.db.execute(
                "INSERT INTO actions (at, address, name, action, status, detail, dry_run) VALUES (?,?,?,?,?,?,?)",
                (now_iso(), address, name, action, status, detail, int(dry_run)),
            )
            self.db.commit()

    def mark_unsubscribed(self, address, method, at=None):
        with self._lock:
            self.db.execute(
                "INSERT INTO unsubscribed (address, at, method) VALUES (?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET at=excluded.at, method=excluded.method",
                (address, at or now_iso(), method),
            )
            self.db.commit()

    def mark_blocked(self, address, rule_id=None):
        with self._lock:
            self.db.execute(
                "INSERT INTO blocked (address, at, rule_id) VALUES (?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET at=excluded.at, rule_id=excluded.rule_id",
                (address, now_iso(), rule_id),
            )
            self.db.commit()

    def unsubscribed(self) -> dict[str, str]:
        with self._lock:
            return dict(self.db.execute("SELECT address, at FROM unsubscribed").fetchall())

    def blocked(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self.db.execute("SELECT address FROM blocked")}

    # -- never-touch list --------------------------------------------------
    def protected(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self.db.execute("SELECT address FROM protected")}

    def set_protected(self, address, on: bool):
        with self._lock:
            if on:
                self.db.execute("INSERT OR IGNORE INTO protected (address, at) VALUES (?,?)", (address, now_iso()))
            else:
                self.db.execute("DELETE FROM protected WHERE address = ?", (address,))
            self.db.commit()

    def unmark_blocked(self, addresses):
        with self._lock:
            self.db.executemany("DELETE FROM blocked WHERE address = ?", [(a,) for a in addresses])
            self.db.commit()

    # -- undo ----------------------------------------------------------------
    def start_run(self) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO runs (at) VALUES (?)", (now_iso(),))
            self.db.commit()
            return cur.lastrowid

    def record_moves(self, run_id, moves):
        """moves: [(new_message_id, original_folder_id), ...]"""
        with self._lock:
            self.db.executemany("INSERT INTO undo_moves (run_id, message_id, folder) VALUES (?,?,?)",
                                [(run_id, mid, folder) for mid, folder in moves])
            self.db.commit()

    def record_block(self, run_id, address):
        with self._lock:
            self.db.execute("INSERT INTO undo_blocks (run_id, address) VALUES (?,?)", (run_id, address))
            self.db.commit()

    def last_undoable(self):
        """The most recent clean-up that moved or blocked something and hasn't been undone."""
        with self._lock:
            row = self.db.execute(
                "SELECT r.id, r.at, "
                " (SELECT COUNT(*) FROM undo_moves m WHERE m.run_id = r.id),"
                " (SELECT COUNT(*) FROM undo_blocks b WHERE b.run_id = r.id) "
                "FROM runs r WHERE r.undone = 0 AND ("
                " EXISTS (SELECT 1 FROM undo_moves m WHERE m.run_id = r.id) OR"
                " EXISTS (SELECT 1 FROM undo_blocks b WHERE b.run_id = r.id)) "
                "ORDER BY r.id DESC LIMIT 1").fetchone()
        if not row:
            return None
        return {"run_id": row[0], "at": row[1], "moved": row[2], "blocked": row[3]}

    def run_items(self, run_id):
        with self._lock:
            moves = self.db.execute("SELECT message_id, folder FROM undo_moves WHERE run_id = ?", (run_id,)).fetchall()
            blocks = [r[0] for r in self.db.execute("SELECT address FROM undo_blocks WHERE run_id = ?", (run_id,))]
        return moves, blocks

    def drop_moves(self, run_id, message_ids):
        """Forgets moves that have already been put back, so a retry after a
        partly failed undo doesn't try to move them a second time with ids
        Outlook has since changed."""
        with self._lock:
            self.db.executemany("DELETE FROM undo_moves WHERE run_id = ? AND message_id = ?",
                                [(run_id, mid) for mid in message_ids])
            self.db.commit()

    def mark_undone(self, run_id):
        with self._lock:
            self.db.execute("UPDATE runs SET undone = 1 WHERE id = ?", (run_id,))
            self.db.execute("DELETE FROM undo_moves WHERE run_id = ?", (run_id,))
            self.db.execute("DELETE FROM undo_blocks WHERE run_id = ?", (run_id,))
            self.db.commit()

    # -- scan history --------------------------------------------------------
    def record_scan(self, scope, checked, found, other_senders=0, dry_run=False):
        with self._lock:
            self.db.execute(
                "INSERT INTO scans (at, scope, checked, found, other_senders, dry_run) VALUES (?,?,?,?,?,?)",
                (now_iso(), scope, int(checked), int(found), int(other_senders), int(dry_run)),
            )
            self.db.commit()

    def scans(self, limit=50):
        with self._lock:
            rows = self.db.execute(
                "SELECT at, scope, checked, found, other_senders, dry_run FROM scans ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ("at", "scope", "checked", "found", "other_senders", "dry_run")
        return [dict(zip(keys, row)) for row in rows]

    def wipe(self):
        """Forget everything MailSift has recorded."""
        with self._lock:
            self.db.execute("DELETE FROM scans")
            self.db.execute("DELETE FROM actions")
            self.db.execute("DELETE FROM unsubscribed")
            self.db.execute("DELETE FROM blocked")
            self.db.execute("DELETE FROM protected")
            self.db.execute("DELETE FROM runs")
            self.db.execute("DELETE FROM undo_moves")
            self.db.execute("DELETE FROM undo_blocks")
            self.db.commit()
            self.db.execute("VACUUM")  # so deleted rows don't linger in the file

    def history(self, limit=200):
        with self._lock:
            rows = self.db.execute(
                "SELECT at, address, name, action, status, detail, dry_run FROM actions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ("at", "address", "name", "action", "status", "detail", "dry_run")
        return [dict(zip(keys, row)) for row in rows]
