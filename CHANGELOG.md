# Changelog

## Unreleased

### External-Agent-Job-Preflight v1

- Nicht einreihenden `POST /api/v1/jobs/preflight` für externe KI-Clients ergänzt.
- Preflight und echter Submit verwenden dieselbe interne Admission-Funktion für Scope-,
  Repository-, Policy-, Image-, Ressourcen-, Laufzeit-, Inline-Secret- und Payload-Gates.
- Der Receipt kennzeichnet den Snapshot ausdrücklich: kein Job, keine Queue- oder
  Idempotenzreservierung; dynamische Limits und sämtliche Regeln werden beim Submit erneut
  geprüft.
- Capability- und Schema-Discovery veröffentlichen den Preflight-Pfad maschinenlesbar.

### External-Agent-Validierungsfehler v1

- Abgewiesene v1-Job-Payloads liefern zusätzlich zum kompatiblen Textfehler begrenzte,
  versionierte Fehlercodes mit JSON-Pointer-Pfaden.
- Maximal 20 Details werden zurückgegeben; Gesamtzahl und Kürzungsstatus bleiben
  maschinenlesbar.
- Eingabewerte, interne Pydantic-Meldungen und Fehlerkontexte werden nicht gespiegelt;
  ungewöhnliche frei gewählte Feldnamen werden neutralisiert.
- Capabilities und Job-Schema veröffentlichen Version, Pfadformat und Fehlerobergrenze für
  automatische Clients.

### External-Agent-Job-Schema v1

- Authentifizierten, rein lesenden Endpunkt `/api/v1/job-schema` für den vollständigen
  JSON-Schema-Draft-2020-12-Vertrag eines externen Development-Auftrags ergänzt.
- `/api/v1/capabilities` veröffentlicht Methode, Submit-Pfad, Schema-Pfad und Schemaversion für
  maschinelle Discovery.
- API-Validierung und veröffentlichtes Schema verwenden dasselbe strikt typisierte Modell;
  bestehende Scope-, Repository-, Queue-, Image- und Ressourcen-Gates bleiben verbindlich.

### Single-Host-Runner-Readiness

- Rein lesenden JSON-Doctor für den Betrieb des Development-Runners auf Linux oder WSL2 ergänzt.
- Dediziertes nicht-root Konto, CPU/RAM/Festspeicher-Mindestwerte, lokales persistentes
  Speicherlayout und nicht world-writable Runner-Pfade werden fail-closed geprüft.
- Docker-/Podman-Profil muss Linux, rootless und Cgroup v2 melden; Windows-/Netzwerk-Mounts wie
  `/mnt/c`, 9p/DrvFS, NFS und SMB werden für Queue-, Fence-, Repository-, Worktree- und
  State-Persistenz abgewiesen.
- Der Doctor erstellt keine Pfade, startet keine Queue, liest keine Secrets und verändert keine
  Runtime- oder Produktionskonfiguration.

### External Agent Gateway v1

- Workspacegebundene Service-Agentenkonten mit sofortiger Deaktivierung ergänzt.
- Sicheren Operator-CLI-Lifecycle zum Anlegen, Auflisten, Token-Erzeugen,
  Widerrufen und Deaktivieren von Service-Agenten ergänzt; Token-Repositories
  werden gegen aktive Queue-Registrierungen geprüft.
- Zeitlich begrenzte, widerrufbare und ausschließlich gehasht gespeicherte
  Maschinen-Tokens mit getrennten Scopes und Repository-Allowlist ergänzt.
- Versionierte Bearer-API für Fähigkeiten, freigegebene Repositories sowie das
  Einreichen, Lesen und Abbrechen eigener Development-Jobs ergänzt.
- Agentenspezifische Job-Grants verhindern Einsicht in menschliche oder von
  anderen Service-Agenten eingereichte Aufträge.
- Maschinen-Idempotenzschlüssel werden serverseitig pro Agent namensräumlich
  getrennt; bestehende Queue-, Container-, Ressourcen- und Review-Gates bleiben
  unverändert verbindlich.
- HTTP-Regressionstest deckt Scope-, Repository-, Job-, Token- und
  Widerrufsgrenzen Ende-zu-Ende ab.

## 1.10.0-module-ownership

