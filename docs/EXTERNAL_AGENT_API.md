# External Agent Gateway v1

Das External Agent Gateway erlaubt externen KI-Systemen, kontrollierte
Development-Jobs an TankAICore zu übergeben. Externe Agenten erhalten weder
Hostpfade noch Shell-, Datenbank- oder direkte Git-Zugänge. Jeder Auftrag läuft
weiterhin durch die bestehende mandanten- und repositorygebundene
Development-Queue und deren Admission-, Container-, Lease- und Review-Gates.

## Ehrliche Funktionsgrenze

Version 1 ist eine Machine-to-Machine-Schnittstelle für bereits vollständig
definierte `WorkerPipelineJob`-Aufträge. Sie erzeugt noch nicht selbstständig
aus einem freien Zieltext eine ausführbare Programmier-Pipeline. Ein autonomer
Goal-to-Code-Compiler ist ein separates späteres Gate.

KI-Agenten registrieren sich nicht selbst. Ein menschlicher Workspace-Owner
oder -Admin legt ein Service-Agentenkonto an, wählt die freigegebenen
Repositories und Scopes und erzeugt einen zeitlich begrenzten Token. Der
Roh-Token wird genau einmal zurückgegeben.

## Voraussetzungen

- `TANKAI_AUTH_MODE=session`
- `TANKAI_DEV_QUEUE_ENABLED=1`
- eine aktive Queue-Richtlinie für den Workspace
- mindestens ein operatorseitig registriertes Repository
- ein fest per Digest oder Image-ID freigegebenes Worker-Image

## Scopes

| Scope | Wirkung |
|---|---|
| `repositories:read` | Freigegebene Repository-Metadaten lesen |
| `jobs:read` | Nur Jobs dieses Service-Agenten lesen |
| `jobs:submit` | Jobs einreichen; benötigt zusätzlich `jobs:read` |
| `jobs:cancel` | Eigene, noch nicht geleaste Jobs abbrechen; benötigt `jobs:read` |

Ein Token ist zusätzlich an höchstens 50 konkrete Repository-UUIDs gebunden.
Ein Service-Agent kann keine Jobs eines anderen Service-Agenten sehen, selbst
wenn beide demselben menschlichen Owner und Repository zugeordnet sind.

## Verwaltungsendpunkte

Diese Endpunkte verwenden die vorhandene Session-Cookie-Authentifizierung und
bei `POST` den vorhandenen CSRF-Schutz.

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/agents` | Agenten im aktiven Workspace auflisten |
| `POST` | `/api/agents` | Service-Agent anlegen |
| `GET` | `/api/agents/{agent_id}/tokens` | Token-Metadaten ohne Geheimnis auflisten |
| `POST` | `/api/agents/{agent_id}/tokens` | Token einmalig erzeugen |
| `POST` | `/api/agents/{agent_id}/tokens/{token_id}/revoke` | Token sofort widerrufen |
| `POST` | `/api/agents/{agent_id}/deactivate` | Agent deaktivieren und alle Tokens widerrufen |

Beispiel zum Erzeugen eines Tokens nach erfolgter Browser-Anmeldung:

```http
POST /api/agents/AGENT_UUID/tokens
X-CSRF-Token: SESSION_CSRF_TOKEN
Content-Type: application/json

{
  "label": "Claude coding client",
  "scopes": [
    "repositories:read",
    "jobs:submit",
    "jobs:read",
    "jobs:cancel"
  ],
  "repository_ids": ["REPOSITORY_UUID"],
  "expires_in_days": 30
}
```

Das Feld `secret` in der Antwort muss sofort in einem Secret-Manager abgelegt
werden. Spätere Listenantworten enthalten nur `token_prefix`, Metadaten und
Nutzungszeitpunkte.

## Operator-CLI für den Single-Host-Betrieb

Auf dem dedizierten Queue-Host können Owner und Admins denselben Lifecycle ohne
Browser-Session und CSRF-Übertragung über die lokale Operator-CLI verwalten. Die
CLI benötigt Zugriff auf Auth- und Queue-Datenbank. Sie prüft Repository-IDs
gegen die aktiven Registrierungen des ausgewählten Workspaces.

Service-Agent anlegen:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --auth-db /srv/tankai/data/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  create-service-agent \
  --actor-email admin@example.com \
  --workspace-id WORKSPACE_UUID \
  --name "External Coder" \
  --description "Freigegebener Programmierclient"
```

