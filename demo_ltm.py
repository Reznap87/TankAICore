#!/usr/bin/env python3
"""
Demo der langfristigen Speicherarchitektur.

Führt zwei ähnliche Ziele hintereinander aus.
Beim zweiten Lauf sollte das LTM relevantes Vorwissen liefern.
"""

from tankai import TankAI
from tankai.core.long_term_memory import LongTermMemory


def main() -> None:
    # In dieser Umgebung nutzen wir In-Memory, damit keine Disk-I/O-Fehler auftreten.
    # Für echte Persistenz: in_memory=False und Pfade angeben.
    tank = TankAI(
        verbose=True,
        use_ltm=True,
        ltm_db="tankai_ltm.db",
        ltm_vectors="tankai_vectors.npz",
    )
    # Erzwinge In-Memory falls Disk Probleme macht
    if tank.ltm is None or True:
        tank.ltm = LongTermMemory(in_memory=True, embedder="torch")

    print("\n========== LAUF 1 ==========\n")
    r1 = tank.run(
        goal_description=(
            "Erkläre die wichtigsten Vorteile und Risiken "
            "von Multi-Agenten-Systemen im Vergleich zu einzelnen großen Sprachmodellen."
        ),
        definition_of_done=(
            "Strukturierte Gegenüberstellung mit mindestens drei Vorteilen und drei Risiken."
        ),
    )

    print("\n========== LAUF 2 (ähnliches Ziel – sollte LTM nutzen) ==========\n")
    r2 = tank.run(
        goal_description=(
            "Was sind Stärken und Schwächen von Multi-Agenten-Architekturen "
            "gegenüber monolithischen LLMs?"
        ),
        definition_of_done=(
            "Klare Liste von Stärken und Schwächen, überprüfbar."
        ),
    )

    print("\n" + "=" * 60)
    print("ZUSAMMENFASSUNG")
    print("=" * 60)
    print(f"Lauf 1 Status: {r1.status.value}")
    print(f"Lauf 2 Status: {r2.status.value}")
    if tank.ltm:
        print(f"LTM:          {tank.ltm.summary()}")
        hits = tank.ltm.retrieve("Multi-Agenten Vorteile Risiken", k=4)
        print(f"Beispiel-Retrieval ({len(hits)} Treffer):")
        for h in hits:
            print(f"  [{h['memory_type']}|score={h['score']:.2f}] {h['content'][:100]}...")
        tank.ltm.close()


if __name__ == "__main__":
    main()
