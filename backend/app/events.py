"""Persistent, cursor-friendly event protocol."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import db


@dataclass(frozen=True)
class EventRecord:
    id: int
    run_id: str
    node_id: str
    session_id: str
    kind: str
    source: str
    provenance: str
    payload: dict
    ts: str


class EventStore:
    def append(self, run_id: str, kind: str, source: str, payload: dict,
               node_id: str = "", session_id: str = "",
               provenance: str = "deterministic",
               conn: sqlite3.Connection | None = None) -> EventRecord:
        sql = (
            "INSERT INTO events(run_id,node_id,session_id,kind,source,provenance,payload,ts) "
            "VALUES(?,?,?,?,?,?,?,?)"
        )
        args = (run_id, node_id, session_id, kind, source, provenance,
                json.dumps(payload, ensure_ascii=False), db.now())
        if conn is None:
            event_id = db.execute(sql, args)
            row = db.one("SELECT * FROM events WHERE id=?", (event_id,))
        else:
            event_id = conn.execute(sql, args).lastrowid
            found = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            row = dict(found) if found else None
        if not row:
            raise RuntimeError(f"event {event_id} was not persisted")
        return self._record(row)

    def list(self, run_id: str, after_id: int = 0, limit: int = 500) -> list[EventRecord]:
        limit = max(1, min(500, int(limit)))
        rows = db.query(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
            (run_id, max(0, int(after_id)), limit),
        )
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: dict) -> EventRecord:
        return EventRecord(
            id=row["id"], run_id=row["run_id"], node_id=row["node_id"] or "",
            session_id=row["session_id"] or "", kind=row["kind"], source=row["source"],
            provenance=row["provenance"], payload=db.json_load(row["payload"], {}), ts=row["ts"],
        )


events = EventStore()
