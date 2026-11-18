# Einmaliger nativer Snapshot-Pilot: Laufplan

Stand: 3. November 2026. Dieser Plan beschreibt einen getrennten Test mit
synthetischen Daten. Er ist kein ausgeführter Deploy und kein Nachweis für
Produktionsbereitschaft. Der [Controllervertrag](CLOUDFLARE_NATIVE_PILOT.md)
und der [Offline-Supervisor](CLOUDFLARE_SNAPSHOT_PILOT.md) gelten weiter.

## Ziel und aktueller Befund

Der erste echte Lauf soll einen frischen Mock-Container starten, geordnet
stilllegen, nativ aufnehmen und auf einer zweiten Instanz ausschließlich
inspizieren. Root-ID, Dateidigest und gebundenes Image müssen übereinstimmen.
Es gibt keine Produktionsaktivierung, echten Nutzerdaten oder Modellaufrufe.

Die lesende Kontoabfrage ergab:

| Gegenstand | Befund |
| --- | --- |
| Autorisiertes Konto | `11720f44c5b36d93ad89b5db715beab9` |
| Vorhandener Worker | `tankai-core` |
| Vorhandene Container-Anwendung | `tankai-core-tankaicontainer`, ID `a0378ad1-191f-4114-ab2f-9c2c906484a0`, Scheduling `default` |
| Geplanter neuer Worker | `tankai-snapshot-pilot`, bei der Prüfung nicht vorhanden |
| Geplante neue Anwendung | `tankai-snapshot-pilot-native`, bei der Prüfung nicht vorhanden |
| Lokale CLI | `wrangler whoami`: nicht angemeldet |
| Billing-Nachweis | Konto-Subscriptions GET: HTTP 403, Code 10000; Connector-OAuth deckt Billing nicht ab |

Die Connector-Anmeldung ersetzt keine Wrangler-Anmeldung. Ein existierender
Container belegt weder aktuelles Guthaben noch eine freigegebene neue Ausgabe.
Vor externen Änderungen Kontozugehörigkeit und Abwesenheit der neuen Namen
erneut lesen. Nicht in ein temporäres Wrangler-Konto ausweichen.

## Privater Bedienweg

```text
Python CLI -> http://127.0.0.1:8766
           -> lokaler Worker mit remote Service-Bindung PILOT
           -> tankai-snapshot-pilot -> source / inspection
```

Die separate `wrangler.pilot-operator.jsonc` ist ausschließlich für `wrangler
dev` bestimmt. Sie hat keine öffentlichen Routen. Der Zielworker behält
`workers_dev:false`, deaktivierte Preview-URLs und keine Domains.
Der Produktionsworker wird weder als Proxy noch als Zugangsweg verändert.

Die Brücke akzeptiert ausschließlich die fünf festen Pilotpfade an der festen
Loopback-Adresse. Browser-Origin-/Sec-Fetch-Header werden abgewiesen. Nur die
benötigten Protokollheader werden weitergegeben; Antworten sind begrenzt und
enthalten keine Upstream-Cookies, Weiterleitungsziele oder Diagnoseheader.
Die Brücke prüft das Tokenformat; **der entfernte Pilot authentifiziert den
eigentlichen Token**. Lokale Prozesse mit dem Token sind weiterhin berechtigt.

