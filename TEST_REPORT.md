# TankAI 1.10.0-module-ownership — Testbericht

**Statusdatum:** 11. September 2026

**Releasevertrag:** `TankAI-Core-1.10.0-module-ownership` · `ProjectState` Schema 6

## Unreleased: External-Agent-Joblisten-Paginierung v1 — Nachweis 11. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 217 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Pagination-, External-Agent-API- und Reality-Contract-Prüfungen: 3 PASS
- `python -m pip check`: PASS
- `npm audit --omit=dev --offline`: 0 bekannte Funde
- TypeScript 7.0.2 `tsc --noEmit`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- geprüft: eine Liste mit 102 eigenen Jobs bleibt über zwei Seiten vollständig und ohne
  Duplikate erreichbar; identische Zeitstempel werden deterministisch aufgelöst
- geprüft: Standardlimit 100, frei wählbares Limit 1 bis 100, versionierter
  `next_cursor` und maschinenlesbare Capability-Discovery
- geprüft: fremde beziehungsweise nicht mehr freigegebene Cursor sowie leere, zu große,
  mehrfache und unbekannte Parameter liefern neutral HTTP 400, ohne Werte zu spiegeln
- lokale Wrangler-Typgenerierung und der vollständige Worker-Dry-Run wurden von der externen
  Netzwerkfreigabe der Build-Umgebung blockiert; Docker/Podman ist lokal nicht installiert.
  Worker-Artefakt, Compose-Prüfung, Produktions-Container-Build und Container-Smoke bleiben
  deshalb verpflichtende GitHub-CI-Gates vor einem Merge
- keine Queue, kein Worker, Service-Agent oder Token außerhalb isolierter temporärer
  Testdatenbanken aktiviert beziehungsweise erzeugt; keine Secrets gelesen oder verändert, kein
  Provideraufruf und kein Deployment

## Unreleased: External-Agent-Jobverlauf v1 — Nachweis 10. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 216 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Queue-, External-Agent-API- und Reality-Contract-Prüfungen: 3 PASS
- `python -m pip check`: PASS
- `npm audit --omit=dev`: 0 bekannte Funde
- Wrangler 4.124.0 Typgenerierung und TypeScript 7.0.2 `tsc --noEmit`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- geprüft: eigener Agenten-Job liefert `queued`, `leased`, `running` und `succeeded` in
  chronologischer Reihenfolge; ein Abbruch liefert `queued` und `cancelled`
- geprüft: höchstens 100 neue Zustände werden ausgegeben und `truncated_before=true` markiert
  vorhandene ältere Zustände
- geprüft: ein anderer Service-Agent erhält trotz gleicher Repository-Freigabe `404`; die Antwort
  enthält keine internen Eventdetails, Akteur-/Worker-IDs, Fence-Daten, Fehler-/Hostpfade oder
  globale Queue-Sequenz
- der vollständige lokale Wrangler-Dry-Run erreichte Typgenerierung und Typecheck, konnte den
  konfigurierten Container jedoch ohne installierte Docker-/Podman-Runtime nicht bauen.
  Worker-Artefakt, Compose-Prüfung, Produktions-Container-Build und Container-Smoke bleiben
  deshalb verpflichtende GitHub-CI-Gates vor einem Merge
- keine Queue, kein Worker, Service-Agent oder Token außerhalb isolierter temporärer
  Testdatenbanken aktiviert beziehungsweise erzeugt; keine Secrets gelesen oder verändert, kein
  Provideraufruf und kein Deployment

## Unreleased: Single-Host-Konfigurations-Readiness — Nachweis 9. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 215 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Bootstrap-Readiness- und Reality-Contract-Prüfungen: 3 PASS
- `python -m pip check`: PASS
- `npm audit --omit=dev`: 0 bekannte Funde
- Wrangler 4.124.0 Typgenerierung und TypeScript 7.0.2 `tsc --noEmit`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- geprüft: fehlende Service-Agenten-/Token-Konfiguration ergibt Exitcode 2 und `ready=false`;
  aktive Policy, gültiges Git-Repository, berechtigter Agent und passender nicht abgelaufener
  Token ergeben Exitcode 0 und `ready=true`
- geprüft: Token-Widerruf macht denselben Receipt sofort wieder fail-closed; Member dürfen den
  workspaceweiten Status nicht abrufen; eine Registrierung, deren Pfad keine gültige Git-Wurzel
  mehr ist, zählt nicht als einsatzbereites Repository
- geprüft: Receipt enthält nur aggregierte Zähler und weder Token-Geheimnisse/-Metadaten noch
  Hostpfade; Host- und Runtime-Status bleiben ausdrücklich außerhalb dieses Snapshots
