#!/usr/bin/env python3
"""
Demo: Procedural Memory steuert den Planner.

Lauf 1: Neues Ziel → Plan wird normal erstellt und als Procedure gespeichert.
Lauf 2: Ähnliches Ziel → Planner bekommt das Pattern und markiert reused_pattern.
"""

from tankai import TankAI
from tankai.core.long_term_memory import LongTermMemory


def main() -> None:
    tank = TankAI(verbose=True, use_ltm=True)
    tank.ltm = LongTermMemory(in_memory=True, embedder="torch")

    print("\n" + "=" * 60)
    print("LAUF 1 — erstes Ziel (kein Procedural Memory)")
    print("=" * 60 + "\n")

    r1 = tank.run(
        goal_description=(
            "Vergleiche Vorteile und Nachteile von Multi-Agenten-Systemen "
            "gegenüber einzelnen großen Sprachmodellen."
        ),
        definition_of_done="Strukturierte Gegenüberstellung mit klaren Punkten.",
    )

    print("\nPlan-Rationale Lauf 1:")
    print(" ", r1.plan.rationale if r1.plan else "—")
    print("Receipt Plan:", r1.receipts[0].output_summary if r1.receipts else "—")
    print("LTM:", tank.ltm.summary())

    print("\n" + "=" * 60)
    print("LAUF 2 — ähnliches Ziel (sollte Pattern wiederverwenden)")
    print("=" * 60 + "\n")

    r2 = tank.run(
        goal_description=(
            "Welche Stärken und Schwächen haben Multi-Agenten-Architekturen "
            "im Vergleich zu monolithischen LLMs?"
        ),
        definition_of_done="Klare Liste von Stärken und Schwächen.",
    )

    print("\nPlan-Rationale Lauf 2:")
    print(" ", r2.plan.rationale if r2.plan else "—")
    print("Receipt Plan:", r2.receipts[0].output_summary if r2.receipts else "—")
    print("LTM:", tank.ltm.summary())

    reused = False
    if r2.receipts:
        reused = r2.receipts[0].details.get("reused_pattern", False)
    print("\n→ Pattern wiederverwendet:", reused)

    tank.ltm.close()


if __name__ == "__main__":
    main()
