# Privater TankAI-Test mit FreeLLMAPI

Dies ist ein separater Einbenutzer-Test auf einem Docker-Host. Er veröffentlicht
keinen Router und ändert weder Cloudflare noch tankaicore.com. Die Software ist
vorbereitet; ein laufender Container und echte Antworten müssen am Zielhost
noch nachgewiesen werden. Docker und Docker Compose v2 sind erforderlich.

Der bestehende OpenAI-kompatible TankAI-Adapter wird wiederverwendet. Der Name
`TANKAI_LLM=openai` bezeichnet hier das Protokoll; die Zieladresse ist ausschließlich
`http://freellmapi:3001/v1`. Es wird kein OpenAI-Plattformschlüssel benötigt.

## 1. Router starten

Im Repository, auf dem vorgesehenen privaten Host:

```bash
python3 scripts/freellmapi_test.py init
# Nur falls noch keine .env existiert; vorhandene Dateien bleiben erhalten:
test -e .env || cp .env.example .env
docker compose -p tankai-freellm-test --env-file .tankai/freellmapi.env \
  -f docker-compose.yml -f docker-compose.freellmapi.yml up -d freellmapi
```

Die generierte Datei `.tankai/freellmapi.env` ist gitignoriert und nur für den
Besitzer lesbar. Sie enthält den Verschlüsselungsschlüssel; niemals ausgeben,
committen oder bei Updates neu erzeugen. Datenbankvolume und Schlüssel gemeinsam
geschützt sichern. `init` erhält einen vorhandenen gültigen Schlüssel.