- der lokale Worker-Dry-Run wurde nach erfolgreicher Typgenerierung durch die externe
  Netzwerkfreigabe der Build-Umgebung blockiert; Docker/Podman ist lokal nicht installiert.
  Worker-Dry-Run, Compose-Prüfung, Produktions-Container-Build und Container-Smoke bleiben daher
  verpflichtende GitHub-CI-Gates vor einem Merge
- außerhalb isolierter temporärer Testdatenbanken keine Queue, kein Worker, kein Service-Agent
  oder Token aktiviert beziehungsweise erzeugt; keine Secrets gelesen oder verändert, kein
  Provideraufruf und kein Deployment

## Unreleased: lokale Qwen-Modellintegrität — Nachweis 8. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 213 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte lokale Runtime- und Reality-Contract-Prüfungen: 10 PASS
- POSIX-Shell-Syntaxprüfung des Modell-Initializers: PASS
- `python -m pip check`: PASS
- `npm audit --omit=dev`: 0 bekannte Funde
- Wrangler 4.124.0 Typgenerierung, TypeScript 7.0.2 `tsc --noEmit` und Worker-Dry-Run mit
  `--containers-rollout=none`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- geprüft: erfolgreicher Download wird nur nach passender SHA-256 atomar installiert;
  Prüfsummenabweichungen löschen die temporäre Datei und hinterlassen kein scheinbar gültiges
  Modell
- geprüft: eine manipulierte persistierte Modelldatei wird weder überschrieben noch gestartet;
  der Init-Dienst beendet sich fail-closed
- geprüft: Compose bindet denselben unveränderlichen Serverimage-Digest an Init und Inferenz und
  erzwingt die Reihenfolge `service_completed_successfully` → `service_healthy` → TankAI
- geprüft: derselbe Check startet den eigentlichen `llama.cpp`-Prozess erst nach erfolgreicher
  Prüfung und verhindert damit einen Bypass über automatische Container-Neustarts
- die lokale Umgebung besitzt keine Docker-/Podman-Runtime; die echte zusammengeführte
  Compose-Auswertung und der Produktions-Container-Smoke bleiben deshalb Pflichtchecks der
  GitHub-CI vor einem Merge
- kein GGUF heruntergeladen, kein Container oder Provider gestartet, keine Secrets gelesen oder
  verändert und kein Deployment ausgelöst

## Unreleased: lokaler Qwen2.5-Coder-Runtime-Pfad — Nachweis 7. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 207 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte lokale Runtime-, Action-Pin-, Workflow- und Reality-Contract-Prüfungen: 7 PASS
- `python -m pip check`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- offizieller `ghcr.io/ggml-org/llama.cpp:server`-OCI-Index am 7. September 2026 auf
  `sha256:7fa75431b8a78f9528cab4aaf65e8ae3e13da546a3cc7221247ca81bab864d84`
  aufgelöst; Index enthält Linux amd64, arm64 und s390x
- Modellrepository-`main` auf Revision `1f629da0c8bed16b9e50cee91c70693650e66c35`
  aufgelöst und diese Revision statt eines beweglichen Zweigs gebunden
- veröffentlichte Q4_K_M-Dateigröße 4,68 GB und SHA-256
  `1664fccab734674a50763490a8c6931b70e3f2f8ec10031b54806d30e5f956b6` dokumentiert
- geprüft: TankAI wartet auf `service_healthy`, nutzt ausschließlich
  `http://llama:8080/v1`, veröffentlicht Port 8080 nicht und ändert die Produktions-Compose-
  Defaults nicht
- keine Modelldatei heruntergeladen, kein Container oder Provider gestartet, keine Secrets
  gelesen oder verändert und kein Deployment

## Unreleased: repositoryweite Node.js-24-Action-Runtime — lokaler Nachweis 6. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 204 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Action-Pin-, Workflow-, Deploy-Gate- und Reality-Contract-Prüfungen: 17 PASS
- `python -m pip check`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- offizielle Release-Tags per `git ls-remote` gegen die vollständig verwendeten Commit-SHAs
  geprüft: `actions/checkout@v7.0.1`, `actions/setup-python@v7.0.0`,
  `actions/setup-node@v7.0.0` und `actions/upload-artifact@v6.0.0`
- die `action.yml`-Metadaten aller vier exakten Commits deklarieren `runs.using: node24`
- geprüft: sämtliche sechs Checkout-, drei Setup-Python-, drei Setup-Node- und eine
  Upload-Artifact-Verwendung entsprechen den freigegebenen unveränderlichen Pins
- geprüft: Workflow-Trigger, Berechtigungen, Production-Environment, Secret-Zugriffe,
  exakte Deploy-Bestätigung sowie Test-, Build-, Preflight-, Backup- und Deploy-Befehle sind
  unverändert
- kein manueller Workflow ausgeführt, keine Secrets gelesen oder verändert, kein Provideraufruf
  und kein Deployment