**Releasevertrag: `TankAI-Core-1.10.0-module-ownership` · ProjectState Schema 6**

### Capability- und Modulownership

- `ProjectState` Schema 6 mit persistentem Capability-Register eingeführt.
- Capability-Verträge binden stabile Capability- und Modul-IDs an Owner, Status, Source-Referenz, Abhängigkeiten, Schnittstelle und Abnahmetests.
- Task-Verträge können eine Capability und eine explizite Aktion (`CREATE`, `EXTEND`, `FIX`, `TEST`, `REVIEW`, `INTEGRATE`) referenzieren.
- Doppelte `CREATE`-Arbeit, implizite Erstellung durch andere Aktionen und parallele aktive Tasks für dieselbe Capability werden fail-closed blockiert.
- Registrierung unbekannter Capability-Abhängigkeiten wird abgewiesen.
- Schema-5-Zustände werden verlustarm auf Schema 6 migriert und erhalten ein leeres Capability-Register sowie explizite Task-Bindungsfelder.

### Release- und Deployment-Härtung

- Die bestehende Cloudflare-Worker-/Container-Konfiguration wurde mit dem verifizierten 1.10.0-Core zusammengeführt.
- Der CI-Pfad erzeugt Wrangler-Bindungstypen, führt den strikten TypeScript-Check aus, baut den Worker als Deployment-Dry-Run, prüft das Worker-Artefakt und baut das Produktions-Containerimage.
- Der Produktionsworkflow besitzt keinen automatischen Push-Trigger und verlangt auf `main` die exakte manuelle Bestätigung `DEPLOY`, das Environment `production` und serielle Concurrency.
- Alle externen GitHub Actions sind auf vollständige Commit-SHAs festgesetzt; der Produktionsworkflow verwendet Wrangler `4.124.0`.

### Tests

- Drei Regressionstests decken Schema-5-Migration, Capability-Persistenz sowie Aktions- und Parallelitätssperren ab.
- Drei Regressionstests erzwingen die manuelle Produktionsgrenze; zwei weitere erzwingen unveränderlich gepinnte externe Actions.
- Der vollständige Vertrag umfasst 159 Pytests und 24 TankAI-Self-Tests.

## 1.9.0-agent-governance-v2

### Agenten-Governance

- TECH-AI-V2-Rollenkatalog für mehr als 100 klar abgegrenzte Entwicklungs-, Prüf-, Betriebs- und Audio-Rollen.
- Persistierte Governance-Grenzen: 40 aktive Agenten, 80 Agenten pro Zyklus, Klontiefe 5, drei Kinder pro Agent, ein Schreiber pro Datei und vier Agenten pro Modul.
- Versionierte Agentenverträge mit Zyklus-ID, Abnahmekriterien, Pflicht-Tests, Priorität und Deadlock-Regeln.
- Persistente Entwicklungszyklen mit explizitem, fail-closed `begin-cycle`-Übergang.
- Erweiterter Agenten-Lebenszyklus und Status `waiting_for_review`, `merged`, `rejected` und `terminated`.
- Spezialisierte Quality-, Security- und Architekturrollen für unabhängige Gates.
- ProjectState-Schema 5 mit Migration alter Zustände.
- CLI-Status enthält Governance, Zyklus und strukturierte aktive Agenten.

### Korrekturen

- Versehentlich zurückgesetzte 1.8.0-README-, Changelog- und Testberichtsinhalte wurden vor der neuen Iteration aus dem bestätigten Commit wiederhergestellt.
- Doppelte `dependencies`-Deklaration im Spawn-Modell entfernt.

### Tests

- Neue Regressionen für Governance-Standardwerte, Rollenkatalog, Agentenvertrag, Modulgrenze, Zyklus-Gesamtlimit, Zykluswechsel, Gate-Rollen und Schema-4-Migration.

## 1.8.0-publication-ledger

### Implementiert

