# TankAI: Projektprüfung am 11. Oktober 2026

## Auftrag und Prüfgrenze

TankAI soll ein eigenes, kooperierendes KI-System sein, das bessere Lösungswege findet,
nutzbare Ergebnisse liefert und für möglichst viele Menschen bezahlbar bleibt.
Die Prüfung bewertet vorhandene Belege und behebt die unabhängig reproduzierbare
Lücke in der Qualitätsmessung.

Geprüfter GitHub-Ausgangsstand: `caccc601e6b04a8cc7bb5b5a24a47e514e1d290e`,
Tree `dd8a71ee775658509e2158cad236993c65c11a53` (PR #79).
[CI Run 38118605910](https://github.com/Reznap87/TankAICore/actions/runs/38118605910):
`test` und `cloudflare` completed/success.
Diese CI beweist weder den aktuell deployten SHA noch reale Modellqualität.

Geprüft wurden Masterplan, README, Deployment-/Backup-Verträge, Cloudflare-Worker und
Containerkonfiguration, Modellaufruf-/Retry-Verträge, Live-Readiness, Vergleichskorpus,
Baseline, Zielklärungsimplementierung und zugehörige Tests. Zusätzlich erfolgten
lesende HTTP-Prüfungen der öffentlichen Website und lesende Cloudflare-API-Abfragen.
Authentifizierte Nutzerabläufe, Abrechnung, tatsächliche Provider-Secrets und
Wiederherstellung wurden nicht praktisch geprüft. Kein vollständiges Sicherheits-
oder Lasttest-Audit; keine Mailprüfung im Rahmen dieses Berichts.

## Ergebnis: Zielrichtung stimmt, Produktbeweis bleibt offen

Die erste Zielklärung erweitert den bestehenden Planner innerhalb seines bisherigen
Modellaufrufs. Varianten, Empfehlung, Unsicherheit und ein kleiner Versuch sind
strukturierte Verträge; Critic und Synthese erhalten diese Informationen.
Das passt zur kooperierenden TankAI-Richtung und vermeidet einen zusätzlichen
Planner-Aufruf. Deterministische Tests belegen die Übergabe und Fallbacks.
Eine bessere reale Antwortqualität gegenüber der Baseline ist damit noch nicht belegt.

Die eingefrorene Mock-Baseline erfüllt 3 von 15 Kriterien, verfehlt 12 und erreicht
einen gewichteten Score von 0.1778. Alle drei Fälle sind simuliert; externe Aufrufe
und externe API-Kosten sind null. Dies ist ein offengelegter Ausgangspunkt, keine
Messung der Intelligenz eines echten Modells. Der Korpus und seine Hashbindung
werden durch die Reparatur nicht geändert.

## Befunde und konkrete Folgearbeit

| ID | Priorität / Zustand | Befund und Abnahme |
| --- | --- | --- |
| AUD-01 | P1 / repariert im zugehörigen PR | `baseline_receipt` prüfte bislang nur Korpus-ID und Hash. Direkt übergebene Baselines konnten doppelte Fälle, unvollständige Kriterien und überschrittene Fallbudgets in einen Bericht übernehmen; unbekannte Fälle erzeugten einen `KeyError`. Loader und Receipt verwenden jetzt dieselbe vollständige Prüfung. Widersprüchliche externe Kosten bei `external_calls_made=false` werden ebenfalls verworfen. |
| AUD-02 | P1 / offen, bestehender P2-Owner | Den vollständigen Candidate auf denselben drei Aufgaben mit nutzbaren Ergebnisartefakten bewerten. Strukturierte Varianten allein bestehen keine fachliche Abnahme. Kritische Fehler, Zielerfüllung, Quellen, Laufzeit, tatsächliche Provider-Versuche und Kosten je erfolgreich erledigter Aufgabe messen. Der günstige Pfad muss dieselben Qualitätskriterien bestehen. |
| AUD-03 | P1 / offen, Issue #25 | Das dokumentierte Pilotbudget (10 EUR gesamt, 2 EUR/24 h, 0.20 EUR/Auftrag, höchstens fünf Modellversuche/Auftrag) besitzt noch keinen nachgewiesenen gemeinsamen monetären Reservierungs-/Abrechnungspfad. Vor jeder bezahlten Anfrage konservativ reservieren; Hauptmodell, Critic, Fallback und SDK-interne Retries einbeziehen. Parallelität, Timeout, Restart, unbekannten Tarif und unbekannten Verbrauch deterministisch prüfen. |
| AUD-04 | P1 / offen, Deployment-/State-Owner | `src/cloudflare.ts` verwendet `sleepAfter="5m"` und `TANKAI_RUN_STORE=/tmp/tankai_runs.jsonl`; das Image setzt lokale Auth-/Datenpfade unter `/app/data`. Die geprüfte Cloudflare-Konfiguration zeigt keinen externen persistenten Speicherpfad für diese Daten. Vor kostenpflichtigem oder dauerhaftem Nutzerbetrieb Persistenz, konsistente Sicherung und Wiederherstellung einschließlich Budget-/Auth-State nachweisen. Die konkrete laufende Konfiguration ist über die aktuelle Cloudflare-Verbindung nicht bestätigt. |
| AUD-05 | P1 / offen, Issue #25 | Die Readiness-Prüfung akzeptiert als Suchanbieter nur Brave oder Tavily und verlangt bei konkreter Providerwahl die entsprechende Secret-Anwesenheit. Der Worker setzt bei Live-Freigabe zusätzlich `TANKAI_STRICT_WEB_RESEARCH=1`. Der geplante Pilot mit bereitgestellten Inhalten ohne bezahlte Suche hat damit noch keinen nachgewiesenen zulässigen Pfad. Bewusst begrenzten Modus für bereitgestellte Inhalte implementieren und testen oder Suchnutzung samt Budget ausdrücklich neu entscheiden. Keine bloße Lockerung ohne fachliche Quellenprüfung. |
| AUD-06 | P1 / offen, Production-Owner | Öffentliches Health-Receipt meldet HTTP 200, Version `1.10.0-module-ownership`, `production_ready=false`, Auth erforderlich, Registrierung aus und Development-Queue aus. Die aktuelle Cloudflare-Verbindung listet ein Konto, aber weder die Domainzone `tankaicore.com` noch Workers. Deshalb fehlen aktuelle Konto-/Deploy-/Secret-Belege. Ein Versionsname bindet keinen Runtime-SHA. Richtige Kontoverbindung, exakten Deploy-SHA und aktuellen Preflight verifizieren. |
| AUD-07 | P2 / verbindliche Produktabnahme | Leistbarkeit über Kosten pro erfolgreicher Aufgabe und ein verständliches Nutzungslimit bewerten. Für alle Angebote gleiche Kernabnahme für Zieltreue, Fehlerkennzeichnung und Datenrechte; Unterschiede bei Umfang oder Geschwindigkeit transparent machen. Preis, Freikontingent und mögliche Fair-Use-Grenzen erst nach gemessenen Stückkosten festlegen. Keine unbewiesene Zusage gleicher Maximalfähigkeit bei unbegrenzter Nutzung. |

AUD-03 bis AUD-06 sind Vorgates für den geplanten bezahlten Livepilot.
Sie machen die bereits abgeschlossenen Arbeiten aus PRs #26 bis #29 nicht rückwirkend offen;
sie ergänzen fehlende Nachweise für den jetzt konkreten Pilotvertrag.

Der eingefrorene Vergleichskorpus erlaubt bis zu zwölf Modellaufrufe pro Fall.
Seine Baseline enthält zehn simulierte Aufrufe pro Fall. Das ist mit dem neuen Pilotdeckel
von fünf tatsächlichen Modellversuchen nicht direkt ausführbar. Für den Live-Candidate
muss eine zusätzliche Ausführungspolicy die jeweils strengere Grenze verwenden:
Minimum aus Korpuslimit und Pilotlimit. Den eingefrorenen Korpus oder die Baseline
nicht nachträglich erleichtern; Nichterfüllung innerhalb des Pilotbudgets sichtbar ausweisen.

## Öffentliche Website: begrenzte externe Beobachtung

Am 11. Oktober 2026, etwa 13:00 UTC, antworteten Startseite und `/api/health` mit HTTP 200.
`/favicon.ico`, `/favicon.png` und `/apple-touch-icon.png` lieferten passende Bildtypen
mit HTTP 200. Der nicht authentifizierte Zugriff auf `/api/workspaces` lieferte HTTP 401.
Login- und Aufgabenoberfläche sind im gelieferten HTML vorhanden; es wurde kein Login
versucht und kein Auftrag gestartet.

Die Startseite lieferte `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`
und eine CSP mit Script-Nonce, jedoch weiterhin `style-src 'self' 'unsafe-inline'`;
in dieser Antwort fehlte `Strict-Transport-Security`.
Im aktuellen Repository dokumentierte spätere Header-Härtungen dürfen deshalb
nicht als bereits an dieser URL produktiv nachgewiesen gelten.
Dies ist ein beobachteter Unterschied, kein Nachweis eines erfolgreichen Angriffs.

## Reparatur und Validierung

Vor der Reparatur: sieben vorhandene Tests bestanden; elf neue Regressionen schlugen fehl.
Nach der Reparatur: alle 18 fokussierten Tests bestanden lokal in einer isolierten Kopie
der providerfreien Evaluation mit dem gebündelten Python und Pydantic.

Die Regressionen prüfen direkte Receipt-Aufrufe mit doppelten/unbekannten Fällen,
fehlenden/fremden Kriterien, Zeit-/Aufruf-/Kosten-/Ausgabelimitüberschreitungen,
widersprüchlichen Simulationskosten und nachträglich veränderten Kriterienlisten.
Die bestehende CLI und der unveränderte Baseline-Receipt werden weiterhin geprüft.
Der PR muss zusätzlich die vollständigen Pflichtchecks `test` und `cloudflare` bestehen.

Es wurden keine Modell-/Suchanfragen, Secretänderungen, Käufe oder Deployments ausgeführt.
Die Änderung startet weder Pilot noch Budgetverbrauch.

## Leistbarkeit ohne leere Qualitätsversprechen

Die Arbeitsreihenfolge folgt dem tatsächlichen Bedarf: passendes gespeichertes Ergebnis
mit Aktualitätsprüfung, deterministisches Werkzeug oder ein ausreichend starkes Modell.
Aufwendige Kooperation wird eingesetzt, wenn sie bei derselben Aufgabe nachweislich
hilft. Ein günstiger Pfad, der die Abnahme verfehlt, zählt nicht als Einsparung.

Erst die gemessenen Stückkosten erlauben einen belastbaren Endkundenpreis.
Rechenbeispiel, kein Tarifangebot: 2 EUR reines Nutzungsbudget tragen bei 0.02 EUR
je erfolgreicher Aufgabe rechnerisch 100 Aufgaben, bei 0.20 EUR zehn.
Hosting, Fehlversuche, Support, Zahlungsgebühren und weitere Geschäftskosten müssen
bei einer Preisentscheidung zusätzlich berücksichtigt werden.
Ein eigener Modellhost wird erst anhand von Qualität, Auslastung und Gesamtkosten
gegen API-Nutzung entschieden.

## Quellen zur Container-Persistenz

Cloudflare dokumentiert den Containerdatenträger standardmäßig als flüchtig:
Nach Schlafen/Neustart entsteht ein frischer Datenträger aus dem Image.
Snapshots sind gesonderte, unveränderliche Zustandsaufnahmen und ersetzen keine
atomare Budgetdatenbank.
[Cloudflare: Lifecycle of a Container](https://developers.cloudflare.com/containers/concepts/architecture/).
Die lokale Compose-Volume-Beschreibung in DEPLOY.md beweist keine Cloudflare-Persistenz.
