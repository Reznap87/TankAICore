# Besitzerpilot vor dem ersten Auftrag prüfen

`GET /api/pilot/preflight` prüft die aktuellen lokalen Startvoraussetzungen
des konfigurierten Besitzerpiloten. Die Route benötigt eine gültige
Session-Anmeldung. Bei einem konfigurierten Pilot muss die angemeldete Person
der freigegebene Besitzer im freigegebenen Mandanten und Arbeitsraum sein.
Fremde Besitzer und Mitglieder erhalten keine Budget- oder Modelldaten.

Die bestehende Chatoberfläche ruft diese Prüfung bei einem konfigurierten
Pilot zusammen mit der Verbindungsprüfung auf. Die Einstellungen zeigen das
Ergebnis und verständliche Sperrgründe. Ein Modellpaar ohne gemeinsame
Budgetfreigabe erscheint ausdrücklich als noch gesperrter Live-Start.

## Was geprüft wird

- Aktuelle Session und aktuelle Besitzerrolle, auch nach der Laufzeitprüfung.
- Gültigkeit der bereitgestellten Quellen für den aktuellen Arbeitsraum.
- Tatsächlich konfigurierte, geschützte Hauptmodell- und Critic-Adapter.
- Freigegebene Provider-/Modell-/Endpunkt-Kombination und aktuelle Tarife.
- Unabhängigkeit des Prüfmodells.
- Aktuelles Zeitfenster und dauerhafter Stoppzustand des Geldbuchs.
- Bereits aktive Aufträge, Auftragskontingente und verbleibendes Tages- und
  Gesamtbudget einschließlich aller nicht abgerechneten Reservierungen.

Die Antwort enthält `admission_checks_passed`, stabile `blockers`,
die beiden Modellidentitäten und ausgewählte Budgetwerte in Mikro-Euro.
Ein Euro entspricht 1.000.000 Mikro-Euro. Personenkennungen, Quelleninhalte,
Tarifeinzelheiten, API-Schlüssel und Session-Tokens werden nicht ausgegeben.
HTTP-Antworten verwenden den vorhandenen `Cache-Control: no-store`-Vertrag.

Ohne angehängten Besitzerpilot antwortet die Route für einen angemeldeten
Besitzer mit `owner_pilot_not_configured`. Beschädigter oder unzugänglicher
Pilotzustand wird mit HTTP 503 und `pilot_state_unavailable` ausgewiesen.
Fehlende Anmeldung ergibt HTTP 401, fehlende Berechtigung HTTP 403.

## Keine Freigabe durch einen Statusaufruf

Die Prüfung erstellt keinen Auftrag und keine Reservierung. Sie ruft weder
Tokenzähler noch Provider auf und verändert weder Geldbuch noch dessen
gespeicherte Zeitbeobachtung. Nach Neustart vorgefundene aktive Aufträge und
unklare Kosten bleiben erhalten. Die normale authentifizierte Laufzeit kann
wie beim bestehenden Healthcheck beim ersten Zugriff lokale Speicher öffnen.

`admission_checks_passed=true` ist eine zeitpunktbezogene Prüfung der genannten
Voraussetzungen. Es garantiert weder, dass das Restbudget für die konkrete
Aufgabe reicht, noch dass ein konkurrierender Auftrag den Platz frei lässt.
Vollständige Requestkosten, Berechtigung, Quellen, Budget und Provider-Ratelimits
werden beim tatsächlichen Versand weiterhin geprüft. Die Antwort setzt immer
`dispatch_authorized=false` und `production_ready=false`.

`operational_verification=not_assessed` bezeichnet die getrennten offenen
Betriebsnachweise: tatsächlicher Host, dauerhafte Speicherung, koordinierter
Restore, alte Writer, aktuell ausgerollte Revision und ein ausdrücklich
freigegebener begrenzter Live-Test. Diese Route erteilt keine Produktions-
oder Ausgabenfreigabe und ersetzt Issue #25 nicht.

## Entwicklungsstand

Die Änderung baut auf dem Entwurf PR #101, Head
`b3e82a2fdd8e6878e5aa72b36124cd23c11ba885`, auf. PR #101 ist eine Abhängigkeit;
die vorhandene monetäre Durchsetzung wird hier nicht erneut implementiert.
Die unabhängigen Arbeiten in PR #102/#103 und die Host-/Workerentwürfe
bleiben getrennt.

Die Tests verwenden echte lokale HTTP-Anmeldung und Core-/SDK-Code mit
abgefangenen SDKs und künstlichen Tarifen. Sie prüfen insbesondere null
Provideraufrufe, unveränderte Geldbuch-Snapshots, fremde/revozierte Zugänge,
abgelaufene Quellen/Tarife, Neustartzustand und offene Aufträge. Sie sind
kein Nachweis eines produktiven Modellaufrufs.