Token erzeugen und die einmalige Ausgabe direkt in eine geschützte Datei
schreiben:

```bash
umask 077
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --auth-db /srv/tankai/data/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  create-agent-token \
  --actor-email admin@example.com \
  --workspace-id WORKSPACE_UUID \
  --agent-id AGENT_UUID \
  --scope repositories:read \
  --scope jobs:submit \
  --scope jobs:read \
  --scope jobs:cancel \
  --repository-id REPOSITORY_UUID \
  --expires-in-days 30 \
  --label "production client" \
  > /srv/tankai/secrets/external-coder-token.json
```

Weitere Lifecycle-Befehle sind `list-service-agents`, `list-agent-tokens`,
`revoke-agent-token` und `deactivate-service-agent`. Listen geben niemals den
Roh-Token aus. Die Deaktivierung widerruft alle noch aktiven Tokens des Agenten
atomar.

Vor dem ersten Clientzugriff kann ein Owner oder Admin den gespeicherten
Konfigurationsvertrag zusammenhängend prüfen:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --auth-db /srv/tankai/data/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  bootstrap-readiness \
  --actor-email admin@example.com \
  --workspace-id WORKSPACE_UUID
```

Der JSON-Receipt enthält ausschließlich aggregierte Zähler, keine Token-IDs,
Token-Präfixe, Roh-Tokens oder Hostpfade. Exitcode `0` und `ready=true` verlangen
eine aktive mandantengebundene Policy, mindestens eine weiterhin gültige aktive
Git-Registrierung und mindestens einen nicht widerrufenen beziehungsweise nicht
abgelaufenen `jobs:read`-/`jobs:submit`-Token eines einreichungsberechtigten
Service-Agenten mit Repository-Überschneidung. Exitcode `2` bedeutet unvollständige
Konfiguration. Der Receipt ist nur eine Momentaufnahme: Er prüft weder Host noch
laufenden Web-/Worker-Prozess und ersetzt nicht die erneute Admission beim Submit.

## Machine-to-Machine-Endpunkte

Alle v1-Endpunkte benötigen:

```http
Authorization: Bearer tkai_v1_REDACTED
```

| Methode | Pfad | Scope |
|---|---|---|
| `GET` | `/api/v1/capabilities` | gültiger Token |
| `GET` | `/api/v1/job-schema` | gültiger Token |
| `GET` | `/api/v1/job-result-schema` | gültiger Token |
| `GET` | `/api/v1/repositories` | `repositories:read` |
| `GET` | `/api/v1/jobs` | `jobs:read` |
| `GET` | `/api/v1/jobs/{job_id}` | `jobs:read` |
| `GET` | `/api/v1/jobs/{job_id}/history` | `jobs:read` |
| `POST` | `/api/v1/jobs/preflight` | `jobs:submit` |
| `POST` | `/api/v1/jobs` | `jobs:submit` |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | `jobs:cancel` |

### Maschinenlesbarer Fehlervertrag

Alle JSON-Fehlerantworten der oben aufgeführten, unterstützten `/api/v1/`-Routen behalten das
bisherige menschenlesbare Feld `error` und ergänzen zwei stabile Maschinenfelder:

```json
{
  "error": "Agenten-Scope fehlt: jobs:submit",
  "error_code": "missing_scope",
  "error_contract_version": 1,
  "retryable": false,
  "retry_after_seconds": null
}
```

Clients werten `error_code` aus und zeigen `error` nur als Diagnose an. Die Capability-Antwort
beschreibt unter `error_contract`, welche Felder zum Vertrag gehören, und veröffentlicht die
vollständige Code-Menge in `codes`. Strukturierte Envelope-Fehler verwenden zusätzlich weiterhin
den begrenzten `validation`-Block mit JSON-Pointern. Die aktuelle v1-Code-Menge ist:

| Code | HTTP-Status | Bedeutung |
|---|---:|---|
| `bearer_token_required` | 401 | Bearer-Header fehlt oder ist nicht wohlgeformt |
| `invalid_agent_token` | 401 | Token ist ungültig, abgelaufen oder widerrufen |
| `missing_scope` | 403 | Dem Token fehlt der benötigte Scope |
| `development_queue_unavailable` | 404 | Development-Queue ist für diese Runtime nicht verfügbar |
| `invalid_request_body` | 400/413 | JSON-Body fehlt, ist ungültig, kein Objekt oder zu groß |
| `invalid_job_submission` | 400 | Das Job-Envelope verletzt den veröffentlichten Schema-Vertrag |
| `repository_not_allowed` | 403 | Repository liegt außerhalb der Token-Allowlist |
| `job_submission_forbidden` | 403 | Serverseitige Einreichungsberechtigung fehlt |
| `job_submission_rejected` | 400 | Aktuelle Admission-Regeln lehnen die Einreichung ab |
| `repository_list_forbidden` | 403 | Repository-Liste darf nicht gelesen werden |
| `invalid_job_pagination` | 400 | Limit oder Cursor verletzt den Paginierungsvertrag |
| `invalid_job_filter` | 400 | Repository-Filter verletzt den Joblistenvertrag |
| `job_list_forbidden` | 403 | Jobliste darf nicht gelesen werden |
| `job_not_found` | 404 | Job fehlt oder bleibt wegen Agenten-/Repository-Isolation verborgen |
| `job_state_conflict` | 409 | Status oder Verlauf ist im aktuellen Queue-Zustand nicht verfügbar |
| `job_cancel_conflict` | 409 | Job kann im aktuellen Zustand nicht abgebrochen werden |
| `endpoint_not_found` | 404 | v1-Pfad ist unbekannt |

HTTP-Status und `error`-Text bleiben für bestehende Integrationen erhalten. Fehlercodes enthalten
keine Token, Payloadwerte, Hostpfade oder internen Ausnahmearten. Berechtigungen und die bewusst
neutrale `404`-Antwort für fremde Jobs werden dadurch nicht gelockert.

Jede unterstützte Fehlerantwort enthält außerdem den versionierten Retry-Vertrag.
`retryable=false` bedeutet, dass derselbe unveränderte Request nicht automatisch wiederholt
werden soll. Nur vorübergehende Queue-Kapazitäts- und Stundenlimit-Ablehnungen eines ansonsten
gültigen Submits liefern `retryable=true`. Beim Stundenlimit nennt `retry_after_seconds` die
begrenzte Wartezeit und der HTTP-Header `Retry-After` denselben Wert. Bei voller Queue bleibt die
Wartezeit `null`, weil TankAI keinen seriösen Zeitpunkt für frei werdende Kapazität vorhersagen
kann. Der Client wartet in diesem Fall auf eine Zustandsänderung und verwendet Backoff statt
einer engen Retry-Schleife.

Capability-Discovery veröffentlicht unter `error_contract.retry` Version, Feldnamen und
Headernamen. Die Retry-Metadaten ändern weder vorhandene Fehlercodes noch HTTP-Status und umgehen
keine erneute Token-, Scope-, Repository-, Policy- oder Queue-Prüfung.

`GET /api/v1/capabilities` nennt unter `job_submission` den Submit- und
Preflight-Pfad, die HTTP-Methode, Pfad und Version des zugehörigen Schemas sowie
Version, Pfadformat und Obergrenze strukturierter Validierungsfehler. Ein Client kann danach den
vollständigen JSON-Schema-Draft-2020-12-Vertrag abrufen:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $TANKAI_AGENT_TOKEN" \
  https://TANKAI_HOST/api/v1/job-schema
```

