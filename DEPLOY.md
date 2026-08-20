# TankAI – Deploy-Anleitung

Ziel: **öffentlich erreichbare Demo** mit HTTPS, optional Basic-Auth und echtem LLM.
Das ist kein fertiges „bestes KI-Produkt“, sondern ein **betriebsfähiger Multi-Agenten-Kern**, den man iterativ verbessern kann.

## 1. Voraussetzungen (Server)

- Linux VPS (Ubuntu 22.04/24.04 o.ä.)
- Python 3.11+
- Domain + DNS A-Record auf die Server-IP
- Optional: Docker

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx
```

## 2. App einrichten

```bash
sudo useradd -m -s /bin/bash tankai || true
sudo mkdir -p /opt/tankai
sudo chown tankai:tankai /opt/tankai

# Code nach /opt/tankai kopieren (git clone / scp / rsync)
cd /opt/tankai
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install pydantic rich numpy
# echtes LLM:
pip install openai
# optional:
# pip install anthropic python-dotenv
```

## 3. Umgebungsvariablen

```bash
sudo -u tankai tee /opt/tankai/.env <<'ENV'
TANKAI_LLM=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
TANKAI_HOST=127.0.0.1
TANKAI_PORT=8765
TANKAI_BASIC_AUTH_USER=admin
TANKAI_BASIC_AUTH_PASS=CHANGE_ME_STRONG
TANKAI_RUN_STORE=/opt/tankai/data/runs.jsonl
ENV
mkdir -p /opt/tankai/data
chown -R tankai:tankai /opt/tankai
```

**Ohne API-Key bleibt der Mock aktiv** – für eine echte Demo ist `OPENAI_API_KEY` Pflicht.

## 4. Systemd-Service

```bash
sudo tee /etc/systemd/system/tankai.service <<'UNIT'
[Unit]
Description=TankAI Web Intelligence OS
After=network.target

[Service]
Type=simple
User=tankai
Group=tankai
WorkingDirectory=/opt/tankai
EnvironmentFile=/opt/tankai/.env
ExecStart=/opt/tankai/.venv/bin/python -m tankai.web.server
Restart=on-failure
RestartSec=5
# Sicherheit
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now tankai
sudo systemctl status tankai
```

## 5. Nginx + HTTPS

```bash
sudo tee /etc/nginx/sites-available/tankai <<'NGX'
server {
    listen 80;
    server_name tankai.example.com;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
NGX

sudo ln -sf /etc/nginx/sites-available/tankai /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d tankai.example.com
```

## 6. Docker (Alternative)

```bash
cd /opt/tankai
docker compose up -d --build
```

Siehe `docker-compose.yml`. HTTPS weiterhin über Nginx/Caddy auf dem Host oder Traefik.

## 7. Smoke-Test nach Deploy

```bash
curl -s https://tankai.example.com/api/health
# mit Basic-Auth:
curl -s -u admin:PASS https://tankai.example.com/api/health
```

Erwartet: `{"ok": true, ...}`

## 8. Sicherheits-Checkliste (Minimum)

- [ ] Starke `TANKAI_BASIC_AUTH_PASS`
- [ ] HTTPS (Let's Encrypt)
- [ ] App lauscht nur auf `127.0.0.1`, öffentlich nur Nginx
- [ ] API-Keys nur in `.env`, nie im Git
- [ ] Firewall: nur 80/443 offen (`ufw allow 80,443/tcp`)
- [ ] Rate-Limits / Budget-Alerts beim LLM-Anbieter
- [ ] Regelmäßige Backups von `data/`

## 9. Was „beste KI“ hier bedeutet

TankAI ist **kein neues Frontier-Modell**. Stärke kommt aus:

1. **Zielkontrolle** (Definition of Done)
2. **Routing + Spezialisten**
3. **Critic / Receipts**
4. **Langzeitgedächtnis + Procedural Patterns**
5. **Echtes starkes Backend-LLM** (GPT/Claude/…)

Online gehen = dieser Stack **zuverlässig betrieben** + mit dem besten verfügbaren Modell gefüttert + Feedback-Schleife (Evals, Consolidation, Retention).

## 10. Nächste Qualitätsstufen nach Go-Live

1. Golden-Set an Testzielen + automatischer Self-Test gegen echtes LLM  
2. Bessere Embeddings (`sentence-transformers` oder OpenAI Embeddings)  
3. User-Accounts statt nur Basic-Auth  
4. Observability (Request-Logs, Latenz, Token-Kosten)  
5. Canary: neues Prompt/Routing nur für % der Requests  
