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

## Machine-to-Machine-Endpunkte

Alle v1-Endpunkte benötigen:

```http
Authorization: Bearer tkai_v1_REDACTED
```

| Methode | Pfad | Scope |
|---|---|---|
| `GET` | `/api/v1/capabilities` | gültiger Token |
| `GET` | `/api/v1/job-schema` | gültiger Token |
| `GET` | `/api/v1/repositories` | `repositories:read` |
| `GET` | `/api/v1/jobs` | `jobs:read` |
| `GET` | `/api/v1/jobs/{job_id}` | `jobs:read` |
| `POST` | `/api/v1/jobs` | `jobs:submit` |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | `jobs:cancel` |

`GET /api/v1/capabilities` nennt unter `job_submission` den Submit-Pfad, die
HTTP-Methode, Pfad und Version des zugehörigen Schemas sowie Version, Pfadformat
und Obergrenze strukturierter Validierungsfehler. Ein Client kann danach den
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
