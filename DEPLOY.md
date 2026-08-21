# TankAI 1.4 — sicherer Online-Grundbetrieb

Diese Anleitung beschreibt TankAI hinter einem HTTPS-Reverse-Proxy auf einem Linux-Server. Version 1.4 ergänzt externe monotone Lease-Fences und ein mechanisches Rootless-Runtime-Gate für Development-Worker. Der öffentliche Webdienst und der privilegierte Runner müssen getrennte Prozesse und getrennte Dienstkonten bleiben; deshalb bleibt `production_ready=false`.

## 1. Voraussetzungen

- Linux-Server
- Python 3.11 oder neuer
- Nginx oder Caddy
- TLS-Zertifikat
- getrennte Live-Provider für Hauptmodell und Critic
- Brave- oder Tavily-Suchzugang
- für Development-Worker: dedizierter nicht-root Runner-Host mit rootless Docker oder Podman

## 2. Installation

```bash
sudo useradd -m -s /bin/bash tankai || true
sudo mkdir -p /opt/tankai/.tankai/data
sudo chown -R tankai:tankai /opt/tankai

cd /opt/tankai
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install openai anthropic
```

## 3. Konfiguration

```bash
sudo -u tankai tee /opt/tankai/.env <<'ENV'
TANKAI_LLM=openai
OPENAI_API_KEY=REPLACE_MAIN_KEY
OPENAI_MODEL=REPLACE_MAIN_MODEL

TANKAI_CRITIC_LLM=anthropic
ANTHROPIC_API_KEY=REPLACE_CRITIC_KEY
TANKAI_CRITIC_MODEL=REPLACE_CRITIC_MODEL
TANKAI_REQUIRE_INDEPENDENT_CRITIC=1

TANKAI_SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=REPLACE_SEARCH_KEY
TANKAI_STRICT_WEB_RESEARCH=1
TANKAI_REQUIRE_RESEARCH_EVIDENCE=1
TANKAI_WEB_FETCH=1

TANKAI_HOST=127.0.0.1
TANKAI_PORT=8765
TANKAI_AUTH_MODE=session
TANKAI_DATA_ROOT=/opt/tankai/.tankai/data
TANKAI_AUTH_DB=/opt/tankai/.tankai/data/auth.db
TANKAI_SESSION_HOURS=12
TANKAI_COOKIE_SECURE=1
TANKAI_ALLOW_REGISTRATION=0
TANKAI_LOGIN_ATTEMPTS=5
TANKAI_LOGIN_WINDOW=300
TANKAI_LOGIN_BLOCK=900
TANKAI_EMBEDDER=hashing
ENV

sudo chmod 600 /opt/tankai/.env
sudo chmod 700 /opt/tankai/.tankai /opt/tankai/.tankai/data
sudo chown -R tankai:tankai /opt/tankai
```

`TANKAI_COOKIE_SECURE=1` ist für HTTPS verbindlich. Direkter HTTP-Zugriff auf Port 8765 kann die Secure-Cookie nicht verwenden und ist nur für den Reverse Proxy vorgesehen.

## 4. Ersten Benutzer anlegen

Passwort nicht als Kommandozeilenargument übergeben:

```bash
sudo -u tankai bash -lc "cd /opt/tankai && source .venv/bin/activate && \
  printf '%s\n' 'REPLACE_WITH_A_LONG_PASSWORD' | \
  python -m tankai.web.auth_cli \
    --db /opt/tankai/.tankai/data/auth.db \
    create-user \
    --email admin@example.com \
    --name Administrator \
    --tenant TankAI \
    --workspace Standard \
    --password-stdin"
```

Weitere Verwaltungsbefehle:

```bash
python -m tankai.web.auth_cli --db /opt/tankai/.tankai/data/auth.db list-users
printf '%s\n' 'NEW-LONG-PASSWORD' | python -m tankai.web.auth_cli \
  --db /opt/tankai/.tankai/data/auth.db set-password \
  --email admin@example.com --password-stdin
```

Ein Passwortwechsel widerruft alle Sessions des Nutzers.

## 5. Tests vor dem Start

```bash
sudo -u tankai bash -lc 'cd /opt/tankai && source .venv/bin/activate && python -m compileall -q tankai'
sudo -u tankai bash -lc 'cd /opt/tankai && source .venv/bin/activate && python -m pytest -q'
sudo -u tankai bash -lc 'cd /opt/tankai && source .venv/bin/activate && python -m tankai --selftest'
```