Das Schema `urn:tankai:external-agent-job-submission:v1` beschreibt denselben
Pydantic-Vertrag, den `POST /api/v1/jobs` validiert: die erlaubten Envelope-
Felder, Pflichtfelder, Idempotenz- und Prioritätsgrenzen sowie die vollständige
`WorkerPipelineJob`-Struktur einschließlich Container-Isolation. Unbekannte
Felder sind nicht zulässig. Das Schema gewährt keine Berechtigung und ersetzt
weder Token-Scopes noch Repository-Allowlist, Workspace-Policy, freigegebene
Image-Digests oder Ressourcenbudgets; diese Laufzeit-Gates werden bei jeder
Einreichung erneut geprüft.

### Versionierter Admission-Policy-Snapshot

`GET /api/v1/capabilities` liefert bei konfigurierter Development-Queue unter
`queue_policy` einen versionierten Snapshot der aktuell für den Workspace
geltenden Einreichungsgrenzen. Dazu gehören insbesondere die unveränderlich per
SHA-256 gepinnten Worker-Images, Ressourcen- und Laufzeitbudgets, Queue-Grenzen,
maximale Versuche und das stündliche Nutzerlimit:

```json
{
  "queue_policy": {
    "version": 1,
    "snapshot_only": true,
    "final_submit_revalidates": true,
    "enabled": true,
    "max_queued": 10,
    "max_running": 1,
    "max_memory_mb": 512,
    "max_cpus": 2.0,
    "max_pids": 128,
    "max_runtime_seconds": 120,
    "max_attempts": 3,
    "max_jobs_per_user_hour": 20,
    "allowed_images": [
      "tankai-worker@sha256:APPROVED_DIGEST"
    ]
  }
}
```

