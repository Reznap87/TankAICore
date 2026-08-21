"""
Memory-System mit Provenance, Gültigkeit und Konflikterkennung.
Unterstützt In-Memory und persistente SQLite-Speicherung.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from .models import MemoryEntry, utcnow


class Memory:
    """
    Speichert Wissen mit Herkunft und Gültigkeitsstatus.
    Kann optional in eine SQLite-Datei persistieren.
    """

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        """
        Args:
            db_path: Pfad zur SQLite-Datei. None = rein im Speicher.
        """
        self.db_path = Path(db_path) if db_path else None
        self._entries: dict[str, MemoryEntry] = {}
        self._conn: Optional[sqlite3.Connection] = None

        if self.db_path:
            self._init_db()
            self._load_from_db()

    # ────────────────────────── Public API ──────────────────────────

    def add(
        self,
        content: str,
        source: str,
        *,
        validity: str = "unknown",
        confidence: float = 0.5,
        related_goal_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> MemoryEntry:
        """Fügt einen neuen Memory-Eintrag hinzu und speichert ihn ggf. persistent."""
        entry = MemoryEntry(
            content=content,
            source=source,
            validity=validity,
            confidence=confidence,
            related_goal_id=related_goal_id,
            metadata=metadata or {},
        )
        self._entries[entry.id] = entry

        if self._conn:
            self._save_entry(entry)

        return entry

    def get(self, entry_id: str) -> Optional[MemoryEntry]:
        return self._entries.get(entry_id)

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        include_unverified: bool = False,
    ) -> list[MemoryEntry]:
        """Keyword-Suche; standardmäßig werden nur validierte Einträge zurückgegeben."""
        query_lower = query.lower()
        results = [
            entry
            for entry in self._entries.values()
            if query_lower in entry.content.lower()
            and (include_unverified or entry.validity == "valid")
        ]
        results.sort(key=lambda e: e.confidence, reverse=True)
        return results[:limit]

    def mark_conflict(self, entry_id: str, conflicting_id: str) -> None:
        entry = self._entries.get(entry_id)
        other = self._entries.get(conflicting_id)
        if entry and other:
            if conflicting_id not in entry.conflicts_with:
                entry.conflicts_with.append(conflicting_id)
            if entry_id not in other.conflicts_with:
                other.conflicts_with.append(entry_id)
            entry.validity = "conflicting"
            other.validity = "conflicting"
            if self._conn:
                self._save_entry(entry)
                self._save_entry(other)

    def get_by_goal(self, goal_id: str) -> list[MemoryEntry]:
        return [e for e in self._entries.values() if e.related_goal_id == goal_id]

    def all_entries(self) -> list[MemoryEntry]:
        return list(self._entries.values())

    def clear(self) -> None:
        """Löscht alle Einträge (Vorsicht bei persistenter DB)."""
        self._entries.clear()
        if self._conn:
            self._conn.execute("DELETE FROM memory")
            self._conn.commit()

    def __len__(self) -> int:
        return len(self._entries)

    def summary(self) -> str:
        valid = sum(1 for e in self._entries.values() if e.validity == "valid")
        conflicting = sum(1 for e in self._entries.values() if e.validity == "conflicting")
        unknown = sum(1 for e in self._entries.values() if e.validity == "unknown")
        mode = f"sqlite:{self.db_path.name}" if self.db_path else "in-memory"
        return (
            f"Memory ({mode}): {len(self)} Einträge | "
            f"valid={valid} | unknown={unknown} | conflicting={conflicting}"
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ────────────────────────── SQLite Internals ──────────────────────────

    def _init_db(self) -> None:
        assert self.db_path is not None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                validity TEXT NOT NULL,
                confidence REAL NOT NULL,
                related_goal_id TEXT,
                conflicts_with TEXT,
                created_at TEXT NOT NULL,
                metadata TEXT
            )
        """)
        self._conn.commit()

    def _load_from_db(self) -> None:
        assert self._conn is not None
        rows = self._conn.execute("SELECT * FROM memory").fetchall()
        for row in rows:
            entry = MemoryEntry(
                id=row["id"],
                content=row["content"],
                source=row["source"],
                validity=row["validity"],
                confidence=row["confidence"],
                related_goal_id=row["related_goal_id"],
                conflicts_with=json.loads(row["conflicts_with"] or "[]"),
                created_at=row["created_at"],
                metadata=json.loads(row["metadata"] or "{}"),
            )
            self._entries[entry.id] = entry

    def _save_entry(self, entry: MemoryEntry) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO memory
                (id, content, source, validity, confidence, related_goal_id,
                 conflicts_with, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.content,
                    entry.source,
                    entry.validity,
                    entry.confidence,
                    entry.related_goal_id,
                    json.dumps(entry.conflicts_with),
                    entry.created_at.isoformat(),
                    json.dumps(entry.metadata),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            # Bei I/O-Problemen im Sandbox-Environment nur warnen
            print(f"[Memory] Warnung: konnte nicht speichern ({e})")
