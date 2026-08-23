# TankAI 1.10.0-module-ownership — Testbericht

**Statusdatum:** 23. August 2026

**Releasevertrag:** `TankAI-Core-1.10.0-module-ownership` · `ProjectState` Schema 6

## Verifizierter Bezugsstand

Die Synchronisierung begann auf dem öffentlichen `main`-Commit
`bf8df633fd8f961108278c6e9a09348da3934cd4` mit dem Git-Tree
`df8440b82fb95dceac6b9c78e25dde11cf36a7d6`. Der Synchronisationspatch ändert
Release-, Betriebs- und Dokumentationsmetadaten, aber keine Orchestrierungslogik. Er wird auf dem
Branch `docs/1.10.0-release-sync` über [PR #12](https://github.com/Reznap87/TankAICore/pull/12)
geprüft; dieser Bericht dokumentiert weder einen Merge noch einen produktiven Deploy.

Das veraltete Root-Receipt `tankai_selftest_result.json` wurde unverändert nach
`docs/history/tankai_selftest_result_1.8.0-publication-ledger_2026-07-29.json` verschoben. Beide
Dateistände besitzen denselben Git-Blob `99f17bf25d28c4c76f8702c6ed84b2fd88e0d181`; die Datei ist
historische Evidenz und kein aktueller 1.10.0-Testbeleg.

Die öffentliche GitHub-Actions-API bestätigt [Run 15](https://github.com/Reznap87/TankAICore/actions/runs/32535005507)
als erfolgreich und ordnet ihn dem PR-Head `db45a043b219e291c81085e844b2c581945b3ca2` zu. Der Job
testete GitHubs synthetischen PR-Merge `9fb6449ee8bb573bf9d91b3b257fa7a35de0fd03`; dessen Tree
`df8440b82fb95dceac6b9c78e25dde11cf36a7d6` ist exakt identisch mit dem finalen `main`-Merge-Tree.
Beide Jobs einschließlich Compile, Test, Self-Test, Cloudflare-Typgenerierung, TypeScript,
Wrangler-Dry-Run, Worker-Artefaktprüfung und Produktions-Container-Build sind grün. Der Masterplan
und die statische Testinventur weisen dafür 159/159 Pytests und 24/24 TankAI-Self-Tests aus. Dieser
externe Referenzlauf belegt damit den in `main` übernommenen Basistree; er ist kein CI-Receipt für
einen späteren PR-Head dieses Synchronisationspatches und kein Beleg für einen Live-Produktionsdeploy.

## Umgesetzt und synchronisiert

- aktueller Releasevertrag `TankAI-Core-1.10.0-module-ownership`,
- `ProjectState` Schema 6 mit Migration vorhandener Schema-5-Zustände,
- persistentes Capability-Register mit Modul, Owner, Status, Source-Referenz, Abhängigkeiten,
  Schnittstelle und Abnahmetests,
- explizite Task-Bindung über `capability_id` und `capability_action`,
- Sperren gegen ungültige `CREATE`-/Folgeaktionen und parallele aktive Arbeit an derselben Capability,
- manueller, main-gebundener und an das GitHub-Environment `production` gebundener Cloudflare-Produktionsworkflow,
- unveränderlich gepinnte externe GitHub Actions und Wrangler `4.124.0`,
- synchronisierte README-, Changelog-, Betriebs-, Orchestrator- und Runtime-Versionsmarker.

## Aktuelle lokale Revalidierung

### Bestanden

```text
Python 3.12.13: python -m compileall -q tankai tests
Ergebnis: bestanden

Pytest-Collection: 159 Tests
Workflow-/Action-Pinning-/Web-Health-Auswahl: 11 bestanden, 0 fehlgeschlagen

Node.js 22.23.2 / npm 10.9.8
npm ci --ignore-scripts: gesperrte Abhängigkeiten installiert
npm audit --omit=dev: 0 bekannte Funde
Wrangler 4.124.0 types --check: bestanden
TypeScript 7.0.2 tsc --noEmit: bestanden
Worker-only Dry-Run mit --containers-rollout=none: bestanden
Worker-Artefakt: 55.152 Byte
Worker-Artefakt SHA-256: 40C1A29539BA0D5B9FDA0B7304D41C6FC87E8E9883F4613612B0394B7F8E8348

git diff --check: bestanden
```

Der Worker-only-Dry-Run verwendet weiterhin `--dry-run` und veröffentlicht nichts. Die zusätzliche
Option `--containers-rollout=none` überspringt ausschließlich Containerimage und -Rollout, weil auf
diesem Windows-Rechner keine Docker-Laufzeit vorhanden ist.

### Lokal nicht vollständig reproduzierbar

Die vollständige Suite ist für GitHub CI auf Ubuntu definiert. Unter Windows scheitert der erste
Linux-Sicherheitstest nach fünf bestandenen Tests bereits daran, dass `os.getuid` nicht existiert.
Ein nicht abbrechender Gesamtlauf zeigte weitere Windows-Abweichungen und blockierte später ohne
CPU-Fortschritt; nur dieser exakt identifizierte Testprozess wurde nach dem begrenzten Wartefenster
beendet. Dieses Ergebnis wird nicht als 159/159-Receipt ausgegeben.

Der Self-Test lief nach Setzen von UTF-8 bis zum Ende: 22 Checks bestanden. Die beiden übrigen
Checks `Development-Queue` und `Lease-Fencing` scheiterten beim Aufräumen offener SQLite-Dateien
an Windows-Fehler 32. Auch dieses Ergebnis wird nicht als 24/24-Receipt ausgegeben.

Der exakte Befehl `wrangler deploy --dry-run --outdir dist` benötigt wegen des in `wrangler.jsonc`
konfigurierten lokalen Dockerfiles eine funktionierende Docker CLI. Typgenerierung und TypeScript
bestanden; der vollständige Container-Dry-Run und der separate `docker build` sind lokal mangels
Docker nicht ausführbar. WSL ist ebenfalls nicht installiert.

## Neue Regressionstests im 1.10.0-Tree

- Schema 5 migriert auf Schema 6 und ergänzt das Capability-Register.
- Der vollständige Capability-Vertrag wird persistent gespeichert.
- Ungültige Capability-Aktionen und konkurrierende aktive Tasks werden blockiert.
- Der Produktionsworkflow besitzt keinen automatischen Push-Trigger.
- Produktion verlangt `main` und die exakte Bestätigung `DEPLOY`.
- Production Environment, serielle Concurrency und Secret-Bindung bleiben erzwungen.
- Alle externen Actions müssen auf vollständige 40-stellige Commit-SHAs zeigen.
- Die Action-Pinning-Prüfung erwartet neun reale Action-Verwendungen und kann nicht leer bestehen.

## Produktionsgrenze

Nicht ausgeführt und nicht behauptet wurden ein Live-Cloudflare-Deploy, die externe Verifikation der
GitHub-Environment-Schutzregeln, DNS-/HTTPS-Abnahme, Landingpage-/Readiness-/Auth-Prüfung,
Provideraktivierung, reale Modellaufrufe oder Secret-Änderungen.
Der vollständige Linux-/Cloudflare-/Container-CI-Status ist für jeden konkreten PR-Head extern in
GitHub Actions zu prüfen; ein grüner Lauf autorisiert weder Merge noch Deploy.

---

## Historischer Testbericht: TankAI 1.9.0-agent-governance-v2

### Umgesetzt

- vollständiger TECH-AI-V2-Rollenkatalog,
- persistierte Agenten-Governance mit Standardgrenzen 40/80/5/3/1/4,
- versionierter Agenten-Arbeitsvertrag,
- Entwicklungszyklen mit Gesamtlimit und fail-closed Zykluswechsel,
- Modulkapazitätsprüfung für konfliktarme Parallelität,
- erweiterter Agenten-Lebenszyklus,
- spezialisierte unabhängige Review-, QA- und Security-Rollen,
- ProjectState-Schema 5 mit Migration alter Zustände,
- CLI-Ausgabe für Governance und Zyklusstatus.

### Tatsächlich ausgeführte Prüfungen

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

### Neue Regressionstests

- Governance-Standardwerte entsprechen 40/80/5/3/1/4,
- spezialisierte Rollen `realtime_audio`, `ai_safety` und `project_persistence` sind validierbar,
- Arbeitsvertrag übernimmt Abnahmekriterien, Pflicht-Tests, Priorität, Deadlock-Regeln und Zyklus-ID,
- Modulkapazität blockiert einen zusätzlichen Agenten trotz disjunkter Pfade,
- Gesamtlimit pro Zyklus bleibt auch nach Beendigung einzelner Agenten erhalten,
- expliziter Zykluswechsel setzt das Zyklusbudget zurück,
- Zykluswechsel mit nicht-terminalem Agenten wird blockiert,
- Chief Architect, Quality Lead und AppSec können ihre unabhängigen Gates ausführen,
- Schema-4-Zustand wird auf Schema 5 und vollständige Agentenverträge migriert.

### Nicht getestet

- reale Docker-/Podman-Ausführung, weil in der Build-Umgebung keine Container-Runtime installiert ist,
- reale GitHub-Actions-Ausführung, weil weiterhin kein beschreibbares GitHub-Repository mit der Connector-App verbunden ist,
- verteilter Mehrhost-Agentenbetrieb,
- öffentliches TLS-Deployment,
- reale OpenAI-/Anthropic- und Brave-/Tavily-Aufrufe.
