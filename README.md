# TankAI — Web Intelligence OS

**Version 1.10.0-module-ownership**

**Releasevertrag: `TankAI-Core-1.10.0-module-ownership` · ProjectState Schema 6**

TankAI ist ein ausführbarer Python-Multi-Agenten-Kern mit Planner, Specialists, getrennt konfigurierbarem Critic, Synthesizer, Receipts, Webrecherche und langfristigem Speicher. Es ist **kein eigenes trainiertes Basismodell**.

## Entwicklungsstand

| Bereich | Stand |
|---|---|
| PLAN → ROUTE → VERIFY → LEARN | Implementiert |
| Planner-Reparatur nach Critic-Feedback | Implementiert |
| Specialist-Retry mit Critic-Feedback | Implementiert |
| Episodic / Semantic / Procedural Memory | Implementiert |
| SQLite-Migrationen | Versioniert und idempotent |
| Vector-Persistenz | Ohne Pickle |
| OpenAI / Anthropic | Adapter implementiert |
| Getrennter Critic | Separater Provider/Modell konfigurierbar und erzwingbar |
| Websuche | Brave Search API oder Tavily Search API |
| Seitenabruf | HTML/Text/JSON mit Größenlimit und SSRF-Schutz |
| Quellenbelege | Stabile `[SRC-XXXXXXXX]`-IDs, Receipt-Provenance und Quellenkatalog |
| Deterministische Quellenprüfung | Fehlende oder erfundene Quellen-IDs blockieren Live-Freigabe |
| Development Orchestrator | TECH-AI-V2-Rollenkatalog, persistente Agentenverträge, Entwicklungszyklen, Spawn-Limits und Datei-Sperren |
| Capability-/Modulregister | Persistente Capability-Verträge, Modulzuordnung, Abhängigkeiten, Ownership und Abnahmetests in ProjectState Schema 6 |
| Git-Isolation | Eigene Branches und Worktrees pro Entwicklungsagent |
| Worker-Runner | Explizite argv-Befehle; optional in gehärteten OCI/Docker-Containern |
| Paralleler Worker-Pool | Bis zu zwölf vorab genehmigte, konfliktfreie Programmier-Agenten mit getrennten Worktrees |
| Persistente Development-Queue | SQLite-Queue mit atomaren Leases, Heartbeats, Retry-Budget und Idempotenz |
| Externes Lease-Fencing | Separate monotone Fence-Datenbank pro Repository; veraltete Worker verlieren Schreib- und Commit-Freigabe |
| Aktive Lease-Abbruchkontrolle | Laufende Host- und Container-Kommandos werden bei Heartbeat-/Fence-Verlust aktiv beendet |
| Worktree-Reaper | Operatorgesteuerte Bereinigung sauberer verwaister Worktrees; schmutzige Worktrees werden quarantänisiert, Branches bleiben erhalten |
| Container-Reaper | Labelgebundene Erkennung und kontrollierte Entfernung stale Worker-Container anhand von Mandant, Workspace, Repository, Job und Fence-Epoche |
| Release-Backup | Deterministische, secret-geprüfte ZIP-Snapshots mit internem Manifest, Metadaten und externer SHA-256-Prüfung |
| Publikationsledger | Hashverkettete Drive-Artefakt- und GitHub-Commit-Receipts mit lokaler Integritätsprüfung |
| CI-Vertrag / belegte Baseline | Python-Compile, 196 Pytests, 24 Self-Tests, Workflow-Policy, Wrangler-Typen/Typecheck/Dry-Run, Worker-Artefakt und Produktions-Container-Build; der aktuelle lokale Nachweis steht im `TEST_REPORT.md` |
| Produktionsdeploy | Separater manueller Workflow auf `main`; exakte `DEPLOY`-Bestätigung, Bindung an das GitHub-Environment `production` und serielle Concurrency erforderlich; externe Environment-Schutzregeln vor Deploy verifizieren |
| Rootless-Runtime-Gate | Docker-/Podman-Sicherheitsprofil wird für Online-Queue-Worker mechanisch auf Linux + rootless geprüft |
| Admission-Control | Bindung an Nutzer, Mandant, Workspace, registriertes Repository, Rollen, Image-Digest und Ressourcenbudget |
| Development-API | Authentifiziertes Einreichen, Auflisten und Abbrechen noch nicht geleaster Jobs |
| External Agent Gateway v1 | Zeitlich begrenzte, widerrufbare Maschinen-Tokens mit Workspace-, Scope-, Repository- und Job-Isolation |
| Merge-Gates | Unabhängiger Review, QA, optional Security und Rebase-Pflicht |
| Git-Integration | Exklusiver Rebase, Fast-Forward-Merge, Post-Merge-Tests, Rollback und Crash-Journal |
| Web-UI | CSP, Security-Header, sichere DOM-Erzeugung |
| Benutzerkonten | Scrypt-Passwörter, widerrufbare HttpOnly-Sessions, CSRF-Schutz |
| Mandantentrennung | Verifizierte Workspace-Mitgliedschaft und getrennte Persistenzpfade |
| Rollen | Owner, Admin, Member; serverseitige Prüfung |
| Produktionsreife | Kontrollierter Single-Host-Betrieb möglich; reale Runtime-E2E-Prüfung, Multi-Host-Koordination, Image-Signaturprüfung, Credential-Broker und Monitoring fehlen |

## Was 1.10.0 zusätzlich umsetzt

