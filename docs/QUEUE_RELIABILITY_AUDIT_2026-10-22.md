# Queue-, Zustands- und Parallelitätsprüfung

Datum: 22. Oktober 2026. Ausgangsstand:
`0e4e220` auf `codex/freellmapi-private-test`. Der eingefügte externe Prüftext
war eine Liste von Hinweisen, kein übernommener Sicherheitsnachweis.

## Umfang

`job_queue.py` wurde vollständig gelesen: Aufnahme, Integritätsprüfung,
Idempotenz, Auswahl, Start, Heartbeat, Abschluss, Fehler, Lease-Recovery,
Operator-Reaper und Dispatcher. Zusätzlich wurden Fence-Prüfungen,
Zustandsspeicherung, relevante Orchestrator-Zustandswechsel, Git-Metadatensperren
und der gemeinsam genutzte LLM-Zähler geprüft. Am Webserver wurden Auth-/CSRF-,
Fehler-, CSP-/ETag- und neue Workspace-Routen gezielt betrachtet. Dies war keine
vollständige Prüfung jeder Webroute oder aller Worker-/Container-Implementierungen.

## Reproduzierte und korrigierte Befunde

| ID | Priorität | Auslöser und vorheriges Verhalten | Korrektur und Regression |
|---|---|---|---|
| Q-01 | P2 | Die ersten 200 zulässigen Kandidaten gehören zu einem belegten Repository. Der 201. Auftrag eines freien Repositorys wird nie betrachtet, obwohl Worker frei sind. | Geordnete Seiten mit höchstens 200 Zeilen und stabilem Fortsetzungsschlüssel. Kein Offset: als ungültig markierte Zeilen können folgende Kandidaten nicht verschieben. Test mit 200 blockierten Aufträgen und einem ausführbaren Folgeauftrag. |
| Q-02 | P2 | Nach dem erfolgreichen Queue-Commit fällt die getrennte Fence-Datenbank beim Freigeben aus. Der Dispatcher versucht dann, den bereits erfolgreichen Auftrag als Fehler zu speichern, und wirft einen zweiten Fehler. | SQLite-Fehler bei dieser nachgelagerten Bereinigung verändern den dauerhaft abgeschlossenen Zustand nicht. Falls auch das Audit ausfällt, wird nur eine begrenzte Diagnose mit Fehlertyp protokolliert. Die Fence bleibt bis Ablauf belegt; kein Wiederholen der Pipeline. Tests für beide Datenbankfehler. |
| S-01 | P1 | Projektzustand und Publikationsledger entfernen eine Sperrdatei allein wegen ihres Alters. Ein weiter laufender Besitzer kann dadurch gleichzeitig mit einem neuen Besitzer schreiben; seine spätere Freigabe kann auch die neue Sperre löschen. | Betriebssystem-Sperre am stabilen Dateianker. Alter entzieht keine aktive Sperre. Test mit künstlich gealtertem aktivem Besitzer sowie tatsächlichem Prozessabbruch und anschließendem Erwerb. |
| G-01 | P1 unter Windows | Die Git-Metadatensperre prüft einen Besitzer mit `os.kill(pid, 0)`. Anders als unter POSIX kann dieser Aufruf unter Windows den Besitzer beenden. | Dieselbe Betriebssystem-Sperre statt Prozesssignalen oder PID-Recycling-Heuristiken. Der Regressionstest ersetzt das Signal durch einen ablehnenden Spy und führt niemals den gefährlichen Aufruf aus. |
| L-01 | P2 | Zwei gleichzeitige `TankAI.run`-Aufrufe auf derselben Instanz können Goal, Receipts und Run-Zähler gegenseitig zurücksetzen. Der atomare einzelne Verbrauch verhindert diesen Lebenszyklusfehler nicht. | Eine zweite gleichzeitige Aufgabe wird vor Änderungen an der Instanz abgewiesen. Spezialisten innerhalb des ersten Laufs bleiben parallel ausführbar und teilen denselben Zähler. Test mit angehaltenem Modellaufruf, zweitem Start und anschließendem regulärem Lauf. |
| O-01 | P2 | Ein noch keinem Agenten zugewiesener, bereits abgebrochener Task lässt sich über `start_agent` wieder aktivieren. | Neue Agenten starten nur für `pending`-Tasks. Abbruch, Revision, Agentenbestand und Schreibbereiche bleiben beim abgewiesenen Start unverändert. |