- Manipulationssichtbares SHA-256-Ereignisledger für Release-Publikationen.
- Verifizierte Google-Drive-Artefakt-Receipts mit Remote-ID, URL, Größe und SHA-256/SHA-1/MD5-Transferdigest.
- Verifizierte GitHub-Source-Receipts für den exakten geplanten Commit und Branch.
- Atomare Ledger-Speicherung mit thread- und prozessübergreifendem Lock.
- Lokale Release-Revalidierung und Vollständigkeitsmatrix pro Publikationsziel.
- Schutz vor fremden Domains, falschen Repositories, doppelten Receipts, lokalen Artefaktänderungen, Symlinks und unterbrochenen Hashketten.
- Neue CLI `tankai.dev_orchestrator.publication_cli`.
- GitHub-Workflow erzeugt einen Publikationsplan und zeichnet den ausgeführten GitHub-Commit auf.
- Dokumentation `docs/RELEASE_PUBLICATION.md`.

## 1.7.0-release-backup

### Implementiert

- Deterministische, secret-geprüfte Quellcode-Snapshots mit reproduzierbaren ZIP-Metadaten.
- Internes `BACKUP_MANIFEST.sha256` und `BACKUP_METADATA.json` pro Archiv.
- Externe Metadaten-, Manifest- und `SHA256SUMS`-Dateien.
- Verifikation ohne Extraktion mit Schutz vor Pfad-Traversal, Symlinks, Duplikaten und Manipulation.
- Ausschluss von Secrets, Datenbanken, Laufzeitdaten, Vektordateien, Git-Metadaten und Caches.
- Neue CLI `python -m tankai.dev_orchestrator.release_cli`.
- GitHub-CI und manueller Release-Backup-Workflow.
- Backup- und Recovery-Dokumentation.

## 1.6.0-container-reaper

- Fügt vollständige Queue-/Fence-Identitätslabels für Worker- und optionale Integrationscontainer hinzu.
- Fügt Runtime-Listing und Inspect-Validierung für verwaltete TankAI-Container hinzu.
- Implementiert einen mandanten- und workspacegebundenen Container-Reaper mit Dry-Run, Mindestalter, Live-Lease-/Fence-Schutz und exakter Stale-Bestätigung.
- Ergänzt den CLI-Befehl `reap-containers`.
- Blockiert unbekannte Container-Metadaten-Schlüssel und ein Überschreiben reservierter Labels.
- Ergänzt Regressionstests für Label-Propagation, Runtime-Parsing, Live-Schutz, Terminal-Cleanup, Fremdscope-Blockade und CLI-Dry-Run.

## 1.5.0-active-cancellation

- kooperative, fortlaufende Lease-/Fence-Prüfung während laufender Host- und Container-Kommandos,
- aktive Prozessgruppenbeendigung bei Guard-Verlust oder Zeitlimit,
- zusätzliche zwangsweise Entfernung benannter OCI-Container nach Abbruch,
- Queue-Heartbeat setzt ein unmittelbares Abbruchsignal für die laufende Worker-Pipeline,
- neuer sicherer Worktree-Reaper mit Dry-Run, ProjectState-Schutz und Dirty-Quarantäne,
- Operator-CLI `reap-worktrees` mit Owner-/Admin-Pflicht, Fence-/Queue-Gate und exakter stale-Run-Bestätigung,
- saubere Worktrees werden entfernt, Git-Branches standardmäßig erhalten,
- erhaltene Agent-Branches können kontrolliert wieder als Worktree eingehängt werden,
- 115 Regressionstests einschließlich aktiver Heartbeat-Abbruchkette, Container-Cleanup und Reaper-Scenarios.

## 1.4.0-lease-fencing

### Externes Lease-Fencing

- Separate `LeaseFenceStore`-Datenbank mit monotoner Epoche pro Repository.
- Opaque Tokens werden nur gehasht gespeichert; Acquire, Renew und Release verwenden atomare Compare-and-Swap-Prüfungen.
- Queue- und Fence-Lease werden bei Start, Heartbeat, Ausführung, Commit und Abschluss gemeinsam validiert.
- Lebender externer Fence überstimmt einen lokal abgelaufenen Queue-Timestamp und verhindert Doppelbelegung.
- Operator-Recovery mit exakter Repository-, Job- und Epochenbestätigung.
- Queue-Schema 3 mit `fence_epoch` und exklusivem partiellem Index pro aktivem Repository.

### Rootless-Runtime-Gate

- Docker-/Podman-Sicherheitsprofile werden als JSON ausgelesen.
- Online-Queue-Worker blockieren nicht-rootless oder nicht-Linux Runtimes.
- Neue `runtime_cli` für betriebliche Vorabprüfung.

