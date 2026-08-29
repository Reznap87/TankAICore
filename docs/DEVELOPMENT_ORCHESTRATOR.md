# TankAI Development Orchestrator

Der Development Orchestrator ist die deterministische Kontrollschicht für ein koordiniertes Software-Agententeam. Er ersetzt unkontrolliertes Selbstklonen durch genehmigte Spawn-Requests, getrennte Schreibbereiche, persistente Zustände, echte Git-Worktrees und technische Freigabe-Gates.

## Implementierter Umfang

- zentrale Quelle der Wahrheit als atomar ersetzte JSON-Datei,
- revisionsbasierter Schutz gegen veraltete Schreibvorgänge,
- Task- und Abhängigkeitsgraph mit Zyklusprüfung,
- vollständiger TECH-AI-V2-Rollenkatalog von Core-Leitung bis C++/JUCE/Realtime-Audio,
- eindeutige Agenten-IDs, Eltern-Agent, Generation, Entwicklungszyklus und Ausgangs-Commit,
- versionierte Arbeitsverträge mit Abnahmekriterien, Pflicht-Tests, Priorität und Deadlock-Regeln,
- persistierte Governance-Grenzen 40/80/5/3/1/4 und Modulkapazitätsprüfung,
- kontrollierte Spawn-Requests mit festen Limits,
- konservative Erkennung überlappender Schreibbereiche,
- persistente Datei-Sperren,
- isolierte Git-Branches und Git-Worktrees,
- reale Worker-Ausführung über explizite argv-Befehle ohne Shell,
- optionale gehärtete OCI/Docker-Ausführung für alle untrusted Codepfade,
- persistente Worker-Phasen und strukturierte Statusmeldungen,
- persistente mandantengebundene Development-Queue mit Admission-Control,
- separate monotone Lease-Fence-Datenbank pro Repository,
- aktive Prozess- und Containerbeendigung bei Lease-/Fence-Verlust,
- operatorgesteuerter Worktree-Reaper mit Dirty-Quarantäne und Branch-Erhalt,
- Rootless-/Linux-Gate für Online-Queue-Worker,
- mechanische Prüfung aller geänderten Dateien gegen `allowed_paths` und `denied_paths`,
- erneute Prüfung des vollständigen Commit-Diffs,
- verpflichtende Worker-Tests vor dem Review,
- unabhängiger Reviewer,
- unabhängiger QA-Agent,
- optional verpflichtender Security-Review,
- realer Rebase gegen `CURRENT_STABLE_COMMIT`,
- exklusiver Fast-Forward-Merge nach `MAIN`,
- verpflichtende Post-Merge-Gesamttests,
- Git-Rollback bei fehlgeschlagenen Integrationsprüfungen,
- Crash-Recovery über ein atomar geschriebenes Integrationsjournal,
- Wiedereröffnung blockierter Tasks,
- Abbruch mit Freigabe aller Datei-Sperren,
- fortlaufendes Audit-Protokoll.

## TECH AI V2 Governance

Der Projektzustand speichert die Agentengrenzen selbst. Dadurch kann ein neu gestarteter CLI- oder Worker-Prozess die Grenzen nicht durch lokale Standardwerte umgehen.

```text
MAX_ACTIVE_AGENTS = 40
MAX_TOTAL_AGENTS_PER_CYCLE = 80
MAX_CLONE_DEPTH = 5
MAX_CHILDREN_PER_AGENT = 3
MAX_AGENTS_PER_FILE = 1
MAX_AGENTS_PER_MODULE = 4
```

Ein Agentenvertrag enthält mindestens Rolle, Aufgabe, Basis-Commit, Zyklus-ID, Generation, erlaubte und verbotene Pfade, Abnahmekriterien, Pflicht-Tests, Priorität und Deadlock-Regeln. Folge-Agenten müssen weiterhin dieselbe Fachrolle wie ihr Eltern-Agent besitzen und einen disjunkten Schreibbereich erhalten.

Ein neuer Entwicklungszyklus kann ausschließlich gestartet werden, wenn alle bisherigen Agenten einen terminalen Zustand erreicht haben:

```bash
python -m tankai.dev_orchestrator.cli   --state .tankai/project-state.json   begin-cycle   --reason "Alle Aufgaben des vorherigen Zyklus sind abgeschlossen"
```