Das Image ist auf Manifest-Digest
`sha256:c3b92211cc0ada65e6044e8a17d3878ca27f20af8ca74cfdc4a4d51aa3f6ac63`
festgelegt, gebaut aus Upstream-Commit
`19eb7e1c3bd5e7421ee72c3878a099f511ea4275`.
Nachweis: [Upstream Docker Run](https://github.com/tashfeenahmed/freellmapi/actions/runs/39425845703).
Keine automatischen Containerupdates, kein Docker-Socket im Router.

Der Router startet direkt als Image-Benutzer `node`, weiterhin mit `cap_drop: ALL`
und `no-new-privileges`. Der Root-Start des Images würde `setpriv` zum Wechseln
von Benutzer/Gruppen benötigen; mit vollständig entzogenen Capabilities kann
dieser Schritt mit Exit-Code 127 abbrechen. Neue benannte Volumes übernehmen das
im Image bereits für `node` vorbereitete Datenverzeichnis. Ein fremdes oder
bereits Root gehörendes Volume wird nicht automatisch umgeschrieben.

Bei einem nicht startenden bestehenden Container zunächst dessen konkrete
Startfehlermeldung und Volume-Besitzer prüfen. Den Verschlüsselungsschlüssel und
die Daten erhalten; weder `down -v` noch eine erneute Schlüsselerzeugung ausführen.
Die lokale Konfiguration lässt sich ohne Docker-Engine prüfen:

```bash
python3 scripts/test_freellmapi_startup.py --config-only
```

Ohne `--config-only` prüft das Skript das echte gepinnte Image in einem
wegwerfbaren Container: Benutzer ist nicht Root, effektive Capabilities sind
leer und `/api/ping` antwortet. Der Container hat kein Netzwerk, keinen Hostport,
keine Anbieterzugänge und kein persistentes Volume. Startlogs werden nicht
ausgegeben, weil sie einen Setup-Code enthalten können. Der Test löst keine
Inferenz aus und ersetzt nicht die Prüfung des vorhandenen Hetzner-Containers.

## 2. Eigenen Anbieterzugang hinzufügen

Auf demselben Rechner `http://127.0.0.1:3001` öffnen, das erste private
Dashboardkonto anlegen und unter **Keys** mindestens einen eigenen,
für diesen Test geeigneten Gratis-Anbieterzugang hinzufügen. Keine kostenpflichtigen
Kontingente oder automatischen Aufladungen für diesen Versuch aktivieren.
Providerbedingungen und tatsächlich verbleibende Kontingente gelten weiter.

Bei einem entfernten Host den bestehenden SSH-Zugang für einen lokalen Tunnel
nutzen; Port 3001 bleibt auf Loopback. Die HTTP-Seite niemals öffentlich freigeben.
Falls das Dashboard beim Erstkonto einen Setup-Code verlangt, diesen privat am
Host aus den Router-Startlogs lesen. Keine Zugangsdaten in Chat oder Tickets senden.

Den **unified API key** von FreeLLMAPI im verdeckten Terminaldialog speichern:

```bash
python3 scripts/freellmapi_test.py connect
python3 scripts/freellmapi_test.py check
```

`check` prüft nur den authentifizierten Modellkatalog. Virtuelle Einträge wie
`auto` zählen nicht als verfügbare Modelle. Eine verfügbare Katalogzeile allein
beweist noch keine erfolgreiche Inferenz. Anbieter-Gesundheitsprüfungen des
Routers können eigenes Kontingent verbrauchen; die periodische Prüfung ist hier
auf einmal täglich reduziert, nicht als kostenfrei garantiert.

## 3. Begrenzte echte Antwort und TankAI testen

Dieser Befehl fordert bewusst eine echte Modellantwort an:

```bash
python3 scripts/freellmapi_test.py smoke
docker compose -p tankai-freellm-test --env-file .tankai/freellmapi.env \
  -f docker-compose.yml -f docker-compose.freellmapi.yml up -d --build tankai
```

`smoke` sendet einen kurzen festen Prompt mit höchstens 32 Ausgabetokens an
`auto` und meldet Antwortstatus und geroutetes Modell, keine Schlüssel.
Es gibt einen Gateway-Aufruf; FreeLLMAPI kann intern mehrere Anbieter versuchen.
Das ist keine harte Obergrenze für die Anzahl der Upstream-Aufrufe oder Geldkosten.

TankAI ist unter `http://127.0.0.1:8765` erreichbar. Der separate Compose-Projektname
legt einen eigenen TankAI-Datenspeicher an; bestehende Livekonten werden nicht
kopiert. Den ersten lokalen Owner über die vorhandene Auth-CLI einrichten:

```bash
docker compose -p tankai-freellm-test --env-file .tankai/freellmapi.env \
  -f docker-compose.yml -f docker-compose.freellmapi.yml exec tankai \
  python -m tankai.web.auth_cli create-user --help
```

Dann mit der dort dokumentierten interaktiven Passworteingabe ein Testkonto
anlegen. Anmeldung und einen kurzen, quellenfreien Textauftrag im Chat prüfen.
Ein Gateway-Smoke allein bestätigt nicht den gesamten TankAI-Chatablauf.

Der Versuch begrenzt TankAI auf sieben Adapteraufrufe und 256 Ausgabetokens je
Aufruf, ohne SDK-Retries. Websuche und Development-Queue bleiben aus.
Sieben ist das kleinste vom Workspace-Start akzeptierte gemeinsame Aufrufbudget;
ein kleinerer Wert verhindert bereits den angemeldeten Health- und Chat-Aufruf.
Hauptmodell und Critic nutzen denselben Router; ihre Unabhängigkeit ist damit
**nicht nachgewiesen**. Der Test ist keine Produktionsfreigabe und keine
Qualitätsabnahme. Keine öffentlichen Nutzer an diesen Test anschließen.

## Stoppen

```bash
docker compose -p tankai-freellm-test --env-file .tankai/freellmapi.env \
  -f docker-compose.yml -f docker-compose.freellmapi.yml down
```

Volumes bleiben erhalten; nicht `down -v` verwenden, wenn die Daten bleiben sollen.
Das bestehende Cloudflare-Deployment wird von diesen Befehlen nicht verändert.

Upstream-Anleitungen:
[Docker](https://github.com/tashfeenahmed/freellmapi/blob/19eb7e1c3bd5e7421ee72c3878a099f511ea4275/docs/en/deployment/01-docker.md),
[API](https://github.com/tashfeenahmed/freellmapi/blob/19eb7e1c3bd5e7421ee72c3878a099f511ea4275/docs/en/api/01-rest-api.md).