### Tests

- Regressionen für monotone Epochen, Token-/Epochenverlust, Queue/Fence-Reconciliation, fail-closed Dispatcher, Commit-Gate, Operator-Recovery, Schema-Migration und Docker-/Podman-Rootless-Erkennung.

## 1.3.0-admission-queue

- Persistente SQLite-Development-Queue mit atomarer Claim-Transaktion, Lease-Tokens, Heartbeats, Ablauf-Recovery, Retry-Budget, Priorität und Idempotency-Keys.
- Jeder Job ist an Nutzer, Mandant, Workspace, registriertes Repository, Pipeline-Payload, unveränderliches Worker-Image und Ressourcenwerte gebunden.
- SHA-256-Integritätsschutz umfasst die vollständige Identitäts- und Repository-Bindung sowie den kanonischen Pipeline-Payload.
- Workspacebezogene Admission-Richtlinien für Einreicherrollen, Queue-/Parallelitätsgrenzen, Nutzer-Stundenlimit, CPU, RAM, PIDs, Laufzeit, Retry-Anzahl und exakte Image-Digests.
- Nachträgliche Policy-Verschärfungen werden vor dem Lease erneut durchgesetzt.
- Owner/Admin-geschützte Repository-Registrierung mit Operator-Allowlist für Repository-, Worktree- und State-Basisverzeichnisse.
- Web- und Runner-Trennung: Webeinreichungen benötigen keine Repository-Mounts; der Queue-Worker benötigt keine Auth-Datenbank.
- Ablehnung von Inline-Secrets in Queue-Command-Umgebungen.
- Authentifizierte Development-API zum Auflisten registrierter Repositories, Einreichen/Auflisten von Jobs und Abbrechen wartender Jobs.
- Member sehen standardmäßig nur eigene Jobs; Einreichung ist standardmäßig auf Owner/Admin begrenzt.
- Neue Queue-CLI einschließlich dauerhaftem `run-worker`-Pollingmodus.
- Queue-Schema-Version 2 mit Migration für rollenbasierte Einreicherfreigaben.
- Self-Test um echten Queue-/Admission-/Lease-Durchlauf erweitert.
- Leere JavaScript-Fehlerbehandlungen in System- und Verlaufsansicht durch sichtbare Fehlerzustände ersetzt.

## 1.2.0-worker-isolation

### Isolierte Ausführung

- Gehärteter Docker/OCI-Executor für Implementierung, Worker-Tests, Review, Security, QA und Post-Merge-Tests.
- Netzwerk standardmäßig und mechanisch auf `none`; read-only Root-Dateisystem, Capability-Drop, `no-new-privileges`, private IPC und begrenztes `/tmp` und separates ausführbares `/build`-tmpfs.
- CPU-, RAM-/Swap-, PID- und Dateideskriptor-Limits pro Ausführung.
- Worktree read-only; nur erlaubte Implementierungspfade werden als separate Schreib-Mounts freigegeben. Vorhandene exakt freigegebene Dateien werden nicht auf ihr Elternverzeichnis erweitert.
- `.git` ist im Container verdeckt. Host-Umgebungsvariablen werden nicht vererbt.
- Image-Digest-Pflicht und nicht-root UID:GID.
- Kein Host-Downgrade zwischen Container-Gates und Integrationstests. Prozessausgaben sind größenbegrenzt; Timeout-Abbrüche entfernen den benannten Container zwangsweise.
- Fail-Closed-Schalter `TANKAI_REQUIRE_WORKER_ISOLATION`.
- Persistenzschema 4 für Backend- und Isolationsmetadaten.
- Separates `Dockerfile.worker`; kein Runtime-Socket im öffentlichen Webcontainer.

### Kontrollierte Parallelisierung

- Neuer `WorkerPoolRunner` für zwei bis zwölf bereits genehmigte Programmier-Pipelines mit begrenzter Parallelität.
- Vorabprüfung auf aktive exklusive Task-Zuweisung und überschneidungsfreie Schreibbereiche.
- Eigener Git-Worktree pro Worker; repositoryweite Sperre für Worktree-/Branch-Metadaten.
- MAIN-Integration bleibt exklusiv und seriell.
- Fehler werden pro Agent erfasst; erfolgreiche Worker-Branches bleiben erhalten.
- Neuer CLI-Befehl `run-pool` mit strikt validiertem `WorkerPoolJob`.