Der Zykluswechsel setzt nur das Zyklusbudget zurück. Tasks, Audit-Ereignisse, Agentenhistorie, Worker-Runs und der stabile Commit bleiben erhalten.

## Abgrenzung

Der Worker-Runner führt reale, vorher genehmigte lokale Befehle aus. Er plant nicht autonom mit einem LLM und erzeugt nicht selbstständig beliebige Code-Befehle. Eine spätere LLM-Code-Worker-Schicht muss weiterhin dieselben Task-, Pfad-, Test- und Gate-Verträge verwenden.

Nicht implementiert sind derzeit:

- automatische LLM-basierte Übersetzung eines Tasks in konkrete Codeänderungen,
- automatische semantische Konfliktauflösung,
- vollständig atomare Transaktion über Git und JSON-Datei; stattdessen wird eine wiederherstellbare Journal-Transaktion verwendet,
- externe kryptografisch manipulationsgeschützte Audit-Speicherung.

## Initialisierung

```bash
python -m tankai.dev_orchestrator.cli \
  --state .tankai/project-state.json \
  init \
  --version 1.10.0-module-ownership \
  --branch main \
  --commit "$(git rev-parse HEAD)"
```

Status anzeigen:

```bash
python -m tankai.dev_orchestrator.cli \
  --state .tankai/project-state.json \
  status
```

## Task und Agenten anlegen

```python
from tankai.dev_orchestrator import (
    DevelopmentOrchestrator,
    DevelopmentRole,
    TaskSpec,
)

commit = "abc123"
orchestrator = DevelopmentOrchestrator.initialize(
    ".tankai/project-state.json",
    current_version="1.10.0-module-ownership",
    current_branch="main",
    current_commit=commit,
)

orchestrator.create_task(
    TaskSpec(
        task_id="AUTH-017",
        goal="Sichere Refresh-Token-Rotation implementieren.",
        base_commit=commit,
        allowed_paths=[
            "backend/src/auth/**",
            "backend/tests/auth/**",
        ],
        acceptance_criteria=[
            "Alte Refresh-Tokens werden invalidiert.",
            "Wiederverwendung wird erkannt.",
        ],
        required_tests=["python -m pytest -q backend/tests/auth"],
        requires_security_review=True,
    )
)

author = orchestrator.start_agent("AUTH-017", DevelopmentRole.AUTHENTICATION)
```

Reviewer-, QA- und Security-Agenten werden als getrennte Agentenidentitäten angelegt. Der implementierende Agent darf keine dieser Freigaben selbst erteilen.

## Worker-Pipeline

Ein `WorkerPipelineJob` enthält ausschließlich explizite Prozessargumente. Es wird keine Shell gestartet.

```python
from tankai.dev_orchestrator import (
    CommandSpec,
    GateJob,
    GitWorkspaceManager,
    WorkerJob,
    WorkerPipelineJob,
    WorkerPipelineRunner,
)

job = WorkerPipelineJob(
    worker=WorkerJob(
        agent_id=author.agent_id,
        implementation_summary="Refresh-Token-Rotation implementiert.",
        commit_message="Implement refresh-token rotation",
        implementation_commands=[
            CommandSpec(argv=["python", "tools/implement_auth.py"]),
        ],
        test_commands=[
            CommandSpec(argv=["python", "-m", "pytest", "-q", "backend/tests/auth"]),
        ],
    ),
    gates=GateJob(
        reviewer_agent_id="AGENT_REVIEWER_01",
        review_commands=[
            CommandSpec(argv=["python", "-m", "compileall", "-q", "backend/src/auth"]),
        ],
        qa_agent_id="AGENT_QA_01",
        qa_commands=[
            CommandSpec(argv=["python", "-m", "pytest", "-q", "backend/tests/auth"]),
        ],
        security_agent_id="AGENT_SECURITY_01",
        security_commands=[
            CommandSpec(argv=["python", "tools/security_check.py", "backend/src/auth"]),
        ],
    ),
)

runner = WorkerPipelineRunner(
    orchestrator,
    GitWorkspaceManager(".", "../tankai-worktrees"),
)
result = runner.run(job)
print(result.run.state.value)
```

## Reale Integration nach MAIN