Reale Provider- und Suchaufrufe benötigen zusätzlich einen kontrollierten Smoke-Test mit Kostenlimit.

## 6. Systemd

```ini
[Unit]
Description=TankAI Web Intelligence OS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tankai
Group=tankai
WorkingDirectory=/opt/tankai
EnvironmentFile=/opt/tankai/.env
ExecStart=/opt/tankai/.venv/bin/python -m tankai.web.server
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/tankai/.tankai/data
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
```

## 7. Nginx und HTTPS

```nginx
limit_req_zone $binary_remote_addr zone=tankai_login:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=tankai_run:10m rate=3r/m;

server {
    listen 80;
    server_name tankai.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name tankai.example.com;

    client_max_body_size 220k;

    location = /api/auth/login {
        limit_req zone=tankai_login burst=5 nodelay;
        proxy_pass http://127.0.0.1:8765;
        include proxy_params;
    }

    location = /api/run {
        limit_req zone=tankai_run burst=2 nodelay;
        proxy_pass http://127.0.0.1:8765;
        include proxy_params;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location / {
        proxy_pass http://127.0.0.1:8765;
        include proxy_params;
        proxy_read_timeout 300s;
    }
}
```

Der Anwendungscode wertet keine vom Client übermittelte Nutzer-ID aus. Die aktive Workspace-ID wird aus der serverseitigen Session geladen und gegen die Membership-Tabelle geprüft.

## 8. Docker

```bash
cp .env.example .env
# Live-Provider, Critic und Suche konfigurieren.
docker compose up -d --build

printf '%s\n' 'REPLACE_WITH_A_LONG_PASSWORD' | docker compose exec -T tankai \
  python -m tankai.web.auth_cli --db /app/data/auth.db create-user \
  --email admin@example.com --name Administrator --tenant TankAI \
  --workspace Standard --password-stdin
```

Der Webcontainer besitzt ein read-only Root-Dateisystem, keine Linux-Capabilities und nur `/app/data` sowie `/tmp` sind beschreibbar. **Keinen Docker-/Podman-Socket in diesen Webcontainer mounten.** Development-Worker laufen über einen separat gestarteten Runner.

### 8.1 Cloudflare-Produktion bewusst freigeben

Ein Merge oder Push auf `main` veröffentlicht nicht automatisch. Nach erfolgreichem CI muss ein berechtigter Operator in GitHub Actions den Workflow **Deploy to Cloudflare** auf dem Ref `main` manuell starten und für `confirm_production` ausdrücklich `DEPLOY` wählen. `CANCEL`, ein anderer Ref oder eine fehlende Eingabe überspringt den Deployment-Job.

Der Job bleibt an das GitHub-Environment `production` und die exklusive Concurrency-Gruppe `cloudflare-production` gebunden. Die Cloudflare-Zugangsdaten dürfen ausschließlich als Environment-/Repository-Secrets vorliegen; sie gehören weder in das Repository noch in Workflow-Eingaben oder Receipts.

## 9. Persistente Development-Queue und Admission-Control

Die Queue ist standardmäßig deaktiviert. Für kontrollierte Online-Codeausführung werden Webdienst, Queue-Administration und Runner getrennt betrieben:

```text
Browser → HTTPS-Webdienst → development-jobs.db ← dedizierter Queue-Runner
                             ↑              ↕ fence.db      ↓
                  Auth/Workspace-Prüfung      registrierte Repositories
                                               rootless Docker/Podman
```

Wichtige Trennung:

- Der Webdienst erhält **keinen** Docker-/Podman-Socket und benötigt keine Repository-Mounts.
- Der Queue-Runner benötigt **keinen** Zugriff auf `auth.db`.
- Nur ein administrativer Einrichtungsprozess benötigt gleichzeitig Auth-Datenbank, Queue-Datenbank und Repository-Pfade.
- `development-jobs.db` und die getrennte `development-fences.db` müssen auf lokalem Dateisystem liegen. Beide dürfen nicht dieselbe Datei sein und nicht über NFS geteilt werden.

Webdienst aktivieren:

```dotenv
TANKAI_DEV_QUEUE_ENABLED=1
TANKAI_DEV_QUEUE_DB=/app/data/development-jobs.db
# Runner/Operator-CLI: /srv/tankai/fences/development-fences.db getrennt mounten
TANKAI_REPOSITORY_BASE=/srv/tankai/repositories
TANKAI_WORKTREE_BASE=/srv/tankai/worktrees
TANKAI_STATE_BASE=/srv/tankai/states
```

Die drei Basisverzeichnisse sind im Webprozess logische Allowlist-Werte. Sie müssen dort nicht gemountet sein, müssen aber exakt mit der Runner-Konfiguration übereinstimmen.

### 9.1 Admission-Richtlinie setzen

Zuerst das unveränderliche Worker-Image bauen und dessen Digest ermitteln:

```bash
docker build -f Dockerfile.worker -t tankai-worker:1.6.0 .
docker image inspect tankai-worker:1.6.0 --format '{{.Id}}'
```

Anschließend pro Workspace eine Richtlinie setzen:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --auth-db /srv/tankai/data/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  set-policy \
  --actor-email admin@example.com \
  --workspace-id WORKSPACE_UUID \
  --allowed-image sha256:ACTUAL_64_HEX_IMAGE_ID \
  --max-queued 20 \
  --max-running 2 \
  --max-memory-mb 2048 \
  --max-cpus 4 \
  --max-pids 512 \
  --max-runtime-seconds 3600 \
  --max-attempts 3 \
  --max-jobs-per-user-hour 20
```

Standardmäßig dürfen nur `owner` und `admin` Jobs einreichen. Eine ausdrückliche Freigabe für Mitglieder erfolgt mit zusätzlichem `--submit-role member`; Mitglieder dürfen trotzdem keine erhöhte Queue-Priorität setzen.

### 9.2 Repository registrieren

Hostpfade werden nicht aus Webrequests übernommen. Owner/Admin registrieren sie operatorseitig:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --auth-db /srv/tankai/data/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  register-repository \
  --actor-email admin@example.com \
  --workspace-id WORKSPACE_UUID \
  --name Main \
  --repository /srv/tankai/repositories/WORKSPACE_UUID/main \
  --worktrees /srv/tankai/worktrees/WORKSPACE_UUID/main \
  --state /srv/tankai/states/WORKSPACE_UUID/main.json
```

### 9.3 Queue-Runner starten

Der dauerhafte Runner benötigt keine Auth-Datenbank:

```bash
export TANKAI_REQUIRE_WORKER_ISOLATION=1
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  run-worker \
  --worker-id runner-01 \
  --container-runtime docker \
  --lease-seconds 300 \
  --poll-seconds 2
```

Vor dem Start die Runtime mechanisch prüfen:

```bash
python -m tankai.dev_orchestrator.runtime_cli --container-runtime docker
```

Der Befehl muss `"rootless": true` und `"os_type": "linux"` liefern. Rootful-Runtimes werden für Online-Queue-Worker blockiert.

Der Runner beansprucht Jobs atomar und erhält zusätzlich eine monotone Fence-Epoche aus der getrennten Fence-Datenbank. Heartbeats erneuern Queue-Lease und Fence. Start, Kommandophasen, Commit und Abschluss validieren beide Nachweise; eine neuere Epoche entzieht einem alten Worker sofort die Freigabe für weitere kontrollierte Mutationen.

Operator-Prüfung und bewusstes Recovery:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --auth-db /srv/tankai/data/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  fence-status --actor-email admin@example.com \
  --workspace-id WORKSPACE_UUID --repository-id REPOSITORY_UUID

python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue/development-jobs.db \
  --fence-db /srv/tankai/fences/development-fences.db \
  --auth-db /srv/tankai/data/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees \
  --state-base /srv/tankai/states \
  force-expire-fence --actor-email admin@example.com \
  --workspace-id WORKSPACE_UUID --repository-id REPOSITORY_UUID \
  --expected-epoch 7 --expected-job-id JOB_UUID