### Tests

- Neue Tests für Docker-Argumente, Mount-Scope-Minimierung, exakte Datei-Mounts, Umgebungsisolation, Root-Blockade, begrenzte Ausgaben, Timeout-Bereinigung, vollständige Gate-Abdeckung, Downgrade-Schutz und reale Parallelität zweier konfliktfreier Worker.

## 1.1.0-online

### Authentifizierung und Sitzungen

- Persistenter SQLite-Auth-Store für Benutzer, Mandanten, Workspaces, Memberships, Sessions und Audit-Ereignisse.
- Scrypt-Passwort-Hashes mit individueller Zufallssalt und Mindestlänge von zwölf Zeichen.
- Opaque Session-Tokens, serverseitiger Widerruf, Ablaufzeiten und vollständiger Widerruf nach Passwortänderung.
- HttpOnly-/SameSite-Session-Cookie; Secure-Flag standardmäßig bei nicht-lokalem Betrieb.
- CSRF-Pflicht für Run, Logout, Workspace-Erstellung und Workspace-Wechsel.
- Prozesslokale Begrenzung fehlgeschlagener Login-Versuche.
- Öffentliche Registrierung standardmäßig deaktiviert.

### Mandanten- und Workspace-Trennung

- Serverseitig geprüfte Workspace-Mitgliedschaften mit Owner-, Admin- und Member-Rollen.
- Aktiver Workspace wird in der authentifizierten Session geführt und bei jeder Auflösung gegen die Membership geprüft.
- Separate Laufzeit, Locks, Short-Term-Memory, LTM, Vector-Datei, Cold-Storage und Historie pro Workspace.
- Validierte UUID-basierte Speicherpfade unter einer festen Datenwurzel.
- In-Memory-Runtime-Cache besitzt keine Autorität über Nutzer- oder Workspace-Zugriffe.

### Web und Betrieb

- Neue Endpunkte für Login, Logout, Session-Status, Workspaces und Workspace-Auswahl.
- Browseroberfläche mit Login und Workspace-Auswahl; dynamische Inhalte werden weiterhin nur über sichere DOM-Methoden gesetzt.
- Nicht authentifizierter Health-Check gibt keine Provider- oder Workspace-Details aus.
- Auth-freier Modus wird außerhalb von Loopback blockiert.
- Docker-Härtung: read-only Root-Dateisystem, alle Capabilities entfernt, no-new-privileges und PID-Limit.
- Neue administrative `auth_cli` zur initialen Benutzeranlage und Passwortänderung.

### Tests

- Regressionen für Passwortprüfung, Session-Widerruf, CSRF, Login, Logout, Registrierungssperre, Workspace-Wechsel und mandantengetrennte Historien/LTM-Pfade.

## 1.0.0-integration

### Reale Git-Integration

- Neuer `WorkerIntegrationRunner` für freigegebene Worker-Runs.
- PID-gebundenes Repository-Lock verhindert parallele MAIN-Mutationen.
- Strikte Konsistenzprüfung zwischen Hauptbranch, Repository-HEAD und `ProjectState.current_commit`.
- Reale `git rebase --onto`-Ausführung auf den aktuellen stabilen Commit; Konflikte werden abgebrochen und blockiert.
- Mechanische Prüfung, dass der Rebase den freigegebenen Datei-Scope nicht erweitert.
- Integration ausschließlich über `git merge --ff-only`.
- Integrationsjobs müssen alle in der Task-Spezifikation festgelegten Pflichtprüfungen wiederholen.
- Automatisches `git diff --check` des integrierten Commit-Bereichs vor den projektspezifischen Gesamttests.
- Verpflichtende Post-Merge-Gesamttests im tatsächlichen Haupt-Repository.
- Rollback auf den vorherigen stabilen Commit bei Test-, Integritäts- oder State-Fehlern.
- Atomisches Crash-Journal zur Wiederherstellung eines Merges vor dem persistenten State-Commit.
- Journal-Recovery validiert Worktree-Pfad, Branch und Basis-Commit, bevor ein unterbrochener Rebase zurückgesetzt wird.
- Erfolgreiche Integration aktualisiert Worker-Run, Task, Agent, Audit-Log, Sperren und `CURRENT_STABLE_COMMIT` in einer State-Transaktion.
- Erfolgreich integrierte Worktrees und Branches können kontrolliert bereinigt werden.
- Neuer CLI-Unterbefehl `integrate` und neues Modell `IntegrationJob`.