- `ProjectState` Schema 6 ergänzt ein persistentes Capability-Register mit stabiler Capability-ID, Modul-ID, Owner, Status, Source-Referenz, Abhängigkeiten, Schnittstellenvertrag und Abnahmetests.
- Tasks können über `capability_id` und die expliziten Aktionen `CREATE`, `EXTEND`, `FIX`, `TEST`, `REVIEW` oder `INTEGRATE` an genau eine Capability gebunden werden.
- `CREATE` ist nur für noch nicht begonnene Capabilities zulässig; andere Aktionen können keine nicht begonnene Capability implizit erzeugen.
- Gleichzeitig aktive konkurrierende Tasks für dieselbe Capability werden blockiert, und Capability-Abhängigkeiten müssen vor der Registrierung bereits bekannt sein.
- Bestehende Schema-5-Zustände werden ohne Verlust ihrer bisherigen Tasks und Agenten auf Schema 6 migriert; das Capability-Register und die Task-Bindungen werden dabei explizit ergänzt.
- Der Cloudflare-Build ist in den bestehenden CI-Pfad integriert: Bindungstypen werden aus `wrangler.jsonc` erzeugt, TypeScript wird strikt geprüft, der Worker wird nur als Dry-Run gebaut und das Produktions-Containerimage wird gebaut.
- Der Produktionsdeploy ist vom Merge getrennt und nur manuell auf `main` mit exakter Bestätigung, Bindung an das GitHub-Environment `production` und serieller Produktions-Concurrency möglich; dessen externe Schutzregeln sind vor einem Deploy live zu verifizieren.
- Externe GitHub Actions sind auf vollständige Commit-SHAs und Wrangler ist für den Produktionsworkflow auf `4.124.0` festgesetzt.

## Was 1.9.0 zusätzlich umsetzt

- Vollständiger TECH-AI-V2-Rollenkatalog von Core-Leitung, Architektur, Backend, Frontend, Mobile, Datenbank, KI, Security, QA, DevOps, Observability, Release bis C++/JUCE/Audio.
- Persistierte Governance-Richtlinie mit den Standardgrenzen `40` aktive Agenten, `80` Agenten pro Entwicklungszyklus, Klontiefe `5`, maximal `3` Folge-Agenten und höchstens `4` Agenten pro Modul.
- `MAX_AGENTS_PER_FILE=1` bleibt mechanisch über überlappungsfreie Datei- und Pfad-Sperren erzwungen.
- Jeder Agent erhält einen versionierten Arbeitsvertrag mit Zyklus-ID, Aufgabe, Basis-Commit, Rolle, Schreibbereich, Abnahmekriterien, Pflicht-Tests, Priorität und Deadlock-Regeln.
- Vollständiger Lebenszyklus einschließlich `created`, `initializing`, `ready`, `active`, `blocked`, `waiting_for_review`, `failed`, `completed`, `merged`, `rejected` und `terminated`.
- Spezialisierte Quality-, Security- und Architekturrollen können die jeweils passenden Review-Gates übernehmen; Selbstfreigaben bleiben gesperrt.
- Neue persistente Entwicklungszyklen mit hartem Gesamtlimit und explizitem `begin-cycle`-Übergang. Ein neuer Zyklus ist blockiert, solange nicht-terminale Agenten existieren.
- Zustandsmigration von Schema 1–4 auf Schema 5 ergänzt Governance, Zyklusdaten und vollständige Agentenverträge.

### Governance-Status und neuer Zyklus

```bash
python -m tankai.dev_orchestrator.cli   --state .tankai/project-state.json   status

python -m tankai.dev_orchestrator.cli   --state .tankai/project-state.json   begin-cycle   --reason "Vorheriger Entwicklungszyklus vollständig abgeschlossen"
```

## Was 1.8.0 zusätzlich umsetzt

- Hashverkettetes Publikationsledger für externe Release-Sicherungen.
- Google-Drive-Receipts werden an Zielordner, Datei-ID, HTTPS-URL, Dateigröße und Remote-Digest gebunden.
- GitHub-Receipts werden an Repository, Branch und exakten 40-stelligen Commit gebunden.
- Jeder Receipt-Eintrag besitzt Sequenznummer, vorherigen Hash und eigenen SHA-256-Ereignishash.
- Lokale Artefakte werden beim Statusabruf erneut auf Größe und SHA-256 geprüft.
- Remote-Digests werden gegen die lokalen Bytes gegengeprüft; doppelte oder fremde Receipts werden blockiert.
- Der Status unterscheidet `valid` von `complete`: Ein korrektes, aber noch nicht vollständig gespiegeltes Release bleibt sichtbar unvollständig.
- Neue CLI `python -m tankai.dev_orchestrator.publication_cli`.
- `docs/RELEASE_PUBLICATION.md` dokumentiert Planung, Connector-Receipts, Prüfung und Sicherheitsgrenzen.

### Publikationsledger erstellen und prüfen

```bash
python -m tankai.dev_orchestrator.publication_cli plan \
  --release-directory ../tankai-release \
  --ledger ../tankai-release/publication-ledger.json \
  --version 1.8.0-publication-ledger \
  --commit 0123456789abcdef0123456789abcdef01234567 \
  --branch main \
  --drive-target drive-main=GOOGLE_DRIVE_FOLDER_ID \
  --github-target github-main=OWNER/REPOSITORY

python -m tankai.dev_orchestrator.publication_cli status \
  --ledger ../tankai-release/publication-ledger.json \
  --release-directory ../tankai-release
```

## Was 1.7.0 zusätzlich umsetzt

- Deterministische Quellcode-Backups über `tankai.dev_orchestrator.release_cli`.
- Jedes ZIP enthält ein internes SHA-256-Manifest und maschinenlesbare Backup-Metadaten.
- Externe `.backup.json`, `.manifest.sha256` und `.SHA256SUMS`-Dateien ermöglichen unabhängige Prüfung nach GitHub-/Drive-Upload.
- Secret-Scan blockiert typische API-Schlüssel, GitHub-Tokens, private Schlüssel und versehentlich eingecheckte Client-Secrets.
- `.env`, SQLite-/Vector-/Run-Dateien, Git-Metadaten, Laufzeitdaten und Caches werden fail-closed ausgeschlossen.
- Symlinks, Spezialdateien, Pfad-Traversal, doppelte ZIP-Pfade und manipulierte Dateiinhalte werden abgewiesen.
- GitHub-Actions-Workflows führen Compile, Tests und Self-Test aus und können geprüfte Release-Artefakte erzeugen.
- `docs/BACKUP_AND_RECOVERY.md` dokumentiert Erstellung, Prüfung, GitHub-Sicherung und Drive-Sicherung.

