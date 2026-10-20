#!/usr/bin/env bash
# Install the reviewed TankAI source on a new Debian/Ubuntu server.
# This starts a private, authenticated setup instance; see HETZNER.md.
set -Eeuo pipefail
umask 027

readonly SOURCE_URL='https://github.com/Reznap87/TankAICore.git'
readonly SOURCE_COMMIT='f39f1a010db372e49ebb392173352828e885798d'
readonly INSTALL_DIR='/opt/tankai'
readonly DATA_DIR='/opt/tankai/.tankai/data'
readonly SERVICE_USER='tankai'
readonly SERVICE_NAME='tankai.service'

die() { printf 'Abbruch: %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == '--help' ]]; then
    cat <<'HELP'
TankAI auf einem neuen Debian-/Ubuntu-Server installieren.
Aufruf: bash install-hetzner.sh

Benötigt root und systemd. Bereits vorhandene TankAI-Installationen
werden nicht überschrieben. Startet nur auf 127.0.0.1:8765, mit
Anmeldepflicht und ausgeschalteter öffentlicher Registrierung.
Modellzugänge, Webrecherche, HTTPS und erster App-Benutzer sind
anschließend separat einzurichten. Es werden keine API-Käufe getätigt.
HELP
    exit 0
fi
[[ $# == 0 ]] || die 'Unbekannte Option; --help zeigt die Anleitung.'
[[ $EUID == 0 ]] || die 'Bitte in der als root angemeldeten Serverkonsole starten.'
[[ -r /etc/os-release ]] || die '/etc/os-release fehlt.'
# shellcheck source=/dev/null
. /etc/os-release
case "${ID:-}" in
    debian|ubuntu) ;;
    *) die 'Dieses Skript unterstützt Debian und Ubuntu.' ;;
esac
[[ -d /run/systemd/system ]] || die 'systemd muss auf diesem Server laufen.'
command -v apt-get >/dev/null || die 'apt-get fehlt.'
command -v systemctl >/dev/null || die 'systemctl fehlt.'
[[ ! -e "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]] || die "$INSTALL_DIR existiert bereits; bitte zuerst den vorhandenen Stand prüfen."
if id "$SERVICE_USER" >/dev/null 2>&1; then
    die "Das Dienstkonto $SERVICE_USER existiert bereits; bitte zuerst den vorhandenen Stand prüfen."
fi
if getent group "$SERVICE_USER" >/dev/null; then
    die "Die Gruppe $SERVICE_USER existiert bereits; bitte zuerst den vorhandenen Stand prüfen."
fi
if systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
    die 'Ein TankAI-Dienst existiert bereits; bitte zuerst den vorhandenen Stand prüfen.'
fi

printf '1/5: Systempakete installieren\n'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv
python3 - <<'PY'
import socket
import sys
if sys.version_info < (3, 11):
    raise SystemExit('Abbruch: Python 3.11 oder neuer erforderlich.')
with socket.socket() as sock:
    try:
        sock.bind(('127.0.0.1', 8765))
    except OSError as exc:
        raise SystemExit(f'Abbruch: Der lokale Port 8765 ist nicht frei: {exc}')
PY

printf '2/5: Festgelegten TankAI-Code übertragen\n'
install -d -m 0750 "$INSTALL_DIR"
git -C "$INSTALL_DIR" init --quiet
git -C "$INSTALL_DIR" remote add origin "$SOURCE_URL"
git -C "$INSTALL_DIR" fetch --quiet --depth=1 origin "$SOURCE_COMMIT"
git -C "$INSTALL_DIR" checkout --quiet --detach FETCH_HEAD
[[ "$(git -C "$INSTALL_DIR" rev-parse HEAD)" == "$SOURCE_COMMIT" ]] || die 'Der geladene Code stimmt nicht mit dem festgelegten Stand überein.'
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/.venv/bin/python" -m compileall -q "$INSTALL_DIR/tankai"

printf '3/5: Privates Dienstkonto und Konfiguration anlegen\n'
useradd --system --user-group --home-dir "$INSTALL_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR"
chmod -R o-rwx "$INSTALL_DIR"
chmod 0700 "$INSTALL_DIR/.git"
install -d -o root -g "$SERVICE_USER" -m 0750 "$INSTALL_DIR/.tankai"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR"
cat > "$INSTALL_DIR/.env" <<'ENV'
TANKAI_LLM=mock
TANKAI_LIVE_PROVIDER_ENABLED=0
TANKAI_REQUIRE_INDEPENDENT_CRITIC=1
TANKAI_REQUIRE_RESEARCH_EVIDENCE=1
TANKAI_HOST=127.0.0.1
TANKAI_PORT=8765
TANKAI_AUTH_MODE=session
TANKAI_DATA_ROOT=/opt/tankai/.tankai/data
TANKAI_AUTH_DB=/opt/tankai/.tankai/data/auth.db
TANKAI_COOKIE_SECURE=1
TANKAI_ALLOW_REGISTRATION=0
TANKAI_SESSION_HOURS=12
TANKAI_LOGIN_ATTEMPTS=5
TANKAI_LOGIN_WINDOW=300
TANKAI_LOGIN_BLOCK=900
TANKAI_DEV_QUEUE_ENABLED=0
TANKAI_REQUIRE_WORKER_ISOLATION=1
ENV
chown root:"$SERVICE_USER" "$INSTALL_DIR/.env"
chmod 0640 "$INSTALL_DIR/.env"

cat > /etc/systemd/system/tankai.service <<'UNIT'
[Unit]
Description=TankAI authenticated web service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tankai
Group=tankai
WorkingDirectory=/opt/tankai
EnvironmentFile=/opt/tankai/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/tankai/.venv/bin/python -m tankai.web.server
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/tankai/.tankai/data
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 /etc/systemd/system/tankai.service

printf '4/5: Automatischen Dienststart aktivieren\n'
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

printf '5/5: Laufenden Dienst prüfen\n'
ready=0
for _ in {1..15}; do
    if systemctl is-active --quiet "$SERVICE_NAME" && \
       curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8765/api/health 2>/dev/null | \
       python3 -c 'import json,sys; p=json.load(sys.stdin); sys.exit(0 if p.get("ok") is True and p.get("auth_mode")=="session" and p.get("auth_required") is True and p.get("registration_enabled") is False else 1)' 2>/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
if [[ $ready != 1 ]]; then
    systemctl status --no-pager --lines=15 "$SERVICE_NAME" || true
    die 'Die Dateien wurden übertragen, aber der Dienstcheck ist fehlgeschlagen. Bitte die Fehlermeldung prüfen.'
fi
printf '%s\n' "$SOURCE_COMMIT" > "$INSTALL_DIR/.tankai-installed-commit"
chmod 0640 "$INSTALL_DIR/.tankai-installed-commit"
chown root:"$SERVICE_USER" "$INSTALL_DIR/.tankai-installed-commit"

cat <<'DONE'

TankAI-Code übertragen. Dienst läuft und startet nach einem Neustart automatisch.
Status: systemctl status tankai --no-pager

Dies ist die private Grundinstallation. App-Benutzer, HTTPS/Domäne und
echte Modelle/Webrecherche sind noch einzurichten. Aktuell läuft der
Simulationsmodus. Der öffentliche Zugriff ist noch nicht aktiviert.
DONE