Ein Run mit Zustand `ready_to_integrate` wird über einen separaten `IntegrationJob` integriert. Die angegebenen Testbefehle müssen alle in `TaskSpec.required_tests` geforderten Befehle enthalten.

```python
from tankai.dev_orchestrator import (
    IntegrationJob,
    WorkerIntegrationRunner,
)

integration = WorkerIntegrationRunner(
    orchestrator,
    GitWorkspaceManager(".", "../tankai-worktrees"),
).run(
    IntegrationJob(
        run_id=result.run.run_id,
        test_commands=[
            CommandSpec(argv=["python", "-m", "pytest", "-q"]),
        ],
    )
)

print(integration.integration_commit)
```

Integrationsablauf:

1. exklusives repositoryweites Integrations-Lock erwerben,
2. Haupt-Repository gegen Branch, Commit und Sauberkeit des `ProjectState` prüfen,
3. Worker-Branch bei Bedarf real auf den aktuellen stabilen Commit rebasen,
4. Rebase-Diff erneut gegen erlaubte und gesperrte Pfade prüfen,
5. ausschließlich per `git merge --ff-only` integrieren,
6. automatisch `git diff --check` ausführen,
7. alle verpflichtenden Tests auf dem gemergten Hauptstand ausführen,
8. journalisierte Worktree-Bindungen bei einer Recovery gegen Root, Branch und Basis-Commit validieren,
9. bei Fehlern Git auf den vorherigen stabilen Commit zurücksetzen,
10. bei Erfolg Worker-Run, Task, Agent und `CURRENT_STABLE_COMMIT` gemeinsam aktualisieren,
11. Worktree und Agent-Branch kontrolliert bereinigen.

## CLI-Job

Ein vollständiger Job kann als JSON gespeichert und ausgeführt werden:

```bash
python -m tankai.dev_orchestrator.cli \
  --state .tankai/project-state.json \
  run-pipeline \
  --repository . \
  --workspace-root ../tankai-worktrees \
  --job worker-job.json
```

Minimaler Aufbau:

```json
{
  "worker": {
    "agent_id": "AGENT_BACKEND_01",
    "implementation_summary": "Authentifizierungslogik implementiert.",
    "commit_message": "Implement authentication",
    "implementation_commands": [
      {"argv": ["python", "tools/implement_auth.py"]}
    ],
    "test_commands": [
      {"argv": ["python", "-m", "pytest", "-q", "backend/tests/auth"]}
    ]
  },
  "gates": {
    "reviewer_agent_id": "AGENT_REVIEWER_01",
    "review_commands": [
      {"argv": ["python", "-m", "compileall", "-q", "backend/src/auth"]}
    ],
    "qa_agent_id": "AGENT_QA_01",
    "qa_commands": [
      {"argv": ["python", "-m", "pytest", "-q", "backend/tests/auth"]}
    ]
  }
}
```

Integrationsjob:

```json
{
  "run_id": "RUN-AUTH-017-0123456789ab",
  "test_commands": [
    {"argv": ["python", "-m", "pytest", "-q", "backend/tests/auth"]}
  ],
  "cleanup_workspace_on_success": true,
  "delete_branch_on_success": true
}
```

Ausführen:

```bash
python -m tankai.dev_orchestrator.cli \
  --state .tankai/project-state.json \
  integrate \
  --repository . \
  --workspace-root ../tankai-worktrees \
  --job integration-job.json
```

## Sicherheits- und Integritätsregeln

Der Worker-Runner erzwingt folgende Regeln:

1. Agent und Task müssen aktiv und einander zugewiesen sein.
2. Der Worktree startet exakt auf dem bestätigten Basis-Commit.
3. Implementierungsbefehle dürfen `HEAD` nicht selbst verändern.
4. Nicht erlaubte oder explizit gesperrte Pfade blockieren den Run.
5. TankAI erstellt den Implementierungs-Commit selbst.
6. Der vollständige Commit-Diff wird erneut gegen die Pfadregeln geprüft.
7. Worker-Tests laufen auf dem erzeugten Commit.
8. Review-, QA- und Security-Befehle dürfen weder `HEAD` noch versionierte Dateien verändern.
9. Unversionierte Prüfarbeitsdateien werden nur im isolierten Worktree entfernt.
10. Erst nach allen erforderlichen Gates erhält der Run `ready_to_integrate`.

## Spawn-Regeln