### Backup erstellen und prüfen

```bash
python -m tankai.dev_orchestrator.release_cli build \
  --project-root . \
  --output-directory ../tankai-release \
  --version 1.7.0-release-backup

python -m tankai.dev_orchestrator.release_cli verify \
  --archive ../tankai-release/tankai-core-1.7.0-release-backup.zip \
  --checksums ../tankai-release/tankai-core-1.7.0-release-backup.SHA256SUMS
```

## Was 1.6.0 zusätzlich umsetzt

- Jeder Queue-Worker-Container erhält unveränderliche TankAI-Labels für `tenant_id`, `workspace_id`, `repository_id`, `job_id`, `fence_epoch`, `worker_id`, `run_id` und Phase.
- `DockerCommandExecutor.list_managed_containers()` liest ausschließlich Container mit `tankai.managed=true` und passender Repository-ID und validiert die Inspect-Daten.
- Der neue Operator-Befehl `reap-containers` schützt Container eines aktiven Queue-Leases oder des aktuellen externen Fence.
- Container abgeschlossener Jobs können nach dem Mindestalter entfernt werden. Unbekannte Jobs und stale nicht-terminale Jobs verlangen eine exakte Bestätigung aus Job-ID und Fence-Epoche.
- Mandanten-, Workspace- oder Repository-fremde Labels werden nicht bereinigt. Der Standard bleibt Dry-Run; eine Entfernung erfordert `--apply`.
- Dieselben Identitätslabels können auch für Post-Merge-Integrationstests an den Container-Executor übergeben werden.
- Metadaten dürfen die reservierten Labels `managed`, `run_id` oder `phase` nicht überschreiben.

### Container-Reaper-Sicherheitsgrenzen

Der Reaper arbeitet ausschließlich über vom Runtime-Daemon gelieferte Container-IDs und ruft `rm -f` ohne Shell auf. Ein unbekannter Container wird nie allein aufgrund seines Namens gelöscht. Ein realer Rootless-Docker-/Podman-End-to-End-Test war in der Build-Umgebung weiterhin nicht möglich, weil keine Runtime installiert ist.

## Was 1.5.0 zusätzlich umsetzt

- Laufende Worker-Kommandos prüfen den Lease-/Fence-Guard nicht mehr nur vor und nach einer Phase, sondern fortlaufend während der Prozessausführung.
- Bei Heartbeat-, Lease- oder Fence-Verlust wird die gesamte lokale Prozessgruppe zunächst beendet und bei Bedarf hart abgebrochen. Der ursprüngliche Lease-Fehler wird anschließend unverändert weitergereicht.
- Bei OCI/Docker-/Podman-Ausführung wird zusätzlich der eindeutig benannte Container mit `rm -f` entfernt. Das verhindert, dass ein bereits vom Daemon gestarteter Container nach dem Tod des lokalen Runtime-Clients weiterläuft.
- Dasselbe Abbruchverhalten gilt für Host-Kommandos in kontrollierten lokalen Entwicklungs- und Testläufen.
- Die Queue-Heartbeat-Schleife setzt ein explizites Abbruchsignal. Der produktive `WorkerPipelineRunner` pollt dieses Signal während jedes laufenden Kommandos.
- `GitWorkspaceManager.reap_managed_worktrees()` bereinigt ausschließlich saubere, nicht geschützte direkte Worktrees unter dem konfigurierten Workspace-Root. Nur Branches unter `tankai/` werden berücksichtigt.
- Schmutzige Worktrees werden nicht gelöscht, sondern mit Grund als `quarantined` gemeldet. Gelöschte Worktrees behalten ihren Git-Branch, damit geprüfte oder uncommittete Historie nicht pauschal vernichtet wird.
- Bereits erhaltene Agent-Branches können bei einer späteren Nacharbeit wieder als Worktree eingehängt werden, sofern sie weiterhin auf dem bestätigten Basis-Commit beruhen.
- Neuer Operator-Befehl `reap-worktrees`: standardmäßig Dry-Run, nur für Owner/Admins, blockiert bei aktivem Queue-Job oder Repository-Fence. Ein noch nicht terminaler stale Run kann ausschließlich über eine exakt bestätigte Run-ID freigegeben werden.

### Reaper-Sicherheitsgrenzen

Der Reaper ist kein pauschales `rm -rf`. Er entfernt keine Symlink-Workspaces, keine fremden Branches, keine durch ProjectState geschützten Worktrees und keine Worktrees mit lokalen Änderungen. Die Queue und der externe Fence müssen vor einer echten Bereinigung in einem inaktiven Zustand sein.

## Was 1.4.0 zusätzlich umsetzt