Cloudflare dokumentiert diesen Zugang über einen lokalen Worker mit entfernter
Service-Bindung zu einem deployten Worker; direkte entfernte DO-Bindungen sind
dafür nicht vorgesehen. Remote-Bindungen wirken auf echte Ressourcen und werden
normal berechnet. Der lokale Test ersetzt diesen nativen Plattformnachweis nicht.
[Quelle](https://developers.cloudflare.com/workers/local-development/).

## Was vor dem externen Lauf konkret fehlen darf und was nicht

Vor Deployment müssen der Betreiber die Anmeldung im genannten Konto, den
bestehenden Workers-Paid-Status und den Ausgabenrahmen bestätigen. Kein Planwechsel
oder Guthabenkauf ist Bestandteil dieses Auftrags. Der gegenwärtige Connector
kann Billing nicht prüfen; dafür keinen umfassenden API-Token im Chat anfordern.

Nach Freigabe wird ein sauberer, CI-geprüfter Source-Commit festgehalten. Das
Pilot-Image wird genau daraus gebaut, in die Registry dieses Kontos gepusht und
der **Registry-Digest** unabhängig aus Push-/Registry-Metadaten gelesen. Das
Laufprotokoll bindet diesen Digest und den Commit. Lokale Image-IDs sind dafür
kein Ersatz. Derzeit gibt es noch keinen neuen Registry-Digest und keine
Deployment-ID, die als tatsächlich bereitgestellt angegeben werden könnten.

Wrangler löst benannte Images für `ctx.container.images` als unveränderliche
Digest-Referenzen auf. Dies muss im tatsächlichen Ziel mit dem aufgezeichneten
Registry-Digest übereinstimmen; der Controller verweigert mutable Referenzen.
[Quelle](https://developers.cloudflare.com/containers/guides/image-management/).

Nur für die neue Pilotumgebung wird ein zufälliger, ausreichend langer
URL-sicherer Operator-Token eingerichtet. Er gehört weder ins Repository noch
in CLI-Argumente, Chat, Screenshots oder Ergebnisdateien. Kein Provider-Key
gelangt in den synthetischen Container. Das erforderliche Secret heißt
`TANKAI_PILOT_CONTROL_TOKEN`; das Manifest enthält nur diesen Namen.

## Umfang und Kosten

Technisch begrenzt sind zwei feste Lite-Instanzen, ein Start, eine Aufnahme und
ein Restore. Der HTTP-Läufer erlaubt höchstens sieben Anfragen und drei
Mutationen, ohne automatische Wiederholung. Er prüft vor jedem Aufruf eine
lokale 15-Minuten-Frist; eine einzelne HTTP-Anfrage hat maximal 180 Sekunden.
Das ist **kein serverseitiger Laufzeit- oder Kontokostendeckel**. Ein Client-Abbruch
beendet den entfernten Vorgang nicht. Die zehnminütige Container-Inaktivitätsfrist
ersetzt deshalb weder Beobachtung noch Rückbau.

Laut aktueller Preisübersicht benötigt Containers Workers Paid (5 USD/Monat
mit Inklusivkontingenten). Lite hat 1/16 vCPU, 256 MiB RAM und 2 GB Disk.
Bei zwei Instanzen mit je 15 Minuten und voller CPU ergibt sich rechnerisch
ein Compute-Anteil von etwa **0,003627 USD** vor Inklusivkontingenten:

```text
1800 * (0.0625 * 0.000020 + 0.25 * 0.0000025 + 2 * 0.00000007)
```

Dieser Szenariowert ist keine Gesamtkostenobergrenze. Worker, Durable Objects,
Registry, Logs und mögliche weitere abrechenbare Bestandteile sind gesondert
zu betrachten. Einen Snapshot-Speicherpreis belegt die geprüfte Preisübersicht
nicht; deshalb wird kein erfundener Gesamtdeckel zugesagt.
[Quelle](https://developers.cloudflare.com/containers/platform/pricing/).

Cloudflare dokumentiert für Snapshots eine implizite Aufbewahrung von 30 Tagen,
die ein Restore erneuert; eine eigene TTL lässt sich aktuell nicht setzen.
Die 24-Stunden-Regel unseres Controllers begrenzt nur die Restore-Annahme,
nicht die Speicherung beim Anbieter. Weder das Löschen eines Workers noch
das Löschen einer Anwendung wird hier als bestätigtes vorzeitiges Löschen
des Snapshot-Blobs beschrieben.
[Quelle](https://developers.cloudflare.com/containers/guides/snapshots/).

## Durchführung nach erfüllten Voraussetzungen

1. Konto, Billing, Source-Commit, Registry-Digest und einmaligen Umfang prüfen
   und im Laufprotokoll festhalten. Nur die getrennte Pilotkonfiguration
   bereitstellen und deren neue Worker-/Anwendungs-/Namespace-IDs erfassen.
   Produktions-ID und Produktionsimage bleiben unberührt.
2. Token über den vorgesehenen Secret-Eingabekanal einrichten. Deployment-ID,
   Digest und Scheduling `durable_object` durch unabhängige Control-Plane-
   Abfragen prüfen; ein erfolgreicher CLI-Exit allein reicht nicht.
3. Lokale Operator-Brücke starten, ausschließlich in diesem Konto:

   ```powershell
   $env:CLOUDFLARE_ACCOUNT_ID = '11720f44c5b36d93ad89b5db715beab9'
   npm run pilot:operator
   ```

4. In einem zweiten Terminal ist zunächst ein folgenloser Plan möglich:

   ```powershell
   .venv/Scripts/python.exe scripts/pilot_rehearsal.py
   ```

   Der echte Befehl benötigt die zuvor erfassten Werte; Platzhalter nicht als
   Nachweis übernehmen. Token bei interaktiver Ausführung verdeckt eingeben;
   bei Automatisierung ausschließlich über stdin zuführen:

   ```text
   python scripts/pilot_rehearsal.py --execute
     --account-id 11720f44c5b36d93ad89b5db715beab9
     --image REGISTRY_REFERENCE_WITH_SHA256
     --source-commit REVIEWED_40_HEX_COMMIT
     --output NEW_RECEIPT_PATH.json
   ```

   Den Befehl als eine Zeile ausführen. Die Ausgabedatei wird vor dem ersten
   Aufruf exklusiv angelegt; existiert sie, startet kein weiterer Lauf.

5. Erwartet werden leere Quelle und Inspector, danach Quellrevisionen 2, 4,
   6 und Inspectorrevision 2 mit identischer Capsule. Jedes ungewisse oder
   abweichende Ergebnis beendet den Läufer. Kein Reset, kein erneuter Start
   und kein Retry mit neuer Operations-ID. Nur Status lesend abgleichen.
6. HTTP-Receipt mit tatsächlicher Deployment-/Image-/Instanzidentität und
   nativen Plattformbefunden abgleichen. Der Läufer behauptet ausdrücklich
   `native_execution_independently_verified:false`; er belegt selbst nur
   HTTP-Vertrag und Root-Abgleich. Erst zusätzliche native Belege erlauben
   den Befund „Cloudflare-Aufnahme/Restore tatsächlich geprüft“.

## Rückbau und Abschluss

Nach jedem Mutationsversuch markiert das Receipt `cleanup_required:true`, auch
bei verlorener Antwort. Der Läufer löscht absichtlich keine Remote-Ressourcen.
Ein verantwortlicher Operator muss den Rückbau zu Ende führen:

1. Lokal laufende Brücke beenden, beide neuen Pilotinstanzen im genannten
   Konto lesend zuordnen und beenden. Kein automatischer Neustart.
2. Nur die neu protokollierte Pilot-Anwendung und den neuen Pilotworker mit
   ihren verifizierten IDs entfernen. Bei Namens-/ID-Konflikt stoppen.
   `tankai-core` und Anwendungs-ID `a0378ad1-191f-4114-ab2f-9c2c906484a0`
   dürfen nicht Ziel eines Rückbaus sein.
3. Nur das ausschließlich für diesen Lauf erzeugte, nicht anderweitig
   referenzierte Registry-Image entfernen. Bestehende Rollback-Images erhalten.
   Namespace-/Secret-Restzustand gesondert lesen und dokumentieren; das
   Entfernen eines Worker-Scripts ist kein Beweis für gelöschte DO-Daten.
4. Fehlende Pilot-Anwendung/Worker, keine laufenden Pilotinstanzen und den
   tatsächlichen Rückbaubefund separat festhalten. Snapshot-Aufbewahrung und
   ungeklärte Kostenbestandteile ausdrücklich offen lassen, solange sie nicht
   unabhängig belegt sind. Kein Produktions-Cutover gehört zum Abschluss.

## Lokale Abnahme

```text
python -m pytest -q tests/test_pilot_rehearsal.py
npm run pilot:test
npm run pilot:operator:check
npm run pilot:operator:test
```

Die Python-Tests verwenden einen echten lokalen HTTP-Server mit synthetischen
Antworten. Die Operator-Tests prüfen echte workerd-Service-Bindung mit lokalem
Fake, Headerfilter, Browser-Abweisung und Größenlimits. Die 22 Controllergruppen
verwenden echte Durable-SQLite-Persistenz und simulierte native Effekte.
Keine dieser Prüfungen startet entfernte Container oder bezahlt ein Modell.