### Persistenz

- `ProjectState` Schema-Version 3.
- Worker-Runs speichern Rebase-Ursprung, Rebase-Commit, Integrationstests und Integrations-Commit.
- Kompatible Migration von Schema-Version 1 und 2.

### Tests

- Regressionen für Fast-Forward-Integration, Rebase auf fortgeschrittenes MAIN, Rebase-Konflikt, Post-Merge-Rollback, unsauberes MAIN, Crash-Recovery und CLI-Integration.
- Der integrierte Self-Test führt jetzt zusätzlich einen realen Merge mit Post-Merge-Gate aus.

## 0.9.0-worker-runner

### Reale Worker-Ausführung

- `WorkerPipelineRunner` führt genehmigte `WorkerPipelineJob`-Aufträge in echten Git-Worktrees aus.
- Befehle werden ausschließlich als explizite `argv`-Arrays ohne Shell und mit Zeitlimit gestartet.
- Implementierungs-, Test-, Review-, QA- und Security-Ausführungen werden als strukturierte `TestExecution`-Datensätze gespeichert.
- Persistente Worker-Run-Phasen und Statusmeldungen mit Run-ID, Branch, Workspace, Basis-Commit, Änderungsdateien und Implementierungs-Commit.
- Implementierungsbefehle dürfen `HEAD` nicht selbst verändern; versteckte Eigen-Commits werden blockiert.
- Der vollständige Commit-Diff wird nach dem Commit erneut gegen erlaubte und gesperrte Pfade geprüft.
- Test- und Gate-Befehle dürfen keine versionierten Dateien oder den geprüften Commit verändern.
- Unversionierte Prüfarbeitsdateien werden nur im isolierten Worktree entfernt.
- Getrennte Agentenidentitäten für Implementierung, Review und QA sind mechanisch vorgeschrieben; Security erhält bei Pflichtprüfung eine vierte Identität.
- CLI-Unterbefehl `run-pipeline` liest einen strikt validierten JSON-Job und gibt den vollständigen strukturierten Run zurück.

### Persistenz und Migration

- `ProjectState` Schema-Version 2 ergänzt `worker_runs`.
- Bestehende Schema-Version-1-Zustände werden beim Laden kompatibel migriert.
- Tasks speichern `worker_run_id` und `implementation_commit`.

### Tests

- Erfolgreicher kompletter Worker-/Review-/QA-/Security-Zyklus in echten temporären Git-Repositories.
- Regressionen für fremde Pfade, versteckte Worker-Commits, verändernde Review-Gates und abgelehnte Reviews.
- Integrierter Self-Test führt einen realen Worker-Run mit Git-Commit, Review und QA aus.

### Weiter offen

- autonome LLM-Code-Worker, die genehmigte Aufgaben selbst in konkrete Codeänderungen übersetzen,
- tatsächliche Merge-Ausführung und Konfliktauflösung in der Merge-Warteschlange,
- transaktionale Kopplung von Git-Ref und `ProjectState`,
- Benutzerkonten und Mandantentrennung der Webplattform.

## 0.8.0-orchestrator

### Kontrollierte Agenten-Replikation

- Persistenter `ProjectState` als gemeinsame Quelle der Wahrheit.
- Atomare JSON-Speicherung mit Lock-File und revisionsbasiertem Schutz gegen veraltete Writer.
- Rollenmodell für Architektur, Backend, Frontend, Datenbank, Security, QA, Debug, DevOps, Review und Dokumentation.
- Task-Spezifikationen mit Abhängigkeiten, Pfadbereichen, Abnahmekriterien, Pflichtprüfungen und Security-Anforderung.
- Kontrollierte `SpawnRequest`-Freigabe mit gleicher Rolle, aktuellem Basis-Commit, unabhängiger Teilaufgabe und festen Ressourcenlimits.
- Datei-Sperren und konservative Kollisionsprüfung für Git-Pfade und Globs.
- Task-Graph mit Prüfung unbekannter Abhängigkeiten und Zyklen.
- Merge-Gate aus Agententests, unabhängigem Review, QA, optionalem Security-Review und Rebase-Pflicht.
- Nacharbeits- und Abbruchpfad für blockierte Tasks.
- Fortlaufendes Audit-Protokoll für Zustandsänderungen.