- Jeder Queue-Lease erhält zusätzlich eine monotone externe Fence-Epoche in einer **separaten SQLite-Datenbank**. Queue- und Fence-Datenbank dürfen nicht dieselbe Datei sein.
- Der Fence ist pro registriertem Repository exklusiv. Eine neuere Epoche macht ältere Worker-Tokens mechanisch ungültig, auch wenn ein Worker noch eine alte Queue-Kopie oder einen wiederhergestellten Queue-Stand besitzt.
- Start, Heartbeat, Ausführungsphasen, Commit, Review-/QA-/Security-Gates und Abschluss prüfen Queue-Lease und externen Fence fail-closed. Ein verlorener Fence kann keinen erfolgreichen Jobstatus erzeugen.
- Worker-Kommandos werden vor und nach jeder Phase geprüft. Bei Fence-Verlust wird insbesondere der Implementierungs-Commit verhindert; veraltete Runner dürfen ihren Fehlerzustand nicht in den gemeinsamen Orchestrator-State schreiben.
- Ein weiterhin aktiver externer Fence verhindert, dass eine nur lokal abgelaufene Queue-Lease einen zweiten Worker startet. Erst nach Ablauf oder expliziter, exakt bestätigter Operator-Recovery kann eine neue Epoche vergeben werden.
- `fence-status` und `force-expire-fence` ergänzen die Queue-CLI. Die Recovery verlangt Owner/Admin-Rechte, Repository-ID, erwartete Epoche und bestätigte Job-ID.
- Pro Repository ist in der Queue zusätzlich ein partieller Unique-Index für `leased`/`running` aktiv. Queue-Schema 3 migriert ältere Zustände und ergänzt `fence_epoch`.
- Online-Queue-Worker verwenden `DockerCommandExecutor(..., require_rootless=True)`. Docker-/Podman-`info` wird mechanisch auf Linux und expliziten Rootless-Betrieb geprüft.
- Neue Runtime-Diagnose: `python -m tankai.dev_orchestrator.runtime_cli --container-runtime docker`. Rootful-Runtimes werden standardmäßig mit Exitcode 2 abgelehnt.

### Betriebsgrenze des Fencings

Die separate SQLite-Fence-Datenbank schützt einen kontrollierten **Single-Host-Betrieb**. Sie muss auf lokalem, dauerhaftem Speicher liegen und unabhängig von der Queue gesichert werden. Für mehrere Hosts ist ein externer transaktionaler Koordinator mit atomarem Compare-and-Swap erforderlich.

## Was 1.3.0 zusätzlich umsetzt

- `DevelopmentJobQueue` speichert Development-Aufträge persistent in SQLite und bindet jeden Auftrag unveränderlich an `user_id`, `tenant_id`, `workspace_id`, ein operatorseitig registriertes Repository und einen validierten `WorkerPipelineJob`.
- Repository-, Worktree- und Orchestrator-State-Pfade werden ausschließlich durch Owner/Admins registriert und gegen getrennte, vom Betreiber konfigurierte Basisverzeichnisse geprüft. Webrequests können keine Hostpfade übergeben.
- Pro Workspace gilt eine fail-closed Admission-Richtlinie mit erlaubten Einreicherrollen, exakten unveränderlichen Container-Images, maximalem RAM, CPU, PIDs, Laufzeit, Queue-Größe, Parallelität, Retry-Anzahl und stündlichem Nutzerlimit.
- `owner` und `admin` dürfen standardmäßig einreichen. `member` muss ausdrücklich freigeschaltet werden und darf die Queue-Priorität nicht erhöhen.
- Inline-Secrets in Befehlsumgebungen werden abgewiesen. Kurzlebige Zugangsdaten benötigen weiterhin einen separaten Credential-Broker.
- Jeder Payload besitzt eine SHA-256-Prüfsumme über Nutzer-, Mandanten-, Workspace-, Repository- und Pipeline-Bindung. Manipulierte Queue-Einträge werden vor der Ausführung dauerhaft blockiert.
- Geänderte Richtlinien werden unmittelbar vor dem Lease erneut geprüft. Nachträglich gesperrte Images oder reduzierte Ressourcenbudgets verhindern die Ausführung bereits wartender Jobs.
- Worker beziehen Jobs über atomare Lease-Tokens, verlängern diese per Heartbeat und können nach Ablauf nur innerhalb des festgelegten Retry-Budgets erneut eingeplant werden.
- Der Runner benötigt zum Verarbeiten der Queue keinen Zugriff auf die Auth-Datenbank. Authentifizierte Verwaltungs- und Einreichvorgänge bleiben vom Worker-Prozess getrennt.
- Operator-CLI: `python -m tankai.dev_orchestrator.queue_cli` verwaltet Queue-Richtlinien, Repositories, Jobs, Runner sowie den vollständigen Service-Agent-/Token-Lifecycle.
- Neue authentifizierte Webendpunkte: `GET /api/dev/repositories`, `GET /api/dev/jobs`, `POST /api/dev/jobs` und `POST /api/dev/jobs/<uuid>/cancel`.
- Member sehen nur ihre eigenen Development-Jobs; Owner und Admins sehen die Jobs des gesamten Workspaces. Pipeline-Befehle und Hostpfade werden über die Web-API nicht zurückgegeben.
- Der Webdienst benötigt weiterhin weder Repository-Mounts noch Docker-/Podman-Socket. Nur der separate Runner erhält Zugriff auf Queue-Datei, registrierte Repositories, Worktrees, State-Pfade und die rootless Container-Runtime.

### Betriebsgrenze der Queue

Die SQLite-Queue ist für einen kontrollierten **Single-Host-Betrieb auf lokalem Dateisystem** ausgelegt. Sie darf nicht über NFS oder zwischen mehreren Hosts geteilt werden. Für verteilte Runner ist eine transaktionale Serverdatenbank beziehungsweise ein dedizierter Queue-Broker erforderlich.

### External Agent Gateway v1

Owner und Admins können pro Workspace kontrollierte Service-Agenten anlegen und
ihnen zeitlich begrenzte Bearer-Tokens ausstellen. Ein Token enthält eine feste
Repository-Allowlist und die getrennten Scopes `repositories:read`,
`jobs:submit`, `jobs:read` und `jobs:cancel`. Der Roh-Token wird nur beim
Erzeugen ausgegeben und ausschließlich gehasht gespeichert.