Ein KI-Client kann damit vor dem Preflight ein tatsächlich freigegebenes Image
auswählen und seine Ressourcenanforderungen begrenzen. Der Receipt ist
ausdrücklich nur eine Momentaufnahme: Er reserviert weder Kapazität noch Quote
und ersetzt keine Berechtigung. Preflight und echter Submit lesen die aktuelle
Policy erneut; nur deren jeweilige Antwort ist für diesen Aufruf maßgeblich. Ist
keine Policy konfiguriert, bleibt `queue_policy` wie bisher `null`.

### Admission-Preflight ohne Einreihung

Ein Client kann denselben Auftrag vor dem Submit gegen die aktuell stabilen
Admission-Gates prüfen:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $TANKAI_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  --data @job.json \
  https://TANKAI_HOST/api/v1/jobs/preflight
```

Der Preflight verwendet dasselbe strikt typisierte Envelope und dieselbe interne
Admission-Funktion wie `POST /api/v1/jobs`. Er prüft Bearer-Scope,
Repository-Allowlist und -Bindung, Workspace-/Queue-Policy, Rollen,
Container-Isolation, Image-Digest, Ressourcen- und Laufzeitbudget,
Inline-Secret-Sperre sowie Payload-Größe. Bei Erfolg liefert er beispielsweise:

```json
{
  "preflight": {
    "valid": true,
    "snapshot_only": true,
    "job_enqueued": false,
    "final_submit_revalidates": true,
    "queue_capacity_reserved": false,
    "idempotency_reserved": false,
    "repository_id": "REPOSITORY_UUID",
    "image": "tankai-worker@sha256:APPROVED_DIGEST",
    "memory_mb": 512,
    "cpus": 1.0,
    "pids_limit": 128,
    "runtime_seconds": 480,
    "max_attempts": 3,
    "payload_bytes": 2048,
    "dynamic_checks_deferred": [
      "idempotency",
      "queue_capacity",
      "user_rate_limit"
    ]
  }
}
```

Der Aufruf erzeugt keinen Development-Job und keine Agent-Job-Freigabe; nur der
sicherheitsrelevante Audit-Eintrag wird protokolliert. Er reserviert weder einen
Queue-Platz noch den Idempotenzschlüssel. Diese dynamischen Zustände können sich
nach dem Snapshot ändern und werden deshalb erst beim echten Submit zusammen mit
allen stabilen Regeln atomar beziehungsweise erneut geprüft. Ein erfolgreicher
Preflight ist folglich keine Annahmegarantie.

### Begrenzte Joblisten-Paginierung

`GET /api/v1/capabilities` bewirbt unter `job_monitoring` den Listenpfad und den
versionierten Pagination- und Filtervertrag. Ohne Query-Parameter liefert
`GET /api/v1/jobs` wie bisher die bis zu 100 neuesten eigenen Jobs. Kleinere Seiten können
mit `limit` zwischen 1 und 100 angefordert werden:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $TANKAI_AGENT_TOKEN" \
  "https://TANKAI_HOST/api/v1/jobs?limit=25"
```