## Unreleased: CI-Action-Runtime Node.js 24 — lokaler Nachweis 5. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 204 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Action-Pin-, Workflow- und Reality-Contract-Prüfungen: 4 PASS
- `python -m pip check`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- offizielle Release-Tags per `git ls-remote` gegen die unveränderlich verwendeten vollständigen
  Commit-SHAs geprüft: `actions/checkout@v7.0.0`, `actions/setup-python@v7.0.0` und
  `actions/setup-node@v7.0.0`
- die `action.yml`-Metadaten aller drei exakten Commits deklarieren `runs.using: node24`
- geprüft: Python 3.12, Node.js 22, Caches, Berechtigungen, Install-, Test-, Build- und
  Container-Smoke-Schritte sind unverändert
- der Pull-Request-Lauf muss zusätzlich bestätigen, dass beide Pflichtjobs erfolgreich sind und
  keine Node.js-20-Action-Runtime-Warnung mehr erzeugen
- keine Secrets gelesen oder verändert, keine Produktionsworkflows ausgeführt, kein
  Provideraufruf und kein Deployment

## Unreleased: External-Agent-Job-Preflight v1 — lokaler Nachweis 4. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 203 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Gateway-, Queue- und Reality-Contract-Prüfungen: 46 PASS
- `python -m pip check`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- geprüft: gültiger Preflight, Repository-Scope-Ablehnung, Image-/Ressourcen-Admission,
  identische stabile Prüflogik für Preflight und Submit sowie strukturierte
  Schema-Validierung
- geprüft: kein Development-Job, keine Agent-Job-Freigabe, keine Queue-Kapazitäts- oder
  Idempotenzreservierung; Audit-Protokollierung bleibt aktiv
- dynamische Idempotenz-, Queue-Kapazitäts- und Nutzerlimit-Prüfungen werden im Receipt als
  aufgeschoben ausgewiesen und erst beim Submit atomar geprüft
- lokale TypeScript-/Wrangler- und Container-Prüfung war ohne installierte npm-Artefakte und
  Docker-Runtime nicht ausführbar; die verpflichtenden GitHub-Jobs `test` und `cloudflare`
  müssen deshalb vor jedem Merge Typprüfung, Worker-Dry-Run, Produktions-Container-Build und
  Container-Smoke vollständig bestätigen
- keine Secrets gesetzt oder gelesen, keine Queue oder Runtime aktiviert, kein Provideraufruf
  und kein Deployment

## Unreleased: External-Agent-Validierungsfehler v1 — lokaler Nachweis 3. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 202 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Gateway- und Reality-Contract-Prüfungen: 9 PASS
- `python -m pip check`: PASS
- `wrangler 4.124.0 types`: Typdatei vollständig erzeugt
- `typescript 7.0.2 tsc --noEmit`: PASS
- Worker-Bundle-Dry-Run mit `--containers-rollout=none`: PASS
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- geprüft: kompatibles Textfehlerfeld, Fehlerformat v1, JSON-Pointer, stabile Codes,
  Gesamtanzahl, 20-Fehler-Grenze und explizites Kürzungskennzeichen
- Negativtest mit 26 unbekannten Feldern bestätigt, dass Eingabewerte und ungewöhnliche
  frei gewählte Feldnamen weder in Fehlerdetails noch in Pydantic-Meldungen gespiegelt werden
- Capabilities und Job-Schema veröffentlichen Fehlerformat, Version und Obergrenze; Admission-
  Fehler aus Scope-, Repository- und Queue-Regeln bleiben getrennt
- keine Secrets gesetzt oder gelesen, keine Queue oder Runtime aktiviert, kein Provideraufruf und
  kein Deployment
- vollständiger Container-Build und Container-Smoke bleiben verpflichtende GitHub-CI-Gates für
  den Pull Request

## Unreleased: External-Agent-Job-Schema v1 — lokaler Nachweis 2. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 202 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Gateway- und Reality-Contract-Prüfungen: 9 PASS
- `wrangler 4.124.0 types`: PASS
- `typescript 7.0.2 tsc --noEmit`: PASS
- Worker-Bundle-Dry-Run mit `--containers-rollout=none`: PASS; vollständiger lokaler
  Container-Dry-Run erkennt erwartungsgemäß die in dieser Umgebung fehlende Docker-Runtime
- `git diff --check` und Secret-Pattern-Scan des Inkrements: PASS
- geprüft: Bearer-Pflicht, Capability-Discovery, stabiler Schema-Identifier und Draft,
  unbekannte Felder, Pflichtfelder, Idempotenz-/Prioritätsgrenzen sowie vollständige
  `WorkerPipelineJob`- und Isolationsstruktur
