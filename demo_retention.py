#!/usr/bin/env python3
"""Demo: Retention-Policies und Cold-Storage."""

from datetime import datetime, timezone, timedelta
from tankai.core.long_term_memory import LongTermMemory


def main() -> None:
    ltm = LongTermMemory(in_memory=True, embedder="torch")

    # Frische, wichtige Einträge
    ltm.add_semantic(
        "Multi-Agenten ermöglichen Spezialisierung und parallele Ausführung.",
        confidence=0.9,
        source="demo",
    )
    ltm.add_semantic(
        "Critic-Layer und Receipts senken Halluzinationsrate.",
        confidence=0.85,
        source="demo",
    )

    # Alter, schwach genutzter Eintrag
    weak = ltm.add_semantic(
        "Nebensächliche Beobachtung ohne großen Nutzen für zukünftige Runs.",
        confidence=0.25,
        source="demo",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    ltm._conn.execute(
        "UPDATE memory_entries SET created_at=?, last_accessed=?, access_count=0 WHERE id=?",
        (old, old, weak.id),
    )
    ltm._conn.commit()

    # Etwas älterer, mittlerer Eintrag → sollte warm werden
    mid = ltm.add_semantic(
        "Plan-Muster Research→Analysis→Writing funktioniert gut bei Vergleichsfragen.",
        confidence=0.7,
        source="demo",
    )
    mid_age = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    ltm._conn.execute(
        "UPDATE memory_entries SET created_at=?, last_accessed=? WHERE id=?",
        (mid_age, mid_age, mid.id),
    )
    ltm._conn.commit()

    print("=== VOR Retention ===")
    print(ltm.summary())
    print(ltm.retention_summary())

    stats = ltm.apply_retention(
        warm_after_days=7,
        cold_after_days=30,
        min_confidence_for_hot=0.4,
    )
    print("\nRetention-Stats:", stats)

    print("\n=== NACH Retention ===")
    print(ltm.summary())
    print(ltm.retention_summary())

    print("\nRetrieval (nur Hot/Warm im Vector-Index):")
    for h in ltm.retrieve("Multi-Agenten Spezialisierung Critic", k=5):
        print(f"  [{h['score']:.2f}|conf={h['confidence']:.2f}] {h['content'][:70]}...")

    ltm.close()


if __name__ == "__main__":
    main()