```

`force-expire-fence` darf erst ausgeführt werden, nachdem der alte Prozess beziehungsweise dessen Container nachweislich beendet wurde. Die exakte Epoche und Job-ID verhindern, dass versehentlich ein neuerer Worker widerrufen wird.

**Betriebsgrenze:** SQLite-Queue und SQLite-Fence sind weiterhin nur für einen lokalen Single-Host-Betrieb freigegeben. Multi-Host-Runner benötigen einen externen transaktionalen Koordinator.

### 9.4 Web-API

Nach Session-Login und CSRF-Prüfung stehen bereit:

- `GET /api/dev/repositories`
- `GET /api/dev/jobs`
- `POST /api/dev/jobs`
- `POST /api/dev/jobs/<job-uuid>/cancel`

Ein Jobrequest enthält nur `repository_id`, `idempotency_key`, optionale `priority` und einen validierten `pipeline`-Payload. Nutzer-, Mandanten- und Workspace-ID stammen ausschließlich aus der serverseitigen Session.

## 10. Separater Worker-Runner

Der Worker-Runner darf nicht im öffentlich erreichbaren Webprozess laufen. Empfohlene Trennung:

```text
Internet → HTTPS-Reverse-Proxy → TankAI-Webdienst (kein Runtime-Socket)
                              ↘ persistente Job-Queue
                                 dedizierter nicht-root Runner
                                 ↘ rootless Docker/Podman
```

Worker-Image bauen:

```bash
docker build -f Dockerfile.worker -t tankai-worker:1.6.0 .
docker image inspect tankai-worker:1.6.0 --format '{{.Id}}'
```

Die Ausgabe des zweiten Befehls wird als `isolation.image` im Job verwendet. Produktive Jobs akzeptieren standardmäßig unveränderliche `sha256:<64-hex>`-Image-IDs oder Registry-Referenzen im Format `name@sha256:<64-hex>`.

Runner-Konfiguration:

```bash
export TANKAI_REQUIRE_WORKER_ISOLATION=1
export TANKAI_WORKER_CONTAINER_RUNTIME=docker
```

Die Isolationsrichtlinie erzwingt:

- `network=none`,
- read-only Root-Dateisystem,
- keine Linux-Capabilities,
- `no-new-privileges`,
- private IPC,
- feste CPU-, RAM-/Swap-, PID- und Dateideskriptorgrenzen,
- read-only Worktree für Tests und Gates,
- schreibbare Mounts nur für aus `allowed_paths` abgeleitete Implementierungsbereiche; vorhandene exakt erlaubte Dateien werden einzeln gemountet,
- verdeckte `.git`-Metadaten,
- keine Vererbung der Host-Umgebung,
- begrenzte Prozessausgabe und erzwungene Container-Bereinigung bei Timeout.

Beispiel für die Job-Richtlinie:

```json
{
  "isolation": {
    "backend": "docker",
    "image": "registry.example.invalid/tankai-worker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "require_image_digest": true,
    "network_mode": "none",
    "read_only_root": true,
    "memory_mb": 512,
    "cpus": 1.0,
    "pids_limit": 128,
    "tmpfs_mb": 128,
    "build_tmpfs_mb": 512,
    "nofile_limit": 1024,
    "user": "10001:10001"
  }
}
```

Die Beispiel-Domain ist absichtlich nicht routbar. Im realen Job muss das zuvor lokal gebaute oder aus einer kontrollierten Registry geladene Image mit seinem tatsächlichen Digest stehen.

Mehrere bereits genehmigte und konfliktfreie Agenten können über einen Pool-Job ausgeführt werden:

```bash
python -m tankai.dev_orchestrator.cli \
  --state .tankai/project-state.json \
  run-pool \
  --repository /srv/tankai/repositories/WORKSPACE \
  --workspace-root /srv/tankai/worktrees/WORKSPACE \
  --job worker-pool.json \
  --require-container-isolation