- veröffentlichtes Schema und Submit-Endpunkt verwenden dasselbe Pydantic-Modell; serverseitige
  Scope-, Repository-, Queue-, Image- und Ressourcenprüfungen bleiben unverändert verbindlich
- keine Secrets gesetzt oder gelesen, keine Queue oder Runtime aktiviert, kein Provideraufruf und
  kein Deployment
- vollständiger Container-Build und Container-Smoke bleiben verpflichtende GitHub-CI-Gates für
  den Pull Request

## Unreleased: Single-Host-Runner-Doctor — lokaler Nachweis 1. September 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 202 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- gezielte Host-Readiness- und Reality-Contract-Prüfungen: 7 PASS
- tatsächlicher read-only CLI-Fail-Closed-Lauf in der Build-Umgebung: Exitcode 2 wie erwartet;
  Root-Prozess, fehlendes `/srv/tankai`-Layout und fehlende Docker-Runtime wurden erkannt
- geprüft: Linux/WSL2-Abgrenzung, nicht-root Pflicht, CPU/RAM/Festspeicher-Mindestwerte,
  vollständige lokale Pfade, nicht world-writable Berechtigungen, 9p/DrvFS/NFS/SMB-Sperren,
  Linux-/Rootless-/Cgroup-v2-Runtimeprofil und maschinenlesbarer JSON-Receipt
- keine Pfade erzeugt, keine Queue gestartet, keine Secrets gelesen, kein Provideraufruf und
  kein Deployment
- vollständiger Wrangler-Dry-Run und Container-Build bleiben verpflichtende GitHub-CI-Gates für
  den Pull Request

## Unreleased: Masterplan Reality Sync 5.7.0 — lokaler Nachweis 31. August 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 196 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- `wrangler 4.124.0 types`: PASS
- `typescript 7.0.2 tsc --noEmit`: PASS
- `git diff --check`: PASS
- Drift-Regressionstest für den aktuellen Reality Contract: 1 PASS
- Secret-Pattern-Scan des Inkrements: PASS
- geprüft: Repository-Implementierungsstand und Production-Runtime-Basis bleiben getrennt;
  erledigte repositoryseitige Provider-Gates werden nicht erneut geöffnet; externe Blocker aus
  Issue #25 und der unabhängige Single-Host-Runner-Pfad sind ausdrücklich benannt
- keine Secrets gesetzt oder gelesen, kein Provideraufruf und kein Deployment
- der vollständige Wrangler-Dry-Run und Container-Build bleiben verpflichtende GitHub-CI-Gates
  für den Pull Request

## Unreleased: Service-Agent Operator CLI — lokaler Nachweis 30. August 2026

- `python -m compileall -q tankai tests`: PASS
- `python -m pytest -q`: 195 PASS
- `PYTHONUTF8=1 python -m tankai --selftest`: 24 PASS
- `tsc --noEmit`: PASS
- gezielte CLI-Lifecycle- und Berechtigungsprüfungen: 2 PASS
- geprüft: Anlegen/Auflisten, einmalige Token-Ausgabe, Token-Metadaten ohne
  Secret, Widerruf, Deaktivierung mit Token-Widerruf, Member-Blockade und
  Ablehnung nicht registrierter Repository-IDs
- keine Secrets gesetzt oder gelesen, kein Provideraufruf, kein Deployment
- lokaler Wrangler-Dry-Run in dieser Umgebung wegen blockiertem Netzwerkzugriff
  nicht ausgeführt; der verpflichtende GitHub-CI-Job `cloudflare` bleibt das
  Integrationsgate

## Unreleased: External Agent Gateway v1 — lokaler Nachweis 29. August 2026

Der Feature-Branch wurde mit folgenden nicht mutierenden Prüfungen gegen den
aktuellen Repository-Stand geprüft:

```text
python -m compileall -q tankai tests
python -m pytest -q
PYTHONUTF8=1 python -m tankai --selftest
git diff --check
```

Ergebnis:

- Python-Compile: bestanden,
- Pytest: 193 bestanden, 0 fehlgeschlagen,
- TankAI-Self-Test: 24 bestanden, 0 fehlgeschlagen,
- Diff-Whitespace-Prüfung: bestanden.

Der neue Ende-zu-Ende-Test prüft Session- und CSRF-geschützte
Agentenverwaltung, einmalige Token-Ausgabe, Bearer-Authentifizierung,
Scope- und Repository-Sperren, agentenspezifische Job-Sichtbarkeit,
Idempotenz, serverseitige Agenten-ID-Namensräume, Ergebnisfilterung ohne
Hostpfade, Job-Abbruch und sofortigen Token-Widerruf.

Dieser lokale Nachweis ist kein GitHub-CI-, Merge-, Deployment- oder
Production-Receipt. Die vorhandenen Cloudflare- und Produktionsgates wurden
dadurch nicht erneut ausgeführt oder autorisiert.

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
