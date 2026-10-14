# TankAI auf einem neuen Hetzner-Server

`scripts/install-hetzner.sh` überträgt den festgelegten Core-Stand
`f39f1a010db372e49ebb392173352828e885798d` nach `/opt/tankai` und richtet
einen automatisch startenden systemd-Dienst ein. Unterstützt werden Debian
und Ubuntu mit Python 3.11 oder neuer. In der als `root` angemeldeten
Serverkonsole starten; bestehende TankAI-Verzeichnisse, Dienstkonten oder
Dienste führen zum Abbruch und werden nicht überschrieben.

Der Installer installiert die drei Python-Basisabhängigkeiten aus
`requirements.txt` innerhalb eines eigenen virtuellen Environments.
Der Webdienst läuft als nicht privilegiertes Dienstkonto und darf nur sein
Datenverzeichnis verändern. Der abschließende Check verlangt einen aktiven
Dienst, einen erfolgreichen HTTP-Healthcheck, Anmeldepflicht und deaktivierte
öffentliche Selbstregistrierung. Das neue Dienstkonto hat keine Login-Shell.

## Nach der Grundinstallation

Dies ist noch keine öffentlich erreichbare oder vollständig konfigurierte
KI: Der Dienst bindet ausschließlich an `127.0.0.1:8765`, die Modelle bleiben
im Simulationsmodus, der Development-Worker bleibt ausgeschaltet, und der
Installer legt weder einen App-Benutzer noch neue SSH-Zugangsdaten an. Er
ändert keine Firewall-Regeln und startet keine kostenpflichtigen Provider.

1. Den ersten App-Benutzer mit der interaktiven `auth_cli` anlegen. Das
   Passwort wird direkt in der Serverkonsole gewählt und nicht als
   Kommandozeilenargument oder im Chat angegeben:

   ```bash
   cd /opt/tankai
   runuser -u tankai -- .venv/bin/python -m tankai.web.auth_cli \
     --db /opt/tankai/.tankai/data/auth.db create-user \
     --email DEINE_LOGIN_EMAIL --name Administrator \
     --tenant TankAI --workspace Standard
   ```

   Das App-Passwort muss mindestens zwölf Zeichen lang sein. Der CLI fragt
   das Passwort und seine Bestätigung verdeckt ab.

2. Einen HTTPS-Reverse-Proxy und die gewählte Domäne nach `DEPLOY.md`
   einrichten. Die Konfiguration verwendet bereits sichere Session-Cookies.
3. Die gewünschten Modelle, den unabhängigen Critic und die Webrecherche
   konfigurieren und testen. Kostenpflichtige Live-Provider erst nach einer
   ausdrücklichen Auswahl und einem festgelegten Kostenlimit aktivieren.

Status und Diagnose:

```bash
systemctl status tankai --no-pager
curl --fail http://127.0.0.1:8765/api/health
journalctl -u tankai -n 30 --no-pager
```

Eine laufende lokale Health-Route bestätigt den Grunddienst. Sie bestätigt
keine Erreichbarkeit vom Handy, keine erfolgreichen Modellaufrufe und keine
vollständige Produktionsreife. Diese Grenzen bleiben entsprechend der
vorhandenen TankAI-Architektur bestehen.
