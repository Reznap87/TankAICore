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

Unter **Modelle → Chat-Modelle → Fallback-Ketten** eine leere Kette `tankai`
anlegen, vorübergehend aktivieren und nur für allgemeine Chat-Aufgaben geeignete
Modelle hinzufügen. **Änderungen speichern** anklicken und die Zuordnung nach
einem Neuladen prüfen. Die Umschalter allein speichern noch nicht. Danach kann
die bisherige Standardkette wieder aktiviert
werden: TankAI ruft ausdrücklich `auto:tankai` auf. Reine Moderationsmodelle
(etwa Qwen3Guard) gehören nicht in diese Kette. Modelle, die im gewählten
Tokenrahmen nur Zwischentexte statt vollständigem JSON liefern, ebenfalls
zunächst ausschließen. Die automatische HTTP-Erfolgsbewertung des Routers
belegt keine Kompatibilität mit Planner und Critic.

Im privaten OVH-Versuch wurden Llama 3.3 70B, Mistral Small 3.2 24B,
Qwen2.5 VL 72B und Mistral Nemo ausgewählt. Verfügbarkeit und Eignung am
eigenen Zielhost prüfen; diese Auswahl ist keine allgemeine Qualitätszusage.

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

Der Versuch begrenzt TankAI auf 20 Adapteraufrufe und 1024 Ausgabetokens je
Aufruf, ohne SDK-Retries. Websuche und Development-Queue bleiben aus.
Sieben ist nur das kleinste vom Workspace-Start akzeptierte Aufrufbudget.
Der normale Ablauf braucht bei fünf Planschritten bereits 14 Aufrufe vor der
optionalen Memory-Verarbeitung. Die Testgrenze lässt dafür Platz; sie bleibt
eine harte Obergrenze und garantiert keinen Abschluss aller Reparaturversuche.
1024 Ausgabetokens geben den strukturierten Planner- und Critic-Antworten mehr
Platz. Ein abgebrochener oder abgelehnter Lauf gilt weiterhin nicht als Erfolg.
Schlägt erst die zusätzliche Memory-Aufbereitung nach dem gespeicherten
Ergebnis fehl (zum Beispiel wegen eines Ratenlimits), bleibt die ursprüngliche
Antwort samt Prüfstatus erhalten. Ein fehlgeschlagenes Receipt
`defer_memory_consolidation` weist die ausstehende Aufbereitung aus. Neue
Aufbereitungen werden zusätzlich dauerhaft in der Workspace-Datenbank als
`consolidation_jobs` vorgemerkt. Das aktiviert keinen automatischen Modell-Retry.
`LongTermMemory.pending_consolidations()` liest diesen Arbeitsstand ohne
Modellaufruf. Ein expliziter erneuter Aufruf von `consolidate(run_id, llm=...)`
kann ihn fortsetzen; der Betreiber muss dafür denselben Budget-, Kosten- und
Zugriffsschutz wie beim ursprünglichen Lauf bereitstellen. Ein Hintergrunddienst
wird dadurch nicht aktiviert.

Bereits strukturierte, vollständig validierte Einträge werden vor der
Indexierung zwischengespeichert. `consolidate(run_id, prepared_only=True)` kann
diesen Schritt ohne Modell fortsetzen; ohne vorbereiteten Stand bricht es ab.
Dieser Modus setzt ausdrücklich den lokalen `HashingEmbedder` voraus. Andere
oder unbekannte Embedder werden abgewiesen, damit auch die Indexierung keine
Provider-Anfrage oder zusätzlichen Kosten auslösen kann.
Ein Retry schaltet nicht still auf eine heuristische Aufbereitung um. Stabile
Eintrags-IDs und eine gemeinsame SQL-Transaktion verhindern doppelte oder
teilweise freigegebene Einträge. Eine fünfminütige Claim-Lease verhindert die
doppelte Bearbeitung desselben Auftrags; ein nach Prozessabbruch verwaister
Claim kann danach explizit neu übernommen werden. Workspace-Zugriffe bleiben
wie bisher serialisiert; dies ist kein allgemeiner Mehrprozess-VectorStore.

Nur abgeschlossene, geprüfte und freigegebene Live-Läufe sind für neue
Wissenseinträge geeignet. Bei Altdaten ohne gespeicherten Prüfstatus wird dieser
nicht nachträglich angenommen. Historische ausstehende Hinweise werden nicht
automatisch zu neuen Aufträgen umgedeutet. Ungültiges Modell-JSON wird als Fehler
vorgemerkt und nicht als Rohtext-Wissen gespeichert. Die Strukturprüfung ersetzt
keine unabhängige inhaltliche Prüfung der extrahierten Aussagen.
Der Chat zeigt die ausstehende Wissensaufbereitung direkt bei der Antwort und
erneut beim Öffnen aus dem Verlauf an. Der Gesprächsexport enthält denselben
Hinweis.

Neue Chatantworten und Verlaufseinträge enthalten ihre stabile `goal_id`. Bei
ausstehender Wissensaufbereitung lädt die Oberfläche den aktuellen Zustand aus
dem angemeldeten Workspace. Nur bei vorhandenem vorbereitetem Stand und lokalem
Index erscheint **Wissen fertig speichern**. Der Knopf ruft ausschließlich
`prepared_only=True` auf. Ein noch nötiger Modellversuch wird erklärt und nicht
gestartet. Ältere Einträge ohne Laufkennung zeigen einen entsprechenden Hinweis.
Der nachträgliche Status ersetzt keine historischen Receipts oder Prüfverdicts;
die aktuelle Anzeige und der Export berücksichtigen ihn auch nach Neuladen.

Die Statusschnittstelle `GET /api/memory/consolidation/{goal_id}` benötigt eine
Session. `POST /api/memory/consolidation/{goal_id}/resume` benötigt zusätzlich
den vorhandenen CSRF-Schutz und ein leeres JSON-Objekt. Die Laufkennung wird nur
innerhalb des aktiven, serverseitig bestimmten Workspaces aufgelöst. Andere
Workspaces erhalten dieselbe 404-Antwort wie bei einem unbekannten Lauf. Beide
Operationen verwenden die vorhandene Workspace-Sperre. Der Status enthält
weder Wissensinhalte noch interne Fehlertexte. Ein erfolgreicher Speicherversuch
wird separat auditiert; wiederholtes Speichern eines abgeschlossenen Auftrags
legt keine neuen Einträge an.

Quellenmarkierungen werden auch bei Aufgaben ohne Recherche geprüft. Eine
erfundene oder fehlerhafte Quellen-ID verhindert die Freigabe, selbst wenn der
Modell-Critic zustimmt. Der Antworttext bleibt zur Nachvollziehbarkeit erhalten.
Die Synthese erhält nur die bestätigten IDs aus den aktuellen Research-Receipts;
ohne solche Quellen verlangt sie keinen Quellenabschnitt. Ältere gespeicherte
Antworten mit unbestätigten Markierungen werden in der Oberfläche entsprechend
gekennzeichnet. Ihre historischen Datensätze und Receipts werden nicht
umgeschrieben; der Export unterscheidet gespeicherten Laufstatus und aktuelle
Ergebnisanzeige. Das ist eine Prüfung der Quellen-IDs, kein Beweis für die
inhaltliche Richtigkeit aller Aussagen oder die Unabhängigkeit des Kritikers.
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