```

Der Pool erzeugt keine Agenten und keine Aufgaben. Er akzeptiert nur bereits aktive, exklusiv zugewiesene Programmier-Agenten mit getrennten Schreibbereichen. Die Integration nach `main` erfolgt anschließend weiterhin einzeln über die exklusive Merge-Warteschlange.


### 10.1 Aktiver Lease-/Fence-Abbruch

Der Queue-Runner prüft Heartbeat und externen Fence während jedes laufenden Befehls. Bei Verlust wird die Prozessgruppe beendet. Bei Container-Ausführung wird der benannte Container zusätzlich mit `docker rm -f` beziehungsweise `podman rm -f` entfernt. Für den Produktivbetrieb muss der Runner deshalb Zugriff auf dieselbe rootless Runtime besitzen, über die der Container gestartet wurde.

### 10.2 Verwaiste Worktrees prüfen und bereinigen

Zuerst immer Dry-Run:

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

Erst nach Prüfung mit `--apply` ausführen. Der Reaper blockiert bei aktivem Fence oder aktivem Queue-Job. Schmutzige Worktrees werden nicht gelöscht. Git-Branches bleiben erhalten. Für einen stale nicht-terminalen ProjectState-Run muss die konkrete Run-ID mit `--expected-stale-run-id` bestätigt werden.

## 11. Datentrennung

Die verbindliche Struktur lautet:

```text
DATA_ROOT/
  auth.db
  tenants/<tenant-id>/workspaces/<workspace-id>/
    memory.db
    ltm.db
    vectors.npz
    runs.jsonl
    web_history.jsonl
    cold/
```

Alle Tenant- und Workspace-IDs werden serverseitig aus der Auth-Datenbank bezogen. Direkte Pfadangaben aus Requests werden nicht akzeptiert.

## 12. Backup und Wiederherstellung

Konsistent sichern:

- `auth.db`, `development-jobs.db` und die getrennte `development-fences.db` einschließlich jeweiliger `-wal`/`-shm`, falls Dienste laufen,
- kompletten Ordner `tenants/`,
- Provider-Secrets getrennt und verschlüsselt.

Für einen einfachen konsistenten Dateisystem-Snapshot den Dienst stoppen oder SQLite-Backup-APIs verwenden. `ltm.db` und `vectors.npz` sind weiterhin keine gemeinsame atomare Transaktion.

Alte globale Dateien aus Version 1.0 liegen im Release unter `legacy-global-state/` und werden nicht automatisch einem Mandanten zugeordnet. Eine manuelle Kopie in einen Workspace ist nicht empfohlen, weil IDs und Provenance geprüft werden müssen.

## 13. Offene Betriebsrisiken

- Die Container-Isolation ist implementiert, aber in diesem Release nicht gegen eine reale Docker-/Podman-Runtime im CI ausgeführt worden.
- SQLite-Queue und separate Fence-Datenbank sind auf einen einzelnen Host und lokales Dateisystem begrenzt; Multi-Host-Runner benötigen einen externen transaktionalen Koordinator.
- Ein Fence-Verlust beendet laufende lokale Prozessgruppen aktiv. Bei Container-Ausführung wird zusätzlich der eindeutig benannte Container entfernt; die reale Daemon-/Kernel-Durchsetzung muss auf dem Zielhost dennoch per End-to-End-Test bestätigt werden.
- Rootless-Betrieb wird mechanisch geprüft; Image-Signaturprüfung und Registry-Allowlist müssen weiterhin im Zielsystem eingerichtet werden.
- Login-Limits in der Anwendung sind prozesslokal; der Reverse Proxy muss zusätzlich limitieren.
- Es fehlen persistente Nutzerquoten, Kostenbudgets und Abrechnung.
- Audit-Ereignisse sind persistent, aber nicht extern manipulationsgeschützt.
- Es fehlen zentrale Metriken, Alarmierung und verteiltes Tracing.
- Keine E-Mail-Verifikation, Passwort-Reset-Mail oder MFA.
- Reale Provider-/Search-E2E-Tests wurden ohne Testkonten nicht automatisiert.

Deshalb bleibt `production_ready=false`, obwohl authentifizierter Onlinebetrieb für kontrollierte Nutzer technisch möglich ist.
## 14. Verwaiste Container kontrolliert bereinigen

Der Container-Reaper ist standardmäßig ein Dry-Run und benötigt Owner-/Admin-Rechte sowie eine rootless Linux-Runtime:

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

Für tatsächlich abgeschlossene Jobs ergänzt der Betreiber `--apply`. Bei einem unbekannten oder im Queue-State noch nicht terminalen stale Job sind zusätzlich beide Werte zwingend:

```text
--expected-stale-job-id JOB_ID
--expected-fence-epoch EPOCH
```

Container eines aktiven Queue-Leases oder des aktuellen Repository-Fence werden unabhängig von `--apply` geschützt. Container mit fremder Mandanten-, Workspace- oder Repository-Bindung werden übersprungen.