Jede Antwort enthält zusätzlich den tatsächlich verwendeten Grenzwert und den
Cursor für die nächste Seite:

```json
{
  "jobs": [],
  "filters": {
    "version": 1,
    "repository_id": null
  },
  "pagination": {
    "version": 1,
    "limit": 25,
    "next_cursor": "JOB_UUID"
  }
}
```

Ist `next_cursor` nicht `null`, übergibt der Client den Wert unverändert als
`cursor` an die nächste Anfrage. Die Sortierung ist stabil und erreicht dadurch
auch Jobs jenseits der ersten 100 Einträge. Der Cursor ist an denselben
Service-Agenten und die aktuelle Repository-Allowlist gebunden. Ungültige,
fremde, mehrfach angegebene oder unbekannte Pagination-Parameter liefern eine
neutrale HTTP-400-Antwort, ohne den übermittelten Wert zu spiegeln.

Besitzt ein Token Freigaben für mehrere Repositories, kann der Client dieselbe Liste mit der
maschinenlesbar beworbenen `repository_id` auf genau eine ID aus
`GET /api/v1/capabilities.repository_ids` begrenzen:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $TANKAI_AGENT_TOKEN" \
  "https://TANKAI_HOST/api/v1/jobs?repository_id=REPOSITORY_UUID&limit=25"
```

Die Antwort wiederholt ausschließlich die kanonische, freigegebene ID unter `filters`. Ein
Cursor darf nur mit demselben Repository-Filter weiterverwendet werden; ein Cursor aus einem
anderen Repository wird neutral als ungültige Paginierung abgewiesen. Syntaktisch ungültige
Filter liefern `invalid_job_filter`, nicht freigegebene gültige IDs
`repository_not_allowed`. Weder Antwort noch Fehler spiegeln fremde Eingabewerte. Der Filter
wird vor dem `ETag`-Vergleich geprüft und kann deshalb keine Authentifizierungs-, Scope- oder
Repository-Grenze umgehen.

### Begrenzter Job-Zustandsverlauf

Nach dem Submit bewirbt `GET /api/v1/capabilities` unter `job_monitoring` den
Status- und History-Pfad sowie die History-Version. Ein Client mit `jobs:read`
kann den Verlauf seines eigenen Jobs abrufen:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $TANKAI_AGENT_TOKEN" \
  "https://TANKAI_HOST/api/v1/jobs/JOB_UUID/history"
```

Die Antwort ist eine Momentaufnahme und enthält höchstens die 100 neuesten
öffentlichen Zustandswechsel in chronologischer Reihenfolge:

```json
{
  "history": {
    "version": 1,
    "job_id": "JOB_UUID",
    "snapshot_only": true,
    "truncated_before": false,
    "events": [
      {"state": "queued", "occurred_at": "2026-09-10T06:30:00+00:00"},
      {"state": "leased", "occurred_at": "2026-09-10T06:30:05+00:00"},
      {"state": "running", "occurred_at": "2026-09-10T06:30:06+00:00"}
    ]
  }
}
```

`truncated_before=true` bedeutet, dass ältere Zustände nicht in dieser Antwort
enthalten sind. Die Route prüft dieselbe Agenten-Jobfreigabe und die aktuelle
Repository-Allowlist wie der Einzelstatus. Jobs anderer Service-Agenten bleiben
auch bei gemeinsamem Owner und Repository mit `404` verborgen. Veröffentlicht
werden nur Zustand und UTC-Zeit; interne Eventtypen, Details, Fehlertexte,
Akteur-/Worker-IDs, Fence-Epochen und die globale Queue-Sequenz bleiben privat.

