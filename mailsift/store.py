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

    def wipe(self):
        """Forget everything MailSift has recorded."""
        with self._lock:
            for table in ("actions", "unsubscribed", "blocked"):
                self.db.execute(f"DELETE FROM {table}")
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
