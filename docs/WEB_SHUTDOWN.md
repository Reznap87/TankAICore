# Webaufträge vor dem Schließen der Speicher abwarten

Der Webserver darf die Anwendung erst schließen, wenn seine bereits zugelassenen
HTTP-Worker beendet sind. Zuvor schloss `ThreadedHTTPServer.server_close()` zuerst
die Anwendung, obwohl `ThreadingMixIn` weiterhin aktive Daemon-Threads ausführen
konnte. Zwei Regressionstests reproduzieren den vorzeitigen Speicherabschluss
und den fehlenden begrenzten Drain-Vertrag.

## Ablauf

- `begin_draining()` stoppt die Annahme neuer Arbeit in dieser Serverinstanz.
- Neue vollständige normale HTTP-Anfragen erhalten HTTP 503 mit
  `error_code=server_draining`, `Retry-After: 1` und `Cache-Control: no-store`.
  Sie durchlaufen keine Anmeldung, Kontosicherung oder Modellroute. Unvollständige
  beziehungsweise übergroße Header können stattdessen zur Verbindungstrennung
  führen. Die Ablehnung liest höchstens 16 KiB Header mit einer absoluten
  Ein-Sekunden-Frist und begrenzt auch den Antwortversand.
- Bereits gestartete Aufträge dürfen ihre bestehenden Berechtigungs-, Quellen-,
  Budget- und Versandprüfungen weiter durchlaufen. Es gibt durch den Stopp keine
  zusätzlichen Modellversuche, automatische Abrechnung oder neue Freigabe.
  Eine HTTP-Anfrage, die lediglich auf die Workspace-Lease wartet, wird vor dem
  Auftragsstart erneut geprüft und bei inzwischen aktivem Drain mit 503
  abgewiesen. Sie darf nach Freigabe der Lease keinen neuen Modellauftrag starten.
- `drain(timeout=...)` wartet auf alle zuvor registrierten HTTP-Worker. Die
  Registrierung erfolgt vor deren Threadstart. Ein erfolgreicher Aufruf liefert
  `quiesced=true` und `scope=this-server-only`, schließt aber noch keinen Speicher.
- `server_close()` schließt den Listener, wartet standardmäßig höchstens
  130 Sekunden auf die Worker und schließt erst danach die Anwendung. Bei
  `ServerDrainTimeout` bleiben die Anwendungsspeicher geöffnet. Nach Abschluss
  der Arbeit kann der Aufrufer den Abschluss erneut versuchen.

Ein HTTP-Worker darf seinen eigenen Server weder drainen noch schließen. Socket-
Leseoperationen besitzen standardmäßig einen Timeout von 30 Sekunden. Dieser
Timeout ersetzt keine Gesamtfrist gegen langsam eintreffende normale Requests;
ein nicht beendeter Worker verhindert weiterhin einen erfolgreichen Drain.

Für einen in einem anderen Thread laufenden `serve_forever()` gilt der bestehende
Python-Vertrag: erst `begin_draining()`, dann von außen `shutdown()` aufrufen,
danach `server_close()`. Die Methode `main()` übernimmt den kontrollierten Stopp
bei SIGINT/SIGTERM selbst und stellt vorherige Signalhandler nach dem Abschluss
wieder her. Weitere Stoppsignale dürfen die laufende Bereinigung nicht abbrechen.
Eine Erfolgsmeldung erscheint erst nach erfolgreichem Speicherabschluss.
Der Signalhandler markiert dazu `request_shutdown()`; die Accept-Schleife endet
erst an ihrer nächsten abgeschlossenen Dispatch-Grenze. Er wirft keine
asynchrone Ausnahme mitten in die Worker-Registrierung oder den Threadstart.

## Laufzeitgrenzen

Der vorhandene Compose-Dienst erhält `stop_grace_period: 150s`, damit der
130-Sekunden-Drain nicht bereits durch die kürzere Standardfrist beendet wird.
Compose sendet nach Ablauf seiner Frist SIGKILL; die Standardeinstellung beträgt
zehn Sekunden. Quelle: [Docker Compose Service Reference](https://docs.docker.com/reference/compose-file/services/#stop_grace_period).

Die Compose-Einstellung ändert keinen laufenden Dienst und gilt nicht automatisch
für Cloudflare-Container oder andere Supervisoren. Deren tatsächliche Stoppsignale
und Fristen müssen auf dem gewählten Zielhost separat geprüft werden. Ein
SIGKILL, Prozessabsturz oder Betriebssystemabbruch kann nicht geordnet abgefangen
werden. Nach einem erzwungenen Ende bleiben die vorhandenen dauerhaften
Reservierungen und gegebenenfalls aktiven Jobs konservativ zur Klärung bestehen.
Ein Timeout ist kein Beleg, dass eine upstream laufende Anfrage gestoppt wurde.

## Grenze zum gemeinsamen Backup

Der Drain gilt für HTTP-Worker genau dieser Serverinstanz. Er sperrt keine anderen
Webprozesse, CLI-Aufträge, Development-Runner oder Datenbankschreiber. Er liefert
weder einen gemeinsamen Snapshot aller Komponenten noch Writer-Fencing,
Freshness, ein produktives Backupformat oder eine Restore-Freigabe.

Der vorhandene `scripts/cold_recovery_drill.py` bleibt ein eigener synthetischer
Probelauf: Sein einziger Writer-Prozess endet vor der Sicherung. Konten,
Workspace-Dateien, Quellen und Geldbuch werden gemeinsam verschlüsselt gesichert,
die künstliche Quelle wird entfernt und zwei frische Prozesse prüfen das neue
Restore-Ziel. Die Prüfungen bedeuten keinen Restore des Produktionsbestands.

PR #102 wurde vor diesem Inkrement geprüft. Dessen koordinierte Konto-/CSV-
Recovery deckt weder sämtliche Workspace-Dateien noch die gemeinsame Geldbuch-
Generation ab. Sie wird hier nicht erneut implementiert oder stillschweigend
integriert. Der nächste Backup-Schritt muss diese bestehenden Verträge und den
nun begrenzten HTTP-Drain zu einer tatsächlich gemeinsamen Generation verbinden,
zusätzliche Writer ausschließen und in ein neues Ziel wiederherstellen.

Die HTTP-Integrationstests halten einen künstlichen Provider-Versand nach der
dauerhaften Geldreservierung an. Sie belegen den geordneten Abschluss, erhaltenen
Verlauf nach Wiederanlauf und unveränderte Reservierungen. Bei einem künstlich
unklaren Ausgang bleiben aktiver Auftrag und volle Reservierung erhalten; die
neue Startprüfung sperrt nach Wiederanlauf den nächsten Auftrag. Echte externe
Modellanfragen sind dabei ausgeschlossen.