Externe KI-Systeme verwenden die versionierten Endpunkte unter `/api/v1/`.
Sie können ihre Fähigkeiten und Repositories abfragen sowie vollständig
validierte `WorkerPipelineJob`-Aufträge einreichen, verfolgen und vor dem Lease
abbrechen. Die bestehende Queue erzwingt weiterhin Rollen, Image-Digests,
Ressourcenbudgets, Idempotenz, Container-Isolation, Leases und Prüf-Gates.
Jeder Service-Agent sieht ausschließlich die von ihm selbst eingereichten Jobs.

Die Verwaltungs- und M2M-Verträge einschließlich Beispielpayload stehen in
[`docs/EXTERNAL_AGENT_API.md`](docs/EXTERNAL_AGENT_API.md). Version 1 ist bewusst
noch kein autonomer Goal-to-Code-Compiler; freie Zieltexte werden nicht heimlich
in ausführbare Shell-Kommandos übersetzt.

Für den Single-Host-Betrieb kann ein Owner/Admin Service-Agenten und Tokens auch
direkt über die lokale Queue-Operator-CLI verwalten. Token-Repository-IDs werden
dabei gegen die aktive Workspace-Registrierung geprüft; Roh-Tokens erscheinen
nur einmal beim Erzeugen.

## Was 1.2.0 zusätzlich umsetzt

- `WorkerPipelineJob` und `IntegrationJob` können eine strikt validierte `WorkerIsolationSpec` tragen.
- Implementierung, Worker-Tests, Review, Security, QA und Post-Merge-Tests laufen dann über einen dedizierten OCI/Docker-Executor.
- Kein Shell-Aufruf und keine Übernahme der Host-Umgebung; nur explizite `argv`- und `env`-Werte werden weitergegeben.
- Netzwerk ist zwingend `none`; das Root-Dateisystem ist read-only; alle Linux-Capabilities werden entfernt und `no-new-privileges` ist aktiv.
- CPU-, RAM-/Swap-, PID-, Dateideskriptor- und `/tmp`-/`/build`-Grenzen sind pro Container verbindlich.
- Der gesamte Git-Worktree wird read-only gemountet. Nur aus `allowed_paths` abgeleitete Bereiche werden für die Implementierungsphase beschreibbar überlagert; vorhandene exakt erlaubte Dateien werden als einzelne Datei-Mounts freigegeben statt ihr Elternverzeichnis zu öffnen.
- Test- und Gate-Container sehen den Workspace ausschließlich read-only. Python-Caches liegen unter `/tmp`; Build- und Testtemporärdateien werden in ein separates, begrenztes `/build`-tmpfs umgeleitet.
- `.git` wird im Container je nach Worktree-Typ durch einen read-only Null- oder tmpfs-Mount verdeckt; Worker erhalten keinen Zugriff auf das Hauptrepository oder dessen Git-Metadaten.
- Container-Images müssen standardmäßig per `name@sha256:<digest>` oder unveränderlicher `sha256:<image-id>` fixiert sein. Mutable Tags sind nur bei expliziter Entwicklungsfreigabe zulässig.
- Ein containergeprüfter Worker-Run darf bei der Integration nicht auf Host-Ausführung zurückgestuft werden; Integrationstests müssen dasselbe fixierte Image verwenden.
- `TANKAI_REQUIRE_WORKER_ISOLATION=1` erzwingt Fail-Closed-Verhalten für den Runner. Prozessausgaben werden auf die letzten 10.000 Byte begrenzt; bei einem Zeitlimit wird der benannte Container zusätzlich zwangsweise entfernt.
- Projektzustände der Schema-Versionen 1–3 werden auf Schema 4 migriert; Ausführungsbackend und Isolationsrichtlinien werden im Audit-Zustand gespeichert.
- `Dockerfile.worker` liefert ein separates Worker-Image. Der öffentliche Webcontainer erhält bewusst keinen Docker-Socket.
- `WorkerPoolRunner` führt zwei bis zwölf bereits genehmigte Programmier-Pipelines begrenzt parallel aus. Vor dem Start werden aktive exklusive Task-Zuweisungen und überschneidungsfreie Schreibbereiche geprüft.
- Jeder Pool-Worker erhält einen eigenen Git-Worktree. Worktree-/Branch-Metadatenänderungen werden repositoryweit gesperrt; MAIN-Integration bleibt weiterhin seriell.
- Der CLI-Befehl `run-pool` verarbeitet einen strikt validierten `WorkerPoolJob`. Fehler eines Workers löschen erfolgreiche Ergebnisse anderer Worker nicht.

### Sicherheitsgrenze

Der Container-Executor ist ausschließlich für einen **dedizierten nicht-root Runner-Prozess** vorgesehen. Der öffentliche Webserver darf weder `/var/run/docker.sock` noch gleichwertige Runtime-Rechte erhalten. Für den öffentlichen Mehrmandantenbetrieb sind zusätzlich ein rootless Runtime-Dienst, Image-Signaturprüfung, ein kurzlebiger Credential-Broker, zentrale Metriken und für Multi-Host-Betrieb eine serverbasierte Queue erforderlich.

## Was 1.1.0 zusätzlich umsetzt

