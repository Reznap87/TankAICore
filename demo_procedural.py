#!/usr/bin/env python3
"""
Demo: Procedural Memory steuert den Planner.

Lauf 1: Neues Ziel → simulierter Plan wird erstellt.
Danach wird das Pattern für diese isolierte Demo ausdrücklich manuell gesät.
Lauf 2: Der Planner bekommt das markierte Demo-Pattern.
"""

from tankai import TankAI, get_llm
from tankai.core.long_term_memory import LongTermMemory


def main() -> None:
    tank = TankAI(
        llm=get_llm("mock"), verbose=True, use_ltm=False, enable_tools=False
    )
    tank.ltm = LongTermMemory(in_memory=True, embedder="hashing")
    tank.tools.register_defaults(ltm=tank.ltm)

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

    # Simulations-Runs werden absichtlich nicht automatisch als erfolgreiches
    # Verfahren gelernt. Für diese isolierte Demo wird das Pattern manuell gesät.
    if r1.plan:
        tank.ltm.promote_to_procedure(r1.plan, 0.5, r1.final_answer[:120])

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