Ein Spawn wird abgelehnt, wenn mindestens eine Bedingung verletzt ist:

- Eltern-Agent ist nicht aktiv.
- Rolle des Kindes unterscheidet sich von der Elternrolle.
- Ausgangs-Commit ist nicht `CURRENT_STABLE_COMMIT`.
- Teilaufgabe oder Abnahmekriterien fehlen.
- Schreibbereich kollidiert mit einer bestehenden Datei-Sperre.
- Abhängigkeiten sind nicht integriert.
- `MAX_ACTIVE_AGENTS`, `MAX_CLONE_DEPTH` oder `MAX_CHILDREN_PER_AGENT` ist erreicht.

Standardwerte:

```text
MAX_ACTIVE_AGENTS = 12
MAX_CLONE_DEPTH = 3
MAX_CHILDREN_PER_AGENT = 2
```

## Persistenz

Der Zustand wird atomar gespeichert. Ein Lock-File verhindert gleichzeitige lokale Writer. Jede Änderung erhöht `revision`. Ein Writer mit alter Revision wird über `StateConflictError` abgewiesen.

Schema-Version 4 enthält zusätzlich:

```text
worker-runs
worker-run-id pro Task
implementation-commit pro Task
Worker-Phasen
strukturierte Statusmeldungen
Befehls- und Gate-Ergebnisse
rebased-from-commit
rebased-commit
integration-commit
Post-Merge-Testresultate
execution-backend
worker-isolation-policy
integration-isolation-policy
```

Ältere Schema-Version-1-, Schema-Version-2- und Schema-Version-3-Dateien werden beim Laden kompatibel ergänzt.

## Container-Isolation ab 1.2.0

`WorkerPipelineJob.isolation` aktiviert den OCI/Docker-Executor. Die Richtlinie ist Teil des persistenten Worker-Runs. Sie erzwingt ein read-only Root-Dateisystem, `network=none`, Capability-Drop, `no-new-privileges`, private IPC, feste CPU-/RAM-/PID-/FD-Limits und ein begrenztes tmpfs. Die Host-Umgebung wird nicht vererbt.

Der Worktree wird zunächst vollständig read-only nach `/workspace` gemountet. Nur die aus `allowed_paths` abgeleiteten Bereiche werden während der Implementierung als beschreibbare Nested-Mounts freigegeben; vorhandene exakt erlaubte Dateien bleiben einzelne Datei-Mounts. Worker-Tests, Review, Security, QA und Integrationstests sehen den Workspace ausschließlich read-only. Eine Worktree-`.git`-Datei wird durch einen read-only `/dev/null`-Mount, ein `.git`-Verzeichnis durch ein read-only tmpfs verdeckt. Ausgaben werden auf 10.000 Byte begrenzt; bei Timeout wird der benannte Container zwangsweise entfernt.

Produktive Images müssen per sha256-Digest fixiert sein. Ein containergeprüfter Run darf bei der Integration nicht auf Host-Tests zurückgestuft werden. `TANKAI_REQUIRE_WORKER_ISOLATION=1` blockiert Jobs ohne Isolationsrichtlinie.

Der öffentliche Webdienst darf keinen Docker-/Podman-Socket erhalten. Der Executor gehört in einen dedizierten nicht-root Runner-Prozess mit rootless Runtime. Die mitgelieferten Tests prüfen Richtlinie, Mount-Plan, Gate-Abdeckung und Downgrade-Schutz; eine reale Docker-Runtime ist in der Entwicklungsumgebung dieses Releases nicht vorhanden gewesen.

## Begrenzter paralleler Worker-Pool

`WorkerPoolRunner` kann zwei bis zwölf bereits genehmigte `WorkerPipelineJob`-Objekte gleichzeitig ausführen. Der Pool erzeugt weder Tasks noch Agenten. Vor dem Start prüft er aktive exklusive Zuweisungen und konservativ überschneidungsfreie `allowed_paths`. Jeder Worker verwendet einen eigenen Git-Worktree. Änderungen an gemeinsamen Worktree-/Branch-Metadaten laufen über ein repositoryweites Lock; der persistente `ProjectStateStore` serialisiert State-Transaktionen.

