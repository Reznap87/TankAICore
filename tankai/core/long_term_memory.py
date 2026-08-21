"""
Langfristige Speicherarchitektur für TankAI.

Schichten:
- Working   : flüchtig (wird nicht hier gehalten)
- Episodic  : Runs, Receipts, Pläne (SQLite)
- Semantic  : Fakten + Embeddings (VectorStore)
- Procedural: erfolgreiche Plan-Muster (ebenfalls Semantic mit Typ-Tag)

Consolidation wandelt episodische Runs in Semantic-Einträge um.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from .models import MemoryEntry, Plan, Receipt, RunResult, utcnow
from .llm import BaseLLM
from .vector_store import VectorStore
from .embeddings import BaseEmbedder, get_embedder


class LongTermMemory:
    """
    Vereinheitlichte Langzeit-Memory-Schnittstelle.
    """

    def __init__(
        self,
        db_path: str | Path = "tankai_ltm.db",
        vector_path: str | Path = "tankai_vectors.npz",
        embedding_dim: int = 384,
        *,
        in_memory: bool = False,
        embedder: str | BaseEmbedder = "hashing",
        cold_dir: str | Path = "tankai_cold",
        allow_in_memory_fallback: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if not in_memory else None
        self.vector_path = Path(vector_path)
        self.cold_dir = Path(cold_dir)
        self._in_memory = in_memory
        if not in_memory:
            self.cold_dir.mkdir(parents=True, exist_ok=True)

        if in_memory:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            try:
                if self.db_path:
                    self.db_path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(
                    str(self.db_path), check_same_thread=False, timeout=30
                )
            except sqlite3.Error as exc:
                if not allow_in_memory_fallback:
                    raise RuntimeError(
                        f"LTM-Datenbank konnte nicht geöffnet werden: {self.db_path}"
                    ) from exc
                print("[LTM] Disk nicht verfügbar – expliziter In-Memory-Fallback aktiv")
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._in_memory = True

        self._conn.row_factory = sqlite3.Row
        self._init_schema()

        # Vector-Persistenz nur wenn nicht pure in-memory
        vpath = None if self._in_memory else str(self.vector_path)
        if isinstance(embedder, str):
            emb = get_embedder(embedder, dim=embedding_dim) if embedder == "hashing" else get_embedder(embedder)
        else:
            emb = embedder
        try:
            self.vectors = VectorStore(dim=emb.dim, persist_path=vpath, embedder=emb)
        except Exception:
            self._conn.close()
            raise

    # ────────────────────────── Schema ──────────────────────────

    SCHEMA_VERSION = 2

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        if column not in self._table_columns(table):
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def _init_schema(self) -> None:
        """Erstellt und migriert das SQLite-Schema atomar und idempotent."""
        table_statements = [
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                goal_description TEXT NOT NULL,
                definition_of_done TEXT,
                status TEXT,
                final_answer TEXT,
                duration_seconds REAL,
                created_at TEXT NOT NULL,
                metadata TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                action TEXT,
                actor TEXT,
                input_summary TEXT,
                output_summary TEXT,
                success INTEGER,
                details TEXT,
                timestamp TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_entries (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                validity TEXT NOT NULL,
                confidence REAL NOT NULL,
                related_goal_id TEXT,
                related_run_id TEXT,
                conflicts_with TEXT,
                provenance TEXT,
                created_at TEXT NOT NULL,
                last_accessed TEXT,
                access_count INTEGER DEFAULT 0,
                retention_policy TEXT DEFAULT 'hot',
                metadata TEXT
            )
            """,
        ]
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_receipts_run ON receipts(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_entries(memory_type)",
            "CREATE INDEX IF NOT EXISTS idx_memory_goal ON memory_entries(related_goal_id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_run ON memory_entries(related_run_id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_retention ON memory_entries(retention_policy)",
        ]
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("BEGIN IMMEDIATE")
            for statement in table_statements:
                self._conn.execute(statement)

            # Migrationen müssen vor Indizes laufen. Genau daran scheiterte die alte DB.
            self._ensure_column("runs", "definition_of_done", "TEXT")
            self._ensure_column("runs", "metadata", "TEXT")
            self._ensure_column("memory_entries", "last_accessed", "TEXT")
            self._ensure_column("memory_entries", "access_count", "INTEGER DEFAULT 0")
            self._ensure_column(
                "memory_entries", "retention_policy", "TEXT DEFAULT 'hot'"
            )
            self._ensure_column("memory_entries", "metadata", "TEXT")
            self._conn.execute(
                "UPDATE memory_entries SET retention_policy = 'hot' "
                "WHERE retention_policy IS NULL OR retention_policy = ''"
            )
            self._conn.execute(
                "UPDATE memory_entries SET access_count = 0 WHERE access_count IS NULL"
            )

            for statement in index_statements:
                self._conn.execute(statement)
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self._conn.commit()
        except sqlite3.Error as exc:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            raise RuntimeError("LTM-Schema konnte nicht erstellt oder migriert werden") from exc

    # ────────────────────────── Episodic ──────────────────────────

    def store_episode(self, result: RunResult, goal_description: str) -> str:
        """Speichert einen kompletten Run (episodisch)."""
        run_id = result.goal_id  # wir nutzen goal_id als run_id

        self._conn.execute(
            """
            INSERT OR REPLACE INTO runs
            (id, goal_description, definition_of_done, status, final_answer,
             duration_seconds, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                goal_description,
                getattr(result, "definition_of_done", ""),
                result.status.value if hasattr(result.status, "value") else str(result.status),
                result.final_answer,
                result.duration_seconds,
                utcnow().isoformat(),
                json.dumps({"num_receipts": len(result.receipts)}),
            ),
        )

        for r in result.receipts:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO receipts
                (id, run_id, action, actor, input_summary, output_summary,
                 success, details, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.id,
                    run_id,
                    r.action,
                    r.actor.value if hasattr(r.actor, "value") else str(r.actor),
                    r.input_summary,
                    r.output_summary,
                    1 if r.success else 0,
                    json.dumps(r.details),
                    r.timestamp.isoformat() if hasattr(r.timestamp, "isoformat") else str(r.timestamp),
                ),
            )

        # Episodic Memory-Entry für den gesamten Run
        self._add_entry(
            content=f"Run: {goal_description}\nAntwort: {result.final_answer[:500]}",
            source="system:run",
            memory_type="episodic",
            validity="valid" if result.status.value == "completed" else "unknown",
            confidence=0.7,
            related_goal_id=result.goal_id,
            related_run_id=run_id,
            provenance=[r.id for r in result.receipts[:5]],
        )

        try:
            self._conn.commit()
        except sqlite3.Error as e:
            print(f"[LTM] SQLite-Warnung: {e}")
        return run_id

    # ────────────────────────── Semantic / Procedural ──────────────────────────

    def add_semantic(
        self,
        content: str,
        *,
        source: str = "system",
        confidence: float = 0.6,
        provenance: Optional[list[str]] = None,
        related_goal_id: Optional[str] = None,
        related_run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> MemoryEntry:
        return self._add_entry(
            content=content,
            source=source,
            memory_type="semantic",
            validity="valid",
            confidence=confidence,
            related_goal_id=related_goal_id,
            related_run_id=related_run_id,
            provenance=provenance or [],
            metadata=metadata,
            index_vector=True,
        )

    def promote_to_procedure(
        self,
        plan: Plan,
        success_score: float,
        goal_description: str,
    ) -> MemoryEntry:
        """Speichert einen erfolgreichen Plan als wiederverwendbares Muster."""
        steps_text = " → ".join(
            f"[{s.specialist_type}] {s.description}" for s in plan.steps
        )
        content = (
            f"Erfolgreiches Plan-Muster (score={success_score:.2f})\n"
            f"Ziel-ähnlich: {goal_description[:200]}\n"
            f"Schritte: {steps_text}\n"
            f"Rationale: {plan.rationale}"
        )
        return self._add_entry(
            content=content,
            source="system:procedure",
            memory_type="procedural",
            validity="valid",
            confidence=success_score,
            related_goal_id=plan.goal_id,
            provenance=[plan.id],
            metadata={"plan_id": plan.id, "num_steps": len(plan.steps)},
            index_vector=True,
        )

    # ────────────────────────── Consolidation ──────────────────────────

    def consolidate(self, run_id: str, llm: "BaseLLM | None" = None) -> list[MemoryEntry]:
        """
        Wandelt einen episodischen Run in hochwertige Semantic-Einträge um.

        Mit LLM:
          Das Modell extrahiert 2–5 klare, wiederverwendbare Fakten/Insights.
        Ohne LLM:
          Fallback auf einfache Heuristik (Final-Answer + erfolgreiche Receipts).
        """
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return []

        created: list[MemoryEntry] = []
        goal = row["goal_description"] or ""
        final = row["final_answer"] or ""
        status = row["status"] or ""

        receipts = self._conn.execute(
            "SELECT * FROM receipts WHERE run_id = ? ORDER BY timestamp",
            (run_id,),
        ).fetchall()

        if llm is not None:
            created.extend(self._consolidate_with_llm(llm, run_id, goal, final, status, receipts))
        else:
            created.extend(self._consolidate_heuristic(run_id, goal, final, receipts))

        return created

    def _consolidate_with_llm(
        self,
        llm: "BaseLLM",
        run_id: str,
        goal: str,
        final: str,
        status: str,
        receipts: list,
    ) -> list[MemoryEntry]:
        """LLM extrahiert strukturierte, wiederverwendbare Insights."""
        import json
        import re as _re

        receipt_lines = []
        for r in receipts:
            if r["success"] and r["output_summary"]:
                receipt_lines.append(
                    f"- [{r['actor']}|{r['action']}] {r['output_summary'][:220]}"
                )
        receipts_text = "\n".join(receipt_lines[:12]) or "(keine erfolgreichen Receipts)"

        system = (
            "Du bist der Memory-Consolidator eines Multi-Agenten-Systems. "
            "Deine Aufgabe: Aus einem abgeschlossenen Run klare, wiederverwendbare "
            "Wissenseinträge extrahieren. Jeder Eintrag soll für sich allein verständlich "
            "und für zukünftige ähnliche Aufgaben nützlich sein. "
            "Antworte ausschließlich mit validem JSON."
        )

        prompt = f"""Analysiere diesen abgeschlossenen Run und extrahiere 2–5 hochwertige Memory-Einträge.

Ziel: {goal}
Status: {status}

Finale Antwort:
{final[:1200]}

Wichtige Zwischenergebnisse:
{receipts_text}

Antworte im JSON-Format:
{{
  "entries": [
    {{
      "content": "Klarer, eigenständiger Fakt oder Insight (1–3 Sätze)",
      "confidence": 0.0-1.0,
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

Regeln:
- Keine Meta-Kommentare („Der Run war erfolgreich“).
- Jeder Eintrag muss inhaltlich nützlich für zukünftige ähnliche Ziele sein.
- Bevorzuge konkrete Aussagen gegenüber vagen Zusammenfassungen.
"""

        raw = llm.complete(prompt, system=system)
        created: list[MemoryEntry] = []

        try:
            match = _re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(match.group(0) if match else raw)
            entries = data.get("entries", [])
        except Exception:
            # Fallback: ganze Antwort als einen Eintrag
            entries = [{"content": raw[:600], "confidence": 0.5, "tags": ["fallback"]}]

        for e in entries:
            content = (e.get("content") or "").strip()
            if len(content) < 20:
                continue
            conf = float(e.get("confidence", 0.7))
            tags = e.get("tags") or []
            entry = self.add_semantic(
                content=content,
                source="consolidation:llm",
                confidence=min(max(conf, 0.0), 1.0),
                related_run_id=run_id,
                provenance=[run_id],
                metadata={"tags": tags, "goal_snippet": goal[:120]},
            )
            created.append(entry)

        return created

    def _consolidate_heuristic(
        self,
        run_id: str,
        goal: str,
        final: str,
        receipts: list,
    ) -> list[MemoryEntry]:
        """Einfacher Fallback ohne LLM."""
        created: list[MemoryEntry] = []

        if final and len(final) > 40:
            entry = self.add_semantic(
                content=final[:800],
                source="consolidation:final_answer",
                confidence=0.7,
                related_run_id=run_id,
                provenance=[run_id],
                metadata={"goal_snippet": goal[:120]},
            )
            created.append(entry)

        for r in receipts:
            if (
                r["success"]
                and str(r["action"]).startswith("execute_step")
                and len(r["output_summary"] or "") > 50
            ):
                entry = self.add_semantic(
                    content=r["output_summary"],
                    source=f"consolidation:{r['actor']}",
                    confidence=0.6,
                    related_run_id=run_id,
                    provenance=[r["id"]],
                )
                created.append(entry)

        return created


    def retrieve(
        self,
        query: str,
        *,
        k: int = 6,
        memory_types: Optional[list[str]] = None,
        min_score: float = 0.12,
    ) -> list[dict[str, Any]]:
        """
        Hybrid Retrieval:
        1. Vector-Suche über Semantic + Procedural
        2. Anreicherung mit Metadaten aus der DB
        """
        hits = self.vectors.search(query, k=k * 2, min_score=min_score)
        results = []

        for entry_id, score, meta in hits:
            row = self._conn.execute(
                "SELECT * FROM memory_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if not row:
                continue
            mtype = row["memory_type"]
            if memory_types and mtype not in memory_types:
                continue

            # Access tracking
            self._conn.execute(
                "UPDATE memory_entries SET access_count = access_count + 1, "
                "last_accessed = ? WHERE id = ?",
                (utcnow().isoformat(), entry_id),
            )

            results.append({
                "id": entry_id,
                "content": row["content"],
                "source": row["source"],
                "memory_type": mtype,
                "confidence": row["confidence"],
                "score": score,
                "related_run_id": row["related_run_id"],
                "metadata": json.loads(row["metadata"] or "{}"),
            })
            if len(results) >= k:
                break

        try:
            self._conn.commit()
        except sqlite3.Error as e:
            print(f"[LTM] SQLite-Warnung: {e}")
        return results

    def format_retrieval_context(self, hits: list[dict]) -> str:
        if not hits:
            return ""
        lines = ["### Relevantes Langzeitgedächtnis"]
        for h in hits:
            lines.append(
                f"- [{h['memory_type']}|{h['source']}|score={h['score']:.2f}|conf={h['confidence']:.2f}] "
                f"{h['content'][:280]}"
            )
        return "\n".join(lines)

    # ────────────────────────── Internals ──────────────────────────

    def _add_entry(
        self,
        content: str,
        source: str,
        memory_type: str,
        validity: str,
        confidence: float,
        related_goal_id: Optional[str] = None,
        related_run_id: Optional[str] = None,
        provenance: Optional[list] = None,
        metadata: Optional[dict] = None,
        index_vector: bool = False,
        retention_policy: str = "hot",
    ) -> MemoryEntry:
        entry_id = str(uuid4())
        now = utcnow().isoformat()

        self._conn.execute(
            """
            INSERT INTO memory_entries
            (id, content, source, memory_type, validity, confidence,
             related_goal_id, related_run_id, conflicts_with, provenance,
             created_at, last_accessed, access_count, retention_policy, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                content,
                source,
                memory_type,
                validity,
                confidence,
                related_goal_id,
                related_run_id,
                "[]",
                json.dumps(provenance or []),
                now,
                now,
                0,
                retention_policy,
                json.dumps(metadata or {}),
            ),
        )
        try:
            self._conn.commit()
        except sqlite3.Error as e:
            print(f"[LTM] SQLite-Warnung: {e}")

        if index_vector and memory_type in ("semantic", "procedural"):
            self.vectors.add(
                entry_id,
                content,
                metadata={
                    "memory_type": memory_type,
                    "source": source,
                    "confidence": confidence,
                },
            )

        return MemoryEntry(
            id=entry_id,
            content=content,
            source=source,
            validity=validity,
            confidence=confidence,
            related_goal_id=related_goal_id,
            metadata=metadata or {},
        )


    # ────────────────────────── Retention & Cold Storage ──────────────────────────

    def apply_retention(
        self,
        *,
        warm_after_days: float = 7.0,
        cold_after_days: float = 30.0,
        min_confidence_for_hot: float = 0.4,
        max_hot_entries: Optional[int] = None,
    ) -> dict[str, int]:
        """
        Wendet Retention-Policies an.

        Regeln (vereinfacht):
        - Niedrige Confidence + selten genutzt → eher cold
        - Alter > cold_after_days und wenig Zugriffe → cold
        - Alter > warm_after_days → warm
        - Sonst hot
        - Optional: max_hot_entries erzwingt die ältesten/schwächsten nach warm/cold

        Gibt Statistik zurück: {"to_warm": n, "to_cold": n, "stayed_hot": n}
        """
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        rows = self._conn.execute(
            "SELECT id, confidence, created_at, last_accessed, access_count, "
            "retention_policy, memory_type FROM memory_entries"
        ).fetchall()

        stats = {"to_warm": 0, "to_cold": 0, "stayed_hot": 0, "already_cold": 0}

        # Sortiere für max_hot_entries: schwächste zuerst
        scored = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            except Exception:
                created = now
            try:
                last = datetime.fromisoformat(
                    (row["last_accessed"] or row["created_at"]).replace("Z", "+00:00")
                )
            except Exception:
                last = created

            age_days = (now - created).total_seconds() / 86400
            idle_days = (now - last).total_seconds() / 86400
            access = row["access_count"] or 0
            conf = row["confidence"] or 0.5
            current = row["retention_policy"] or "hot"

            # Score: höher = eher hot behalten
            keep_score = conf * 0.5 + min(access, 10) * 0.05 - age_days * 0.01 - idle_days * 0.02

            target = "hot"
            if current == "cold":
                target = "cold"
            elif age_days >= cold_after_days and access < 2:
                target = "cold"
            elif conf < min_confidence_for_hot and access == 0 and age_days > 1:
                target = "cold"
            elif age_days >= warm_after_days or idle_days >= warm_after_days:
                target = "warm"

            scored.append((row["id"], current, target, keep_score, row["memory_type"]))

        # max_hot_entries: die schwächsten Hot-Kandidaten nach warm schieben
        if max_hot_entries is not None:
            hot_candidates = sorted(
                [s for s in scored if s[2] == "hot"],
                key=lambda x: x[3],
            )
            overflow = len(hot_candidates) - max_hot_entries
            if overflow > 0:
                for i in range(overflow):
                    eid, cur, _, sc, mt = hot_candidates[i]
                    # update target in scored
                    for j, item in enumerate(scored):
                        if item[0] == eid:
                            scored[j] = (eid, cur, "warm", sc, mt)
                            break

        for eid, current, target, _, mtype in scored:
            if current == target:
                if target == "hot":
                    stats["stayed_hot"] += 1
                elif target == "cold":
                    stats["already_cold"] += 1
                continue

            if target == "cold":
                if self._move_to_cold(eid):
                    stats["to_cold"] += 1
            elif target == "warm":
                self._set_retention(eid, "warm")
                # Warm bleibt im Vector-Index, aber markiert
                stats["to_warm"] += 1
            else:
                self._set_retention(eid, "hot")
                stats["stayed_hot"] += 1

        return stats

    def _set_retention(self, entry_id: str, policy: str) -> None:
        self._conn.execute(
            "UPDATE memory_entries SET retention_policy = ? WHERE id = ?",
            (policy, entry_id),
        )
        try:
            self._conn.commit()
        except sqlite3.Error as e:
            print(f"[LTM] SQLite-Warnung: {e}")

    def _move_to_cold(self, entry_id: str) -> bool:
        """
        Schreibt den Eintrag ins Cold-Storage (JSONL) und
        entfernt ihn aus dem heißen Index (DB-Flag + Vector löschen).
        Der DB-Eintrag bleibt mit retention_policy=cold als Stub erhalten
        (content wird gekürzt), damit Provenance nachvollziehbar bleibt.
        """
        row = self._conn.execute(
            "SELECT * FROM memory_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not row:
            return False

        # In Cold-Datei schreiben (wenn nicht pure in-memory)
        record = {
            "id": row["id"],
            "content": row["content"],
            "source": row["source"],
            "memory_type": row["memory_type"],
            "validity": row["validity"],
            "confidence": row["confidence"],
            "related_goal_id": row["related_goal_id"],
            "related_run_id": row["related_run_id"],
            "provenance": row["provenance"],
            "created_at": row["created_at"],
            "last_accessed": row["last_accessed"],
            "access_count": row["access_count"],
            "metadata": row["metadata"],
            "archived_at": utcnow().isoformat(),
        }

        if self._in_memory:
            # Ohne persistentes Archiv darf der Volltext nicht vernichtet werden.
            return False
        try:
            cold_file = self.cold_dir / "memory_archive.jsonl"
            with open(cold_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as e:
            print(f"[LTM] Cold-Write Warnung: {e}")
            return False

        # Erst nach erfolgreich persistiertem Archiv-Eintrag aus dem Index entfernen.
        self.vectors.delete(entry_id)

        # Stub in DB behalten (content gekürzt)
        stub = (row["content"] or "")[:80] + " … [archived]"
        self._conn.execute(
            """
            UPDATE memory_entries
            SET content = ?, retention_policy = 'cold', metadata = ?
            WHERE id = ?
            """,
            (
                stub,
                json.dumps({
                    **json.loads(row["metadata"] or "{}"),
                    "archived": True,
                    "original_length": len(row["content"] or ""),
                }),
                entry_id,
            ),
        )
        try:
            self._conn.commit()
        except sqlite3.Error as e:
            print(f"[LTM] SQLite-Warnung: {e}")
        return True

    def restore_from_cold(self, entry_id: str) -> bool:
        """Lädt einen Eintrag aus dem Cold-Archive zurück nach Hot/Warm."""
        if self._in_memory:
            return False
        cold_file = self.cold_dir / "memory_archive.jsonl"
        if not cold_file.exists():
            return False

        found = None
        try:
            with open(cold_file, "r", encoding="utf-8") as f:
                for line in f:
                    rec = json.loads(line)
                    if rec.get("id") == entry_id:
                        found = rec
                        break
        except (OSError, json.JSONDecodeError) as e:
            print(f"[LTM] Cold-Read Warnung: {e}")
            return False

        if not found:
            return False

        self._conn.execute(
            """
            UPDATE memory_entries
            SET content = ?, retention_policy = 'warm', metadata = ?
            WHERE id = ?
            """,
            (
                found["content"],
                found.get("metadata") or "{}",
                entry_id,
            ),
        )
        try:
            self._conn.commit()
        except sqlite3.Error as e:
            print(f"[LTM] SQLite-Warnung: {e}")

        # Wieder in den Vector-Index
        if found.get("memory_type") in ("semantic", "procedural"):
            self.vectors.add(
                entry_id,
                found["content"],
                metadata={
                    "memory_type": found["memory_type"],
                    "source": found["source"],
                    "confidence": found["confidence"],
                },
            )
        return True

    def archive_old_runs(self, older_than_days: float = 60.0) -> int:
        """
        Schiebt komplette alte Runs (episodisch) ins Cold-Storage.
        Gibt die Anzahl archivierter Runs zurück.
        """
        from datetime import datetime, timezone, timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        rows = self._conn.execute(
            "SELECT id FROM runs WHERE created_at < ?", (cutoff,)
        ).fetchall()

        count = 0
        for row in rows:
            run_id = row["id"]
            # Zugehörige episodic entries nach cold
            entries = self._conn.execute(
                "SELECT id FROM memory_entries WHERE related_run_id = ? AND retention_policy != 'cold'",
                (run_id,),
            ).fetchall()
            for e in entries:
                self._move_to_cold(e["id"])
            count += 1
        return count

    def retention_summary(self) -> str:
        rows = self._conn.execute(
            "SELECT retention_policy, COUNT(*) as c FROM memory_entries GROUP BY retention_policy"
        ).fetchall()
        parts = [f"{r['retention_policy'] or 'hot'}={r['c']}" for r in rows]
        cold_size = 0
        if not self._in_memory:
            cold_file = self.cold_dir / "memory_archive.jsonl"
            if cold_file.exists():
                try:
                    cold_size = sum(1 for _ in open(cold_file, encoding="utf-8"))
                except OSError:
                    pass
        return f"Retention: {' | '.join(parts) or 'empty'} | cold_archive_lines={cold_size}"

    def summary(self) -> str:
        counts = self._conn.execute(
            "SELECT memory_type, COUNT(*) as c FROM memory_entries GROUP BY memory_type"
        ).fetchall()
        parts = [f"{r['memory_type']}={r['c']}" for r in counts] or ["empty"]
        runs = self._conn.execute("SELECT COUNT(*) as c FROM runs").fetchone()["c"]
        ret = self._conn.execute(
            "SELECT retention_policy, COUNT(*) as c FROM memory_entries GROUP BY retention_policy"
        ).fetchall()
        ret_parts = [f"{(r['retention_policy'] or 'hot')}={r['c']}" for r in ret] or ["none"]
        return (
            f"LTM: runs={runs} | vectors={len(self.vectors)} | "
            + " | ".join(parts)
            + " | retention: "
            + " | ".join(ret_parts)
        )

    def close(self) -> None:
        self._conn.close()