### Bedingtes Joblisten- und Status-Polling

Die paginierte Jobliste, der Einzelstatus und der Zustandsverlauf liefern bei einer erfolgreichen
`200`-Antwort jeweils einen starken `ETag` über genau ihre bereits gefilterte öffentliche
JSON-Darstellung. Der Client kann diesen Wert beim nächsten Poll derselben URL unverändert
mitsenden:

```bash
curl --silent --show-error --include \
  -H "Authorization: Bearer $TANKAI_AGENT_TOKEN" \
  -H 'If-None-Match: "ZUVOR_GELIEFERTER_ETAG"' \
  "https://TANKAI_HOST/api/v1/jobs/JOB_UUID"
```

Ist die öffentliche Darstellung unverändert, folgt `304 Not Modified` mit demselben `ETag` und
ohne JSON-Body. Ein neuer freigegebener Job, ein Zustandswechsel oder eine andere paginierte
Listenseite verändert die Darstellung und liefert wieder `200` mit neuem `ETag`. Jede Anfrage
muss weiterhin authentifiziert sein; Scope, konkrete Agenten-Jobfreigabe und aktuelle
Repository-Allowlist werden vor dem Vergleich geprüft. Ein fremder Job bleibt daher auch mit
einem bekannten Validator als `404` verborgen. Überlange oder nicht passende Header werden
ignoriert und lösen eine normale `200`-Antwort aus. `Cache-Control: no-store` bleibt erhalten;
der maschinelle Client verwaltet den Validator ausdrücklich pro angefragter URL selbst.

Der versionierte `conditional_get`-Block unter `job_monitoring` nennt Request-Header,
Response-Header, unterstützte Pfade und den Statuscode für unveränderte Antworten
maschinenlesbar.

### Maschinenlesbarer Zustands- und Abbruchvertrag

Unter `job_monitoring.state_contract` veröffentlicht die Capability-Discovery die vollständige
v1-Zustandsmenge `queued`, `leased`, `running`, `succeeded`, `failed` und `cancelled`. Die
terminalen Zustände sind separat als `succeeded`, `failed` und `cancelled` ausgewiesen. Jede
Jobdarstellung enthält passend dazu das boolesche Feld `terminal`; bei `true` kann der Client das
Status-Polling beenden.

Der Vertrag bewirbt außerdem den Abbruch als `POST /api/v1/jobs/{job_id}/cancel`, den benötigten
Scope `jobs:cancel` und `queued` als einzigen zulässigen Zustand. Der Client kann diese Angaben
zur Ablaufsteuerung verwenden, sie ersetzen aber keine serverseitige Prüfung. TankAI prüft beim
Aufruf erneut Token, Scope, konkrete Agenten-Jobfreigabe, aktuelle Repository-Allowlist und den
Queue-Zustand. Bereits geleaste, laufende, erfolgreiche oder fehlgeschlagene Jobs werden nicht
über diesen Endpunkt abgebrochen.

### Idempotenter Abbruch-Outcome

Ein erfolgreicher Abbruch liefert neben dem terminalen Job einen versionierten Outcome:

```json
{
  "job": {
    "job_id": "JOB_UUID",
    "state": "cancelled",
    "terminal": true
  },
  "cancellation": {
    "version": 1,
    "replayed": false
  }
}
```

Geht diese Antwort beim Client verloren, kann er denselben Abbruch gefahrlos wiederholen. Ist
der Job bereits `cancelled`, antwortet TankAI erneut mit HTTP 200 und `replayed=true`, ohne einen
zweiten Zustandswechsel oder ein zweites öffentliches Abbruchereignis anzulegen. Die Entscheidung
erfolgt unter derselben Queue-Schreibsperre wie der erste Abbruch. Andere nicht abbrechbare
Zustände liefern weiterhin `job_cancel_conflict` mit HTTP 409.

Der `cancel`-Block der Capability-Discovery nennt `cancellation` als `response_field`,
`replayed` als `replay_field` und `cancelled` unter `idempotent_replay_states`. Ein Replay
umgeht keine Zugriffskontrolle: Token, Scope, Agenten-Jobfreigabe, Repository-Allowlist und
Workspace-Zuordnung werden auch bei jeder Wiederholung erneut geprüft.

