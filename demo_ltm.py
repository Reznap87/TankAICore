#!/usr/bin/env python3
"""
Demo der langfristigen Speicherarchitektur im expliziten Mock-Modus.

Simulations-Runs werden nicht automatisch als Wissen konsolidiert. Deshalb wird
zwischen den Läufen ein klar gekennzeichneter Demo-Eintrag manuell gespeichert.
"""

from tankai import TankAI, get_llm


def main() -> None:
    # Die Demo verwendet echte lokale Persistenz, aber ausdrücklich ein Mock-LLM.
    tank = TankAI(
        llm=get_llm("mock"),
        verbose=True,
        use_ltm=True,
        ltm_db="tankai_ltm.db",
        ltm_vectors="tankai_vectors.npz",
    )
    # Demo-Modus bleibt ausdrücklich simuliert; Persistenz ist trotzdem real.

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

    if tank.ltm:
        tank.ltm.add_semantic(
            "Demo-Seed: Multi-Agenten-Systeme verteilen Aufgaben auf spezialisierte Rollen; "
            "zusätzliche Orchestrierung erhöht jedoch Komplexität und Latenz.",
            source="demo:manual_seed",
            confidence=0.5,
            metadata={"simulation_seed": True},
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