Die Prioritäten beschreiben technische Auswirkungen im vorgesehenen lokalen
Betrieb, keine vollständige Bewertung möglicher externer Angriffswege.

## Zusätzliche Ausfall- und Parallelitätsnachweise

- Vier unabhängige Queue-Instanzen beanspruchen denselben Auftrag gleichzeitig:
  genau ein Lease und genau ein belasteter Versuch.
- Eine injizierte Schreibstörung nach Fence-Erwerb rollt Queue-Zustand und
  Versuchszahl zurück. Der erworbene Fence wird freigegeben; der folgende
  Versuch erhält eine höhere Epoche.
- Bestehende Tests prüfen Tokenbindung, monotone Epochen, Recovery nur nach
  Queue- und Fence-Ablauf, Erhalt eines noch lebenden externen Fence,
  Heartbeat-Verlängerung und aktive Beendigung nach Fence-Verlust.
- Ein Fehler innerhalb einer Mutation wird nicht zu einem Sperr-Timeout
  umgedeutet. Ein stabiler, aber freier Dateianker blockiert keinen neuen Start.

## Betriebsgrenzen und Umstellung

Die Queue verwendet weiterhin lokale SQLite-Datenbanken. Schreibtransaktionen,
Repository-Einzigkeit und Fence-Prüfungen sind keine Exactly-once-Garantie für
beliebige externe Nebenwirkungen. Nach Lease-Verlust kann eine alte Ausführung
bis zur nächsten Guard-Prüfung und Prozessbeendigung noch laufen; die Guards
müssen deshalb an allen relevanten Veröffentlichungsstellen erhalten bleiben.
Recovery kann neue Ausführungsversuche beginnen. Die Versuchsgrenze bleibt aktiv.

Die neue Sperre verwendet unter Unix `flock`, unter Windows eine Bytebereichsperre.
Der Dateianker wird absichtlich nicht gelöscht. Alle schreibenden Prozesse müssen
vor dem Wechsel vom alten Alters-/Löschprotokoll beendet werden; alte und neue
Protokollversion dürfen nicht parallel dieselben Dateien verwalten. Die bisherigen
`stale_lock_seconds`-/`stale_seconds`-Argumente bleiben zur Aufrufkompatibilität
vorhanden und entziehen künftig keine Sperren. Lokaler Host und lokales
Dateisystem bleiben Voraussetzung; kein NFS-/Mehrhost-Koordinator.

Auf dem privaten Chat-Testserver bleibt die Development-Queue deaktiviert.
Repositorytests oder ein neu gebauter Container aktivieren keine autonomen Worker.
Öffentliche Produktion und reale langlaufende Worker benötigen eigene Betriebsabnahme.

## Offene Punkte aus dem eingefügten Bericht

Zum ursprünglichen Auditstand blieb IP-Pinning offen. In der anschließenden
Korrektur verbindet der Seitenabruf direkt zu geprüften IPs und behält TLS-SNI,
Zertifikats-/Hostprüfung und Redirect-Revalidierung bei; Umfang und Grenzen stehen
in [WEB_FETCH_PINNING.md](WEB_FETCH_PINNING.md). Ein endgültig abgelehnter Plan
stoppt nun ebenfalls vor Ausführung und optionalen Modellaufrufen, dokumentiert
in [IMPROVEMENTS_IMPLEMENTATION.md](IMPROVEMENTS_IMPLEMENTATION.md).

Image-Digest-Bindung allein ist keine Image-Signaturprüfung. Anschließend wurde
für produktive Rootless-Queue-Executor eine zusätzliche Cosign-Signaturpflicht
implementiert; Release- und Betriebsgrenzen stehen in
[WORKER_IMAGE_SIGNATURES.md](WORKER_IMAGE_SIGNATURES.md). Die
Pipe-Behandlung und eine mögliche scrypt-Anpassung bleiben offen. Ebenso offen bleiben
Geldreservierung über Router-Fallbacks, dauerhafte Chat-Schrittaufträge und die
unabhängige fachliche Abnahme des eingefrorenen Korpus.

Referenzen für die plattformspezifischen Korrekturen:
[Python: Dateisperren unter Unix](https://docs.python.org/3/library/fcntl.html),
[Python: Dateisperren unter Windows](https://docs.python.org/3/library/msvcrt.html),
[Python: Windows-Verhalten von os.kill](https://docs.python.org/3/library/os.html#os.kill).
