from __future__ import annotations

import sqlite3
from pathlib import Path

from ..schemas import MemoryRecord


class MemoryStore:
    """Quality-gated memory index; raw episodes remain in Recorder."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS memories (memory_id TEXT PRIMARY KEY, task_id TEXT, outcome TEXT, record_json TEXT NOT NULL)")
        self.db.commit()

    def add(self, record: MemoryRecord, *, quality_ok: bool = True) -> None:
        if not quality_ok:
            raise ValueError("only quality-checked episodes may enter memory")
        self.db.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?,?)",
                        (str(record.memory_id), record.task_id, record.outcome, record.model_dump_json()))
        self.db.commit()

    def search(self, *, task_id: str | None = None, outcome: str | None = None, tag: str | None = None,
               limit: int = 20) -> list[MemoryRecord]:
        clauses, args = [], []
        if task_id:
            clauses.append("task_id=?")
            args.append(task_id)
        if outcome:
            clauses.append("outcome=?")
            args.append(outcome)
        query = "SELECT record_json FROM memories" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " LIMIT ?"
        rows = self.db.execute(query, (*args, limit)).fetchall()
        records = [MemoryRecord.model_validate_json(row[0]) for row in rows]
        if tag:
            records = [record for record in records if tag in record.tags]
        return records

    def close(self) -> None:
        self.db.close()