### Git-Arbeitsbereiche

- Eigener Git-Branch und Git-Worktree pro Entwicklungsagent.
- Mechanische Prüfung aller Änderungen und unversionierten Dateien gegen `allowed_paths` und `denied_paths`.
- Testausführung ohne Shell mit Zeitlimit und strukturiertem `TestExecution`-Ergebnis.
- Commit-Erstellung erst nach erfolgreicher Pfadprüfung.

### Tests

- Neue Regressionstests für Spawn-Grenzen, Pfadkollisionen, stale commits, Task-Graph, Pflichtprüfungen, unabhängigen Review, QA, Security, Rebase, Nacharbeit, Abbruch, atomare Revisionen und echte Git-Worktrees.
- Integrierter Self-Test prüft kontrollierte Replikation und Datei-Sperren.

### Weiter offen

- autonomer Worker-Runner für LLM- und Code-Agenten,
- tatsächliche Merge-Ausführung und Konfliktauflösung in der Merge-Warteschlange,
- kryptografisch manipulationsgeschütztes externes Audit,
- Benutzerkonten und Mandantentrennung der Webplattform.

## 0.7.1-integrity

### Fail-closed Verifikation

- Ein vom Critic abgelehnter Plan kann nicht mehr durch einen positiven finalen Critic freigegeben werden.
- Nicht bestandene Specialist-Schritte werden nicht mehr als verwertbare Synthese-Eingaben behandelt.
- Ein deterministisches Execution-Gate blockiert Live-Status `completed`, solange Plan oder Schritte ungeklärt sind.
- Parallele Specialists verwenden jetzt dieselben Retry-Grenzen und dasselbe verbindliche Critic-Feedback wie die sequenzielle Ausführung.
- Unerwartete Future-Fehler werden in parallelen Runs als fehlgeschlagene Receipts gekapselt.
- `RunResult`, JSONL-Historie, CLI-JSON und Web-API enthalten jetzt `verification_passed`, `release_ready`, `plan_gate_passed` und `failed_step_ids`.
- CLI respektiert die Umgebungsvariablen `TANKAI_REQUIRE_INDEPENDENT_CRITIC`, `TANKAI_REQUIRE_RESEARCH_EVIDENCE` und `TANKAI_STRICT_WEB_RESEARCH`.
- `pytest.ini` stellt reproduzierbare lokale Importe auch bei direktem `pytest`-Aufruf sicher.
- Nicht vertrauenswürdige Such- und Seitentexte werden vor der Prompt-Einbettung strukturell neutralisiert; sie können keine Source-Blöcke schließen oder gültige Quellen-Tokens vortäuschen.
- URLs mit Kontroll- oder Leerzeichen werden vor der Auflösung verworfen.
- Redirects der Search-API werden abgewiesen, damit API-Keys nicht an umgeleitete Hosts gelangen.

## 0.7.0-research

### Implementiert

