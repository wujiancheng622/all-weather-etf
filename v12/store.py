"""Transactional local snapshots and idempotent event receipts, with stale-writer checks."""
from __future__ import annotations
from pathlib import Path
import json
import sqlite3
from .config import canonical_json, digest


class StateConflict(RuntimeError):
    pass


class Journal:
    def __init__(self, path: str | Path, identity: dict):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, identity TEXT NOT NULL, state TEXT NOT NULL, state_hash TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL)")

    def close(self):
        self.db.close()

    def initialize(self, state: dict):
        text = canonical_json(state)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM snapshot").fetchone():
                raise StateConflict("Existing state: use explicit resume; never overwrite")
            self.db.execute("INSERT INTO snapshot VALUES (1,0,?,?,?)", (canonical_json(self.identity), text, digest(state)))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def read(self):
        row = self.db.execute("SELECT version,identity,state,state_hash FROM snapshot WHERE id=1").fetchone()
        if row is None:
            raise StateConflict("No initial state")
        version, identity, text, checksum = row
        state = json.loads(text)
        if identity != canonical_json(self.identity):
            raise StateConflict("Configuration/data/run identity changed; resume rejected")
        if digest(state) != checksum:
            raise StateConflict("Snapshot checksum mismatch")
        return int(version), state

    def seen(self, event_id: str, payload: dict) -> bool:
        row = self.db.execute("SELECT payload_hash FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return False
        if row[0] != digest(payload):
            raise StateConflict("Event ID reused with different content")
        return True

    def commit(self, expected_version: int, event_id: str, payload: dict, state: dict, *, fail_before_commit=False):
        if not event_id:
            raise ValueError("Event ID required")
        text, payload_text = canonical_json(state), canonical_json(payload)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            version, _ = self.read()
            if version != expected_version:
                raise StateConflict("Stale writer: reload before processing the next event")
            if self.seen(event_id, payload):
                raise StateConflict("Duplicate event must be handled without another mutation")
            next_version = version+1
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (next_version, event_id, digest(payload), payload_text))
            self.db.execute("UPDATE snapshot SET version=?,state=?,state_hash=? WHERE id=1", (next_version, text, digest(state)))
            if fail_before_commit:
                raise RuntimeError("Injected failure before transaction commit")
            self.db.execute("COMMIT")
            return next_version
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def receipts(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM events ORDER BY sequence")]
