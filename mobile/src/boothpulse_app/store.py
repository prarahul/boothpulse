"""Offline store: the booth's voter list and a queue of unsent changes, in SQLite.

Everything an agent does is written here first, so the app keeps working
with no signal. The sync engine sends the queue when the network returns.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

VOTER_COLUMNS = [
    "id", "booth", "booth_number", "serial_no", "epic", "name", "relative_name", "house_no",
    "age", "gender", "status", "needs_assistance", "assistance_note", "contact_consent",
    "phone", "notes", "inclination", "updated_at",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS voters (
    id INTEGER PRIMARY KEY, booth INTEGER, booth_number INTEGER, serial_no INTEGER,
    epic TEXT, name TEXT, relative_name TEXT, house_no TEXT, age INTEGER, gender TEXT,
    status TEXT, needs_assistance INTEGER, assistance_note TEXT, contact_consent INTEGER,
    phone TEXT, notes TEXT, inclination TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS voters_booth ON voters(booth, serial_no);
CREATE INDEX IF NOT EXISTS voters_house ON voters(booth, house_no);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY, voter_id INTEGER, payload TEXT, created_at TEXT,
    attempts INTEGER DEFAULT 0, last_error TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_db_path():
    # Inside a packaged Flet app this points at the app's private storage.
    base = os.environ.get("FLET_APP_STORAGE_DATA") or str(Path.home() / ".boothpulse")
    Path(base).mkdir(parents=True, exist_ok=True)
    return str(Path(base) / "boothpulse.db")


class Store:
    def __init__(self, path=None):
        self.path = path or default_db_path()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # -- key/value -----------------------------------------------------------

    def get(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value)))
            self._db.commit()

    def device_id(self):
        did = self.get("device_id")
        if not did:
            did = uuid.uuid4().hex
            self.set("device_id", did)
        return did

    def clear_session(self, wipe_data=True):
        """Sign out. Unsent changes are kept unless wipe_data is True."""
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key NOT IN ('device_id', 'server_url')")
            if wipe_data:
                self._db.execute("DELETE FROM voters")
                self._db.execute("DELETE FROM outbox")
            self._db.commit()

    # -- voters ----------------------------------------------------------------

    def upsert_voters(self, voters, overwrite_pending=False):
        """Store voters from the server. A voter with unsent local changes is
        skipped so the server copy does not hide what the agent just did."""
        if not voters:
            return 0
        with self._lock:
            pending = set() if overwrite_pending else {
                r[0] for r in self._db.execute("SELECT DISTINCT voter_id FROM outbox")
            }
            rows = []
            for v in voters:
                if v["id"] in pending:
                    continue
                rows.append([
                    int(v[c]) if c in ("needs_assistance", "contact_consent") and v.get(c) is not None
                    else v.get(c, "" if c in ("phone", "notes", "assistance_note") else None)
                    for c in VOTER_COLUMNS
                ])
            placeholders = ",".join("?" * len(VOTER_COLUMNS))
            self._db.executemany(
                f"INSERT OR REPLACE INTO voters({','.join(VOTER_COLUMNS)}) VALUES({placeholders})", rows
            )
            self._db.commit()
            return len(rows)

    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        d["needs_assistance"] = bool(d.get("needs_assistance"))
        d["contact_consent"] = bool(d.get("contact_consent"))
        return d

    def voter(self, voter_id):
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM voters WHERE id=?", (voter_id,)).fetchone())

    def search(self, booth, q="", status=None, assistance=False, limit=200):
        sql, args = "SELECT * FROM voters WHERE booth=?", [booth]
        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            if q.isdigit():
                sql += " AND (serial_no=? OR house_no=? OR epic LIKE ?)"
                args += [int(q), q, like]
            else:
                sql += " AND (name LIKE ? OR relative_name LIKE ? OR epic LIKE ? OR house_no=?)"
                args += [like, like, like, q]
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if assistance:
            sql += " AND needs_assistance=1 AND status != 'vote_done'"
        sql += " ORDER BY serial_no LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql, args)]

    def household(self, booth, house_no, exclude_id=None):
        if not house_no:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM voters WHERE booth=? AND house_no=? AND id != ? ORDER BY serial_no",
                (booth, house_no, exclude_id or 0),
            ).fetchall()
        return [self._row(r) for r in rows]

    def counts(self, booth):
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) n FROM voters WHERE booth=? GROUP BY status", (booth,)
            ).fetchall()
            assist = self._db.execute(
                "SELECT COUNT(*) FROM voters WHERE booth=? AND needs_assistance=1 AND status != 'vote_done'",
                (booth,),
            ).fetchone()[0]
        c = {r["status"]: r["n"] for r in rows}
        c["total"] = sum(c.values())
        c["assistance_open"] = assist
        return c

    # -- local edits + outbox --------------------------------------------------

    def record_change(self, voter_id, fields: dict):
        """Apply a change locally and queue it for the server."""
        event_id = str(uuid.uuid4())
        payload = {"event_id": event_id, "voter_id": voter_id, "client_ts": now_iso(), **fields}
        with self._lock:
            sets, args = [], []
            for k, v in fields.items():
                if k in VOTER_COLUMNS:
                    sets.append(f"{k}=?")
                    args.append(int(v) if isinstance(v, bool) else v)
            if fields.get("status") == "needs_assistance":
                sets.append("needs_assistance=1")
            if fields.get("contact_consent") is False:
                sets.append("phone=''")
            if sets:
                self._db.execute(f"UPDATE voters SET {', '.join(sets)} WHERE id=?", args + [voter_id])
            self._db.execute(
                "INSERT INTO outbox(event_id, voter_id, payload, created_at) VALUES(?,?,?,?)",
                (event_id, voter_id, json.dumps(payload), payload["client_ts"]),
            )
            self._db.commit()
        return event_id

    def outbox_batch(self, limit=200):
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM outbox ORDER BY created_at LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def outbox_remove(self, event_ids):
        if not event_ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE event_id=?", [(e,) for e in event_ids])
            self._db.commit()

    def outbox_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def has_pending(self, voter_id):
        with self._lock:
            return self._db.execute("SELECT 1 FROM outbox WHERE voter_id=? LIMIT 1", (voter_id,)).fetchone() is not None