- Echte Websuche über explizit konfigurierte Brave- oder Tavily-Backends.
- Kontrollierter Research-Toolpfad vor dem Modellaufruf; kein freier Web-Toolaufruf aus generiertem Modelltext.
- Sicherer Seitenabruf für HTML, Text, XHTML und JSON mit Zeit-, Größen- und Content-Type-Grenzen.
- SSRF-Schutz für Schema, Credentials, Ports, Loopback, private, Link-Local-, Multicast-, reservierte und nicht spezifizierte IP-Adressen; Redirect-Ziele werden erneut geprüft.
- IDNA-Normalisierung internationaler Hostnamen.
- Research-Cache zur Vermeidung identischer, mehrfacher Search-API-Aufrufe innerhalb eines Prozesses.
- Deterministische Quellen-IDs im Format `[SRC-XXXXXXXX]` aus normalisierten URLs.
- Quellen-Provenance in Specialist-Receipts, RunResult, Web-API, CLI-JSON und JSONL-Run-Historie.
- Automatisch rekonstruierter Quellenkatalog aus Receipts.
- Mechanische Quellenprüfung für Research-Schritte und finale Synthese; fehlende oder unbekannte IDs blockieren den Live-Status.
- Originalziel, Definition of Done und Constraints werden an Specialists übergeben.
- Separat konfigurierbarer Critic mit eigenem Provider, Modell, Key und optionalem OpenAI-kompatiblem Endpunkt.
- Erzwingbare Critic-Unabhängigkeit anhand der Provider-/Modell-/Endpunktidentität.
- Ausführungsmodus `mixed`, wenn Hauptmodell oder Critic simuliert ist.
- LLM-Identität wird in jedem Agenten-Receipt protokolliert.
- Web-Health liefert Hauptmodell, Critic, Unabhängigkeit, Suchanbieter und `verification_ready`.
- Prompt-Injection-Hinweise für untrusted Webinhalte und klar abgegrenzte Source-Blöcke.
- Pytest-Regressionssuite für Web-Evidence, Cache, SSRF, Quellen-Gates, Critic-Trennung und Web-API.

### Sicherheitsentscheidungen

- Keine stille Websuche ohne Provider und API-Key.
- Provider-Modellnamen müssen explizit gesetzt werden; keine potenziell veralteten Modell-Defaults.
- Research-Modelle dürfen keine eigenen Quellen-IDs oder URLs erfinden.
- Nur Webports 80 und 443 sind für Zielseiten erlaubt.
- `production_ready` bleibt trotz Live-Verifikation bewusst `false`.

### Weiter offen

- IP-Pinning gegen DNS-Rebinding während der eigentlichen TCP-Verbindung,
- PDF-Extraktion und JavaScript-Rendering,
- atomare Koordination von SQLite und Vector-Datei,
- Benutzerkonten, Mandantentrennung, persistente Quoten und manipulationsgeschütztes Audit,
- reale Provider-/Search-E2E-Tests mit Testkonten und Kostenbudget.

## 0.6.0-security

### Behoben

- SQLite-Migration für ältere `memory_entries`-Tabellen läuft vor dem Anlegen des Retention-Index.
- Bestehende Datensätze erhalten bei der Migration `retention_policy='hot'`.
- Providerfehler führen nicht mehr automatisch zu `MockLLM`.
- LTM-Fehler führen nicht mehr automatisch zu flüchtigem In-Memory-Speicher.
- Mock- und Echo-Runs werden als `simulated` gekennzeichnet.
- Simulierter Critic meldet keine erfolgreiche unabhängige Prüfung.
- Dynamisches `innerHTML` wurde aus der Weboberfläche entfernt.
- CSP, Frame-Schutz, No-Sniff, Referrer-Policy und No-Store ergänzt.
- Interne Exception-Texte werden nicht mehr an API-Clients ausgegeben.
- Vector-Store lädt keine Pickle-Objekte mehr.
- Cold-Storage verändert den aktiven Datensatz erst nach erfolgreichem Archivschreiben.
- Critic-Feedback wird bei Plan-Reparaturen und Specialist-Retries in den neuen Prompt übernommen.
- Specialist- und Critic-Fehler werden als fehlgeschlagene Receipts gekapselt.
- `definition_of_done` und `execution_mode` werden im Run-Ergebnis geführt.
- Paket- und Webversion sind vereinheitlicht.
- Vorhandene Mock-Historie und Mock-Memory wurden als `legacy_unverified` bzw. `unknown` quarantänisiert.
- Unverifiziertes Short-Term-Memory wird standardmäßig nicht in neue Runs geladen.

### Geändert

- Persistentes LTM ist der Standard der Webanwendung.
- Öffentlicher Bind ohne Authentifizierung ist standardmäßig blockiert.
- Docker veröffentlicht den Dienst standardmäßig nur auf Loopback.
- Self-Test enthält Regressionstests für Migration, Mock-Sicherheit, Vector-Format und Web-Rendering.

### Weiter offen

- echte Browser-/Suchwerkzeuge,
- unabhängiger Critic-Provider,
- atomare Koordination von SQLite und Vector-Datei,
- Benutzerkonten, Quoten und produktionsreife Observability.