Die Ausführung endet pro Worker bei `ready_to_integrate`. Der Pool führt keinen parallelen MAIN-Merge aus. Rebase, Merge, Post-Merge-Tests und State-Commit bleiben im exklusiven `WorkerIntegrationRunner`. Der CLI-Einstieg lautet `run-pool`.

## Sicherheitsgrenze des lokalen Runners

Host-Ausführung bleibt für lokale Kompatibilität verfügbar, besitzt aber keine Betriebssystem-Sandbox. Sie darf nicht für öffentlich eingereichte oder nicht vertrauenswürdige Entwicklungsaufträge verwendet werden.

## Online-Mandantentrennung ab 1.1.0

Die Webplattform verwendet eine separate Auth-Datenbank als Autoritätsquelle. Web-Runs, Memory, LTM, Vektoren und Historien sind pro Workspace getrennt. Seit Version 1.3 bindet die persistente Development-Queue jeden Worker-Payload zusätzlich an `user_id`, `tenant_id`, `workspace_id` und ein operatorseitig registriertes Repository. Admission-Richtlinien begrenzen Rollen, Queue-Größe, Parallelität, CPU, RAM, PIDs, Laufzeit, Retry-Anzahl und exakte Image-Digests. Das External Agent Gateway v1 ergänzt adminverwaltete Service-Agenten, gehashte zeitlich begrenzte Bearer-Tokens sowie Scope-, Repository- und agentenspezifische Job-Grenzen vor derselben Queue. Der vollständige Vertrag steht in `docs/EXTERNAL_AGENT_API.md`. Für Multi-Host-Ausführung fehlen weiterhin eine serverbasierte Queue, externe Image-Signaturprüfung, kurzlebige Credentials und zentrales Monitoring.

## Persistente Queue und Admission-Control

`DevelopmentJobQueue` ist eine vom `ProjectState` getrennte SQLite-Kontrollschicht. Diese Trennung verhindert, dass Webrequests direkt Git-Worktrees oder Hostpfade erzeugen.

Verbindliche Bindungen eines Queue-Jobs:

```text
user_id
tenant_id
workspace_id
repository_id
payload_sha256
immutable container image
memory / CPU / PID / runtime budget
```

Der Payload-Hash umfasst Identitäten, Repository und den kanonischen `WorkerPipelineJob`. Änderungen an einem dieser Felder führen vor dem Lease zu einem Integritätsfehler.

Ablauf:

```text
Session + CSRF
→ Workspace-/Rollenprüfung
→ Repository-ID gegen persistente Registrierung
→ Admission-Richtlinie
→ persistentes queued
→ atomarer Queue-Lease mit opakem Token
→ externer Repository-Fence mit monotoner Epoche
→ Runner-Filesystem- und Git-Prüfung
→ WorkerPipelineRunner
→ succeeded oder failed
```

Der Webprozess sieht keine Repository-Dateien. Der Runner sieht keine Auth-Datenbank. Ein separater administrativer Einrichtungsprozess setzt Policies und Repository-Bindungen.

Die Queue ist für einen Single-Host-Betrieb ausgelegt. SQLite-Dateien dürfen nicht über NFS oder zwischen mehreren Hosts geteilt werden.


## Aktiver Abbruch und Worktree-Recovery ab 1.5.0

Jeder laufende Worker-Befehl erhält eine `cancellation_check`-Funktion. Der Prozess-Executor prüft sie in kurzen Intervallen. Schlägt die Queue-Heartbeat-Verlängerung fehl oder ist der externe Fence nicht mehr gültig, wird die lokale Prozessgruppe beendet. Bei Container-Ausführung wird anschließend der über `--name` eindeutig adressierte Container mit `rm -f` entfernt. Erst danach wird der ursprüngliche Lease-/Fence-Fehler an die Pipeline zurückgegeben.

Verwaiste Worktrees werden nicht vom möglicherweise veralteten Worker selbst gelöscht. Ein Owner oder Admin verwendet nach Queue-/Fence-Recovery den Operator-Reaper:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/queue/development-fences.db \
  --auth-db /srv/tankai/auth/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  reap-worktrees \
  --actor-email operator@example.com \
  --workspace-id WORKSPACE_ID \
  --repository-id REPOSITORY_ID \
  --min-age-seconds 3600