### Versioniertes Ergebnis-Receipt

Unter `job_monitoring.result_receipt` nennt die Capability-Discovery Version, Schema-Pfad und
Antwortfeld des öffentlichen Worker-Ergebnisses. Das vollständige JSON-Schema kann ein Client
mit jedem gültigen Service-Agent-Token abrufen:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $TANKAI_AGENT_TOKEN" \
  "https://TANKAI_HOST/api/v1/job-result-schema"
```

Das Schema `urn:tankai:external-agent-result-receipt:v1` bindet Pflichtfelder, Worker-Zustände,
Phasen, Ausführungsbackend, Commitformat sowie Anzahl und Länge geänderter Repository-Pfade.
Sobald der Runner ein gültiges Ergebnis gespeichert hat, enthält der Jobstatus beispielsweise:

```json
{
  "result_available": true,
  "result_receipt": {
    "version": 1,
    "run_id": "RUN_ID",
    "task_id": "TASK_ID",
    "state": "ready_to_integrate",
    "phase": "complete",
    "branch": "tankai/agent/branch",
    "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "execution_backend": "docker",
    "changed_files": ["tankai/example.py"],
    "implementation_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "started_at": "2026-09-14T06:00:00Z",
    "finished_at": "2026-09-14T06:01:00Z"
  }
}
```

`result_receipt` ist ausdrücklich nullable: Ein noch nicht vorhandenes Ergebnis oder ein intern
beschädigtes beziehungsweise nicht vertragskonformes Run-Objekt liefert `null`. TankAI gibt in
diesem Fall keine ungeprüfte Teilmenge aus. Das separate `result_available` zeigt nur an, ob die
Queue ein internes Ergebnis gespeichert hat. Workspace- und Hostpfade, Befehle, Testausgaben,
Statusmeldungen, Worker-IDs, interne Fehlertexte und sonstige Rohdaten gehören nicht zum
öffentlichen Receipt. Zugriff auf einen konkreten Status bleibt unabhängig vom Schema an
`jobs:read`, die Agenten-Jobfreigabe und die aktuelle Repository-Allowlist gebunden.

### Strukturierte Validierungsfehler

Kann der Submit-Endpunkt den JSON-Body nicht als
`ExternalAgentJobSubmission` validieren, bleibt das bisherige Textfeld `error`
erhalten. Zusätzlich liefert die Antwort einen versionierten, maschinenlesbaren
Block:

```json
{
  "error": "Ungültiger Entwicklungsauftrag",
  "validation": {
    "version": 1,
    "path_format": "json-pointer",
    "error_count": 1,
    "truncated": false,
    "errors": [
      {
        "path": "/pipeline/isolation/network_mode",
        "code": "string_pattern_mismatch"
      }
    ]
  }
}
```

Es werden höchstens 20 Fehler ausgegeben; `error_count` nennt die Gesamtzahl
und `truncated=true` kennzeichnet eine gekürzte Liste. Die Antwort enthält nur
begrenzte JSON-Pointer und stabile Pydantic-Fehlercodes. Eingabewerte,
Pydantic-Meldungen, Fehlerkontexte, URLs und ungewöhnliche frei gewählte
Feldnamen werden nicht gespiegelt. Admission-Fehler aus Scopes,
Repository-Freigaben oder Queue-Richtlinien bleiben davon getrennt und werden
nicht als Schemafehler ausgegeben.

Beispielauftrag:

```json
{
  "repository_id": "REPOSITORY_UUID",
  "idempotency_key": "feature-reset-password-v1",
  "priority": 0,
  "pipeline": {
    "worker": {
      "agent_id": "EXTERNAL_CODER_01",
      "implementation_summary": "Passwort-Reset implementieren",
      "commit_message": "feat: add password reset",
      "implementation_commands": [
        {"argv": ["python", "scripts/implement_reset.py"], "timeout_seconds": 120}
      ],
      "test_commands": [
        {"argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 120}
      ]
    },
    "gates": {
      "reviewer_agent_id": "REVIEWER_01",
      "review_commands": [
        {"argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 120}
      ],
      "qa_agent_id": "QA_01",
      "qa_commands": [
        {"argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 120}
      ]
    },
    "isolation": {
      "image": "tankai-worker@sha256:REPLACE_WITH_APPROVED_DIGEST",
      "memory_mb": 512,
      "cpus": 1,
      "pids_limit": 128,
      "user": "1000:1000"
    }
  }
}
```

Der externe `idempotency_key` wird serverseitig mit der Agenten-ID
namensräumlich getrennt. Derselbe Agent erhält bei identischem Schlüssel und
identischem Payload denselben Job. Ein abweichender Payload mit demselben
Schlüssel wird abgewiesen.

Jede erfolgreiche Submit-Antwort enthält zusätzlich den in den Capabilities
beworbenen Idempotenz-Outcome:

```json
{
  "job": {"job_id": "JOB_UUID"},
  "idempotency": {
    "version": 1,
    "replayed": false
  }
}
```

`replayed=false` bedeutet, dass dieser Aufruf den Job neu eingereiht hat.
`replayed=true` bedeutet, dass die Queue innerhalb derselben Transaktion einen
bereits angenommenen identischen Auftrag gefunden und dessen Job zurückgegeben
hat. Beide Fälle behalten HTTP 202 und dieselbe öffentliche Jobstruktur. Der
Hinweis reserviert nichts zusätzlich, gibt den internen Schlüssel nicht aus und
lockert weder Agenten-, Repository- noch Queue-Grenzen.

Die im Payload übermittelten Worker-, Reviewer-, QA- und Security-Agenten-IDs
werden nicht als globale Identitäten übernommen. Der Server ersetzt sie durch
einen stabilen Namespace aus Service-Agent und Idempotenzschlüssel. Dadurch
kann ein externer Client keine vorhandene interne Agentenidentität übernehmen
oder mit einem parallelen Auftrag kollidieren.

## Sicherheitsvertrag

- Agenten-Tokens werden ausschließlich als SHA-256-Hash gespeichert.
- Tokens laufen nach spätestens 365 Tagen ab und sind sofort widerrufbar.
- Der menschliche Owner muss weiterhin aktiv Mitglied des Workspaces sein.
- Repository-Scopes werden vor jedem Einreichen und Lesen erneut geprüft.
- Agenten sehen nur explizit ihnen zugeordnete Jobs.
- Joblisten-Cursor gelten nur für denselben Agenten und ein aktuell freigegebenes
  Repository; pro Seite werden höchstens 100 Einträge gelesen.
- Der Jobverlauf enthält nur öffentliche Zustände und UTC-Zeit, niemals interne
  Ereignisdetails, Akteur-/Worker-IDs, Fence-Daten oder globale Sequenznummern.
- Pipeline-Befehle, Hostpfade und Token-Geheimnisse erscheinen nicht in
  Job-Listenantworten.
- Erfolgreiche Standard-Worker liefern nur einen gefilterten Receipt mit
  Zuständen, Repository-relativen Dateinamen und Commit-IDs; rohe Callback-
  Ergebnisse und lokale Workspace-Pfade werden nicht über die API ausgegeben.
- Persistierte interne Fehlermeldungen werden extern nur als Fehlerstatus
  angezeigt, weil Runtimefehler lokale Pfade oder Betriebsdetails enthalten
  können. Vollständige Fehler bleiben dem Operator-Audit vorbehalten.
- Inline-Secrets, nicht freigegebene Images und überschrittene Ressourcenbudgets
  werden von der bestehenden Queue abgewiesen.
- Browser-Sessions werden für `/api/v1/*` nicht als Ersatz für Bearer-Tokens
  akzeptiert.

Für einen öffentlichen Mehrmandantenbetrieb bleiben TLS am Edge, ein zentraler
Rate-Limiter, Missbrauchserkennung, ein kurzlebiger Credential-Broker und eine
serverbasierte Multi-Host-Queue zusätzliche Betriebsanforderungen.