- SQLite-basierte Benutzerkonten mit normalisierten eindeutigen E-Mail-Adressen und `scrypt`-Passwort-Hashes.
- Opaque, widerrufbare Sessions in einer `HttpOnly`-/`SameSite=Strict`-Cookie; bei HTTPS wird `Secure` gesetzt.
- CSRF-Token für alle zustandsändernden authentifizierten API-Aufrufe.
- Anmeldung, Abmeldung, Session-Auflösung und vollständige Session-Invalidierung nach Passwortwechsel.
- Persistente Mandanten, Workspaces und serverseitig geprüfte Memberships mit `owner`, `admin` und `member`.
- Die aktive Workspace-ID stammt aus der autorisierten Session. Vom Client übermittelte Nutzer-IDs werden nicht für Datenzugriffe akzeptiert.
- Jeder Workspace besitzt getrennte `memory.db`, `ltm.db`, `vectors.npz`, `runs.jsonl`, `web_history.jsonl` und `cold/` unter einer validierten Datenwurzel.
- Verlauf, LTM und Short-Term-Memory werden ausschließlich aus dem aktiven autorisierten Workspace geladen.
- Öffentliche Selbstregistrierung ist standardmäßig deaktiviert. Benutzer werden über `python -m tankai.web.auth_cli create-user` angelegt.
- Prozesslokales Login-Rate-Limit ergänzt Reverse-Proxy-Limits.
- Audit-Ereignisse für Login, Workspace-Auswahl, Workspace-Erstellung und Runs werden persistent gespeichert.
- `TANKAI_AUTH_MODE=disabled` ist mechanisch auf Loopback-Binds begrenzt.
- Docker läuft read-only, ohne Linux-Capabilities und mit `no-new-privileges`; nur `/app/data` und `/tmp` sind beschreibbar.

### Ersten Benutzer anlegen

```bash
printf '%s\n' 'EIN-LANGES-SICHERES-PASSWORT' | \
  python -m tankai.web.auth_cli \
    --db .tankai/data/auth.db \
    create-user --email admin@example.com --name Admin \
    --tenant TankAI --workspace Standard --password-stdin
```

Danach `python -m tankai.web.server` starten und über die Weboberfläche anmelden. Für öffentlichen Betrieb muss der Dienst hinter HTTPS laufen und `TANKAI_COOKIE_SECURE=1` gesetzt sein.

### Migrationshinweis

Die alten globalen Dateien wurden unter `legacy-global-state/` archiviert und werden nicht automatisch einem Benutzer zugeordnet. Das verhindert versehentliche Datenübernahme in einen falschen Mandanten. Eine kontrollierte Importfunktion ist noch offen.

## Was 0.7.1 technisch absichert

- Research-Specialists erhalten erstmals das **Originalziel**, nicht nur einen generischen Planschritt.
- Ein Research-Schritt führt die Websuche kontrolliert vor dem LLM-Aufruf aus. Das Modell kann die Websuche nicht über frei erzeugten Tool-Text umleiten.
- Brave und Tavily werden über feste API-Endpunkte angesprochen.
- Search-API-Redirects werden abgewiesen, damit Provider-Credentials nicht an fremde Hosts weitergereicht werden.
- Suchtreffer erhalten deterministische Quellen-IDs aus der URL.
- Zielseiten werden nur über `http`/`https` geladen. Loopback-, private, Link-Local-, Multicast- und reservierte Adressen werden blockiert; Redirects werden erneut validiert.
- Such- und Seiteninhalte sind größenbegrenzt. Binärformate werden nicht als Text verarbeitet.
- Nicht vertrauenswürdige Webtexte werden vor der Prompt-Einbettung neutralisiert; eingebettete `<source>`-Marker und `[SRC-…]`-Tokens können die Quellenstruktur nicht überschreiben.
- Research-Ergebnisse müssen mindestens eine tatsächlich abgerufene Quellen-ID zitieren.
- Die finale Synthese muss gültige Quellen-IDs erhalten. Ein Modell-Critic kann diese mechanische Prüfung nicht überstimmen.
- Der finale Quellenkatalog wird aus den Receipts rekonstruiert, nicht vom Modell erfunden.
- Hauptmodell und Critic können unterschiedliche Provider, Modelle, Keys und OpenAI-kompatible Endpunkte verwenden.
- `TANKAI_REQUIRE_INDEPENDENT_CRITIC=1` verweigert den Start, wenn beide dieselbe Provider-/Modellidentität haben.
- Ist Hauptmodell oder Critic simuliert, lautet der Run-Modus `simulation` oder `mixed`; der Status wird niemals `completed`.
- Ein abgelehnter Plan oder ein nicht bestandener Specialist-Schritt blockiert die Freigabe deterministisch, auch wenn der finale Modell-Critic irrtümlich `passed=true` meldet.
- Fehlgeschlagene Specialist-Ausgaben werden nicht als autoritative Eingabe an den Synthesizer weitergereicht.
- Sequenzielle und parallele Ausführung verwenden dieselben Retry-Grenzen und dasselbe Critic-Feedback.
- Jeder Run weist `verification_passed`, `release_ready`, `plan_gate_passed` und `failed_step_ids` aus.

## Was 0.8.0 zusätzlich umsetzt

- Große Entwicklungsziele können als atomare `TaskSpec`-Objekte mit Abhängigkeiten, Schreibbereichen, Abnahmekriterien und Pflichtprüfungen erfasst werden.
- Der Orchestrator verwaltet den bestätigten Hauptstand, aktive Agenten, Task-Zustände, Datei-Sperren, Reviews, QA, Security-Freigaben und Audit-Ereignisse persistent.
- Ein Agent darf einen gleichartigen Folge-Agenten nur über einen validierten `SpawnRequest` erzeugen.
- Überlappende Schreibbereiche, veraltete Basis-Commits, unbekannte Abhängigkeiten und überschrittene Replikationsgrenzen werden blockiert.
- Jeder Entwicklungsagent kann in einem echten isolierten Git-Worktree mit eigenem Branch arbeiten.
- Vor dem Commit wird mechanisch geprüft, ob alle geänderten Dateien innerhalb der erlaubten Pfade liegen.
- Ein Task wird erst integrationsbereit, wenn Agententests, unabhängiger Review, QA und gegebenenfalls Security bestanden wurden.
- Parallel fertiggestellte Tasks müssen nach einer Änderung von `MAIN` auf den neuen stabilen Commit rebased werden.
- Blockierte Tasks können kontrolliert zur Nacharbeit geöffnet oder unter Freigabe ihrer Sperren abgebrochen werden.