```

Ohne `--apply` ist der Befehl ein Dry-Run. Eine echte Bereinigung wird bei aktivem Fence oder aktivem Queue-Job blockiert. Schmutzige Worktrees werden als `quarantined` gemeldet. Für einen stale, im ProjectState noch nicht terminalen Run ist zusätzlich `--expected-stale-run-id RUN_ID --apply` erforderlich. Der Worktree wird entfernt, der Branch bleibt erhalten und kann für Nacharbeit wieder eingehängt werden.

## Externes Lease-Fencing ab 1.4.0

`DevelopmentJobQueue` verwendet neben der Queue-Datenbank eine eigenständige `LeaseFenceStore`-Datenbank. Jeder aktive Repository-Scope erhält eine streng monoton steigende Epoche. Der Worker muss bei allen kontrollierten Mutationspunkten drei Werte gleichzeitig nachweisen:

```text
repository scope
job_id
fence_epoch + opaker lease token
```

Ein neuerer Fence macht jede ältere Epoche ungültig. Das gilt auch dann, wenn ein veralteter Prozess noch eine alte Queue-Kopie besitzt oder eine Queue-Sicherung zurückgespielt wurde. Queue-Start, Heartbeat, Worker-Phasen, Commit, Review-/QA-/Security-Gates und Abschluss prüfen Queue-Lease und Fence fail-closed.

Die Fence-Datenbank muss von der Queue-Datenbank getrennt sein. Beide liegen auf lokalem, dauerhaftem Speicher. Ein aktiver Fence überstimmt einen nur lokal abgelaufenen Queue-Zeitstempel; dadurch wird kein zweiter Worker für dasselbe Repository gestartet. Eine Operator-Recovery verlangt Owner-/Admin-Rechte sowie die exakt bestätigte Repository-ID, Job-ID und Epoche. Vor dem erzwungenen Ablauf muss der alte Prozess oder Container nachweislich beendet sein.

Runtime-Vorabprüfung:

```bash
python -m tankai.dev_orchestrator.runtime_cli --container-runtime docker
```

Für Online-Queue-Worker müssen `rootless=true` und `os_type=linux` gemeldet werden. Das aktuelle SQLite-Fencing ist ausdrücklich kein Multi-Host-Konsenssystem; dafür ist ein externer transaktionaler Koordinator mit atomarem Compare-and-Swap erforderlich.
## Labelgebundener Container-Reaper ab 1.6.0

Container aus Queue-Worker-Läufen erhalten zusätzlich zu `tankai.managed`, `tankai.run_id` und `tankai.phase` folgende vom Dispatcher erzeugte Labels:

```text
tankai.tenant_id
tankai.workspace_id
tankai.repository_id
tankai.job_id
tankai.fence_epoch
tankai.worker_id
```

Der Operator-Reaper listet nur `tankai.managed=true` plus exakt passende Repository-ID. Vor einer Entfernung prüft er anschließend die vollständige Mandanten- und Workspace-Bindung, das Mindestalter, den Queue-State und den aktuellen externen Fence. Live-Leases und der aktuelle Fence sind geschützt. Für unbekannte oder nicht-terminale stale Jobs ist eine exakte Bestätigung aus Job-ID und Fence-Epoche erforderlich.

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/queue/development-fences.db \
  --auth-db /srv/tankai/auth/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  reap-containers \
  --actor-email operator@example.com \
  --workspace-id WORKSPACE_ID \
  --repository-id REPOSITORY_ID \
  --container-runtime docker \
  --min-age-seconds 3600
```

Ohne `--apply` werden ausschließlich Entscheidungen ausgegeben. Runtime-Container-IDs werden gegen ein enges Hex-Schema validiert und ohne Shell an `docker rm -f` beziehungsweise `podman rm -f` übergeben.

## Release-Publikationsledger

Der Development-Orchestrator kann ab `1.8.0-publication-ledger` einen lokalen Release-Stand mit externen Connector-Receipts verknüpfen. Der Orchestrator selbst führt dabei keine stillen Uploads aus. Externe Aktionen liefern Remote-ID, URL, Größe und Digest beziehungsweise einen exakten GitHub-Commit zurück. Erst danach wird ein hashverkettetes Receipt in das Ledger aufgenommen.

CLI:

```bash
python -m tankai.dev_orchestrator.publication_cli --help
```

Details: [`RELEASE_PUBLICATION.md`](RELEASE_PUBLICATION.md).
