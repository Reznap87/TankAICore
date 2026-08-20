#!/usr/bin/env python3
"""
TankAI Prototyp — Demo

Startet einen kompletten PLAN → ROUTE → VERIFY → LEARN Durchlauf
mit dem eingebauten MockLLM und persistentem Memory.
"""

from tankai import TankAI


def main() -> None:
    # memory_db sorgt dafür, dass Wissen zwischen Läufen erhalten bleibt
    tank = TankAI(verbose=True, memory_db="tankai_memory.db")

    result = tank.run(
        goal_description=(
            "Erkläre die wichtigsten Vorteile und Risiken "
            "von Multi-Agenten-Systemen im Vergleich zu einzelnen großen Sprachmodellen."
        ),
        definition_of_done=(
            "Eine strukturierte Gegenüberstellung mit mindestens "
            "drei Vorteilen und drei Risiken liegt vor. "
            "Die Antwort ist klar und überprüfbar."
        ),
        constraints=[
            "Keine unbelegten Superlative",
            "Technisch präzise bleiben",
        ],
    )

    print("\n" + "=" * 60)
    print("RUN ABGESCHLOSSEN")
    print("=" * 60)
    print(f"Status:          {result.status.value}")
    print(f"Dauer:           {result.duration_seconds}s")
    print(f"Receipts:        {len(result.receipts)}")
    print(f"Critiques:       {len(result.critiques)}")
    print(f"Memory-Einträge: {result.memory_entries_created}")
    print(f"Memory gesamt:   {tank.memory.summary()}")
    print()
    print("Finale Antwort:")
    print("-" * 40)
    print(result.final_answer)

    tank.memory.close()


if __name__ == "__main__":
    main()