## Was 0.9.0 zusätzlich umsetzt

- Genehmigte `WorkerPipelineJob`-Aufträge werden in echten isolierten Git-Worktrees ausgeführt.
- Jeder Prozessaufruf besteht aus einem expliziten `argv`-Array und läuft ohne Shell mit individuellem Zeitlimit.
- Worker-Phasen, Befehlsresultate, geänderte Dateien, Implementierungs-Commit, Gate-Ergebnisse und Fehler werden persistent im zentralen `ProjectState` gespeichert.
- Ein Worker darf `HEAD` während der Implementierung nicht selbst verändern und kann dadurch keine ungeprüften Eigen-Commits einschleusen.
- Der erzeugte Commit wird nochmals vollständig gegen `allowed_paths` und `denied_paths` geprüft.
- Review-, QA- und Security-Befehle dürfen weder den Commit noch versionierte Dateien verändern; unversionierte Prüfarbeitsdateien werden im isolierten Worktree entfernt.
- Ein Run wird nur `ready_to_integrate`, wenn getrennte Agentenidentitäten für Implementierung, Review und QA sowie bei Bedarf Security alle Gates bestanden haben.
- Schema-Version 1 des Orchestrator-Zustands wird beim Laden auf Schema-Version 2 ergänzt.
- Die CLI kann einen vollständigen Pipeline-Job aus einer validierten JSON-Datei ausführen.

Der Runner führt reale, vorher genehmigte Entwicklungs- und Prüfbefehle aus. Eine autonome LLM-Instanz, die selbst Codeänderungen plant und Befehle erzeugt, ist weiterhin nicht eingebaut. Details: `docs/DEVELOPMENT_ORCHESTRATOR.md`.
Host-Ausführung bleibt nur als kompatibler Entwicklungsmodus erhalten. Für Online-Runner muss `TANKAI_REQUIRE_WORKER_ISOLATION=1` gesetzt und ein dedizierter rootless Container-Runner verwendet werden.

## Was 1.0.0 zusätzlich umsetzt

- Ein freigegebener Worker-Run kann über `IntegrationJob` real nach `MAIN` integriert werden.
- Integrationen werden repositoryweit über ein Git-Lock serialisiert.
- Das Haupt-Repository muss auf dem im `ProjectState` gespeicherten Branch und Commit stehen und sauber sein.
- Veraltete Worker-Branches werden real auf `CURRENT_STABLE_COMMIT` rebased; Konflikte blockieren den Task ohne Änderung von `MAIN`.
- Nach dem Rebase wird der vollständige Commit-Diff erneut gegen den erlaubten Schreibbereich geprüft.
- Der Merge ist ausschließlich als `git merge --ff-only` zulässig.
- Vor der Zustandsfreigabe laufen automatisch `git diff --check` und alle verpflichtenden Task-Tests erneut auf dem gemergten Hauptstand.
- Fehlgeschlagene Post-Merge-Tests setzen `MAIN` auf den vorherigen stabilen Commit zurück.
- Ein atomar geschriebenes Integrationsjournal erkennt und repariert Abstürze zwischen Git-Merge und `ProjectState`-Commit.
- Journalisierte Worktree-Pfade, Branches und Basis-Commits werden vor einer Recovery gegen den verwalteten Workspace-Root validiert.
- Erfolgreiche Integrationen aktualisieren Task, Agent, Worker-Run, Teststatus, Release-Status und `CURRENT_STABLE_COMMIT` in einer Zustands-Transaktion.
- Schema-Version 3 ergänzt Rebase-, Integrations- und Post-Merge-Testdaten.

CLI-Beispiel:

```bash
python -m tankai.dev_orchestrator.cli \
  --state .tankai/project-state.json \
  integrate \
  --repository . \
  --workspace-root ../tankai-worktrees \
  --job integration-job.json
```

## Installation

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Für die Tests zusätzlich:

```bash
pip install -r requirements-dev.txt
```

Provider-Adapter zusätzlich installieren:

```bash
pip install openai anthropic
```

## Minimale Live-Konfiguration

```bash
cp .env.example .env
```

Beispiel mit OpenAI als Hauptmodell, Anthropic als Critic und Brave für Websuche:

```dotenv
TANKAI_LLM=openai
OPENAI_API_KEY=...
OPENAI_MODEL=...

TANKAI_CRITIC_LLM=anthropic
ANTHROPIC_API_KEY=...
TANKAI_CRITIC_MODEL=...
TANKAI_REQUIRE_INDEPENDENT_CRITIC=1

TANKAI_SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=...
TANKAI_STRICT_WEB_RESEARCH=1
TANKAI_REQUIRE_RESEARCH_EVIDENCE=1
```

Alternativ:

```dotenv
TANKAI_SEARCH_PROVIDER=tavily
TAVILY_API_KEY=...
```

Ohne Suchprovider wird keine Pseudo-Websuche erzeugt. Plant der Planner trotzdem einen Research-Schritt, schlägt die deterministische Quellenprüfung fehl.

## Setup prüfen

```bash
python -m tankai --setup
python -m tankai --selftest
python -m pytest -q
```

Der Self-Test und die Pytest-Suite verwenden lokale Fakes. Sie verbrauchen keine Provider- oder Such-API-Aufrufe.

## CLI

```bash
# Konfiguration aus .env
python -m tankai "Prüfe eine aktuelle Behauptung und belege sie."

# Provider direkt wählen
python -m tankai \
  --llm openai \
  --critic-llm anthropic \
  --require-independent-critic \
  --strict-web-research \
  "Vergleiche zwei aktuelle technische Ansätze mit Quellen."

# Reine Orchestrierungs-Simulation
python -m tankai --llm mock "Pipeline testen"
```

Exit-Codes:

| Code | Bedeutung |
|---:|---|
| 0 | Live-Run erfolgreich abgeschlossen |
| 2 | Live-Run fehlgeschlagen |
| 3 | Provider-/Critic-/Web-Konfiguration fehlerhaft |
| 4 | Simulations- oder Mixed-Run beendet |

## Python-Beispiel

```python
from tankai import TankAI, get_llm

main_llm = get_llm("openai", model="MAIN_MODEL")
critic_llm = get_llm("anthropic", model="CRITIC_MODEL")

tank = TankAI(
    llm=main_llm,
    critic_llm=critic_llm,
    require_independent_critic=True,
    require_research_evidence=True,
    strict_web_research=True,
    use_ltm=True,
    parallel=False,
)

result = tank.run(
    goal_description="Aktuelle Aussage prüfen",
    definition_of_done="Belegte Antwort mit nachvollziehbaren Quellen",
)

print(result.status.value)
print(result.execution_mode)
print(result.main_llm_identity)
print(result.critic_llm_identity)
print(result.verification_passed)
print(result.release_ready)
print(result.failed_step_ids)
print(result.source_ids)
print(result.source_urls)
print(result.final_answer)
```

## Web-UI

```bash
python -m tankai --web
```

Standardadresse: `http://127.0.0.1:8765`

`GET /api/health` meldet unter anderem:

- Hauptmodell und Critic-Identität,
- `critic_independent`,
- Suchanbieter,
- `execution_mode`,
- `verification_ready`,
- LTM-Status.

`production_ready` bleibt bewusst `false`.

## Run-Integrität

Die Freigabe ist fail-closed:

- `verification_passed=true` bedeutet, dass Modell-Critic und deterministische Gates bestanden wurden.
- `release_ready=true` wird nur bei einem vollständig verifizierten Live-Run gesetzt.
- `plan_gate_passed=false` blockiert die Freigabe.
- `failed_step_ids` listet ungeklärte oder fehlgeschlagene Plan-Schritte.
- Status `simulated` oder Modus `mixed` ist niemals eine produktive Freigabe.

## Research-Provenance

Jeder Research-Receipt enthält:

```json
{
  "web_research_used": true,
  "source_ids": ["SRC-..."],
  "source_urls": ["https://..."],
  "sources": [
    {
      "source_id": "SRC-...",
      "title": "...",
      "url": "https://..."
    }
  ],
  "research_error": ""
}
```

Die Run-Historie speichert zusätzlich Modellidentitäten, Suchanbieter und Quellenlisten.

## Persistenz

```dotenv
TANKAI_RUN_STORE=tankai_runs.jsonl
TANKAI_LTM_DB=tankai_ltm.db
TANKAI_LTM_VECTORS=tankai_vectors.npz
TANKAI_COLD_DIR=tankai_cold
TANKAI_LTM_MEMORY=0
TANKAI_EMBEDDER=hashing
```

`TANKAI_LTM_MEMORY=1` ist nur für Tests oder bewusst flüchtige Ausführungen vorgesehen.

## Projektstruktur

```text
tankai/
├── agents/
│   ├── planner.py
│   ├── specialist.py
│   ├── critic.py
│   └── synthesizer.py
├── core/
│   ├── llm.py
│   ├── web_research.py
│   ├── long_term_memory.py
│   ├── vector_store.py
│   ├── loop.py
│   ├── tools.py
│   └── models.py
├── dev_orchestrator/
│   ├── models.py
│   ├── state_store.py
│   ├── orchestrator.py
│   ├── git_workspace.py
│   ├── job_queue.py
│   ├── queue_cli.py
│   └── cli.py
├── web/
│   ├── auth.py
│   ├── runtime.py
│   └── server.py
├── cli.py
└── selftest.py
```

## Bekannte Grenzen

- Der Seitenabruf verarbeitet derzeit HTML, Text, XHTML und JSON, aber keine PDFs oder clientseitig gerenderten JavaScript-Seiten.
- DNS-Prüfung reduziert SSRF-Risiken, pinnt die Ziel-IP während der Verbindung aber noch nicht kryptografisch gegen DNS-Rebinding.
- Search-API-Quoten, Kostenbudgets und Provider-Rate-Limits müssen extern überwacht werden.
- Der Critic prüft Inhalte modellbasiert; die Quellen-ID-Prüfung beweist Provenance, nicht automatisch die Wahrheit jeder Aussage.
- Benutzerkonten und Workspace-Trennung sind umgesetzt; allgemeine Provider-Kostenbudgets, Abrechnung, MFA und E-Mail-basierte Kontowiederherstellung fehlen weiterhin.
- Die Development-Queue verwendet SQLite auf lokalem Dateisystem und ist nicht für mehrere Hosts oder NFS ausgelegt.
- Lease-Heartbeats und verlängerte Laufzeitleases reduzieren Doppelstarts. Ein externes Fencing-Token, das einen nach Lease-Verlust weiterlaufenden Git-Prozess auf Kernel- oder Repository-Ebene sicher stoppt, fehlt noch. Der Queue-Runner muss deshalb exklusiv pro Repository betrieben und bei längerem Queue-Datenbankausfall angehalten werden.
- Worker-Images sind exakt per Digest freigegeben, werden aber noch nicht gegen eine externe Signatur- oder Attestierungsrichtlinie geprüft.
- Inline-Secrets sind blockiert; ein externer Broker für kurzlebige, auf einzelne Jobs begrenzte Credentials fehlt.
- Das Development-Audit ist persistent, aber noch nicht kryptografisch verkettet oder extern unveränderbar gespeichert.
- Der befehlsbasierte Worker-Runner und echte Git-Integration sind vorhanden; ein autonomer LLM-Code-Worker, der selbst Änderungen plant, ist noch nicht eingebaut.
- JSONL-Run-Historie, SQLite-LTM und Vector-Datei bilden kein vollständig transaktionales Gesamtsystem.
