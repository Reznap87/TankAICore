# TankAI 1.9.0-agent-governance-v2 — Testbericht

## Umgesetzt

- vollständiger TECH-AI-V2-Rollenkatalog,
- persistierte Agenten-Governance mit Standardgrenzen 40/80/5/3/1/4,
- versionierter Agenten-Arbeitsvertrag,
- Entwicklungszyklen mit Gesamtlimit und fail-closed Zykluswechsel,
- Modulkapazitätsprüfung für konfliktarme Parallelität,
- erweiterter Agenten-Lebenszyklus,
- spezialisierte unabhängige Review-, QA- und Security-Rollen,
- ProjectState-Schema 5 mit Migration alter Zustände,
- CLI-Ausgabe für Governance und Zyklusstatus.

## Tatsächlich ausgeführte Prüfungen

```text
python -m compileall -q tankai tests
pytest -q
python -m tankai --selftest
git diff --check
```

Zwischenergebnis nach der ersten Gesamtprüfung:

```text
146 bestanden
3 fehlgeschlagen
```

Ursache: Drei bestehende Migrationstests erwarteten weiterhin Schema 4. Die Erwartungen wurden auf das tatsächlich eingeführte Schema 5 aktualisiert.

Ergebnis nach Korrektur:

```text
Python-Kompilierung: bestanden
Pytest: 151 bestanden, 0 fehlgeschlagen
TankAI-Self-Test: 24 bestanden, 0 fehlgeschlagen
```

## Neue Regressionstests

- Governance-Standardwerte entsprechen 40/80/5/3/1/4,
- spezialisierte Rollen `realtime_audio`, `ai_safety` und `project_persistence` sind validierbar,
- Arbeitsvertrag übernimmt Abnahmekriterien, Pflicht-Tests, Priorität, Deadlock-Regeln und Zyklus-ID,
- Modulkapazität blockiert einen zusätzlichen Agenten trotz disjunkter Pfade,
- Gesamtlimit pro Zyklus bleibt auch nach Beendigung einzelner Agenten erhalten,
- expliziter Zykluswechsel setzt das Zyklusbudget zurück,
- Zykluswechsel mit nicht-terminalem Agenten wird blockiert,
- Chief Architect, Quality Lead und AppSec können ihre unabhängigen Gates ausführen,
- Schema-4-Zustand wird auf Schema 5 und vollständige Agentenverträge migriert.

## Nicht getestet

- reale Docker-/Podman-Ausführung, weil in der Build-Umgebung keine Container-Runtime installiert ist,
- reale GitHub-Actions-Ausführung, weil weiterhin kein beschreibbares GitHub-Repository mit der Connector-App verbunden ist,
- verteilter Mehrhost-Agentenbetrieb,
- öffentliches TLS-Deployment,
- reale OpenAI-/Anthropic- und Brave-/Tavily-Aufrufe.
