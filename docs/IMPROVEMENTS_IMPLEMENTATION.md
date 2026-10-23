# Umsetzung der Verbesserungsanalyse

## Wissensabruf

Langzeitwissen wird vor der Vektor-Rangfolge auf gültige semantische oder
prozedurale Einträge mit Hot-/Warm-Aufbewahrung eingeschränkt. Ungültige,
widersprüchliche, unbekannte oder archivierte Einträge gelangen nicht in den
Aufgabenkontext. Sie können passende gültige Treffer nicht aus der begrenzten
Trefferliste verdrängen. Leere Abrufe benötigen keine Embedding-Anfrage.

Einträge werden nicht nachträglich gelöscht oder umgeschrieben. Korrigierter
Gültigkeitsstatus wirkt ab dem nächsten Abruf; Zugriffszähler werden nur für
tatsächlich verwendete gültige Einträge erhöht. Die Prüfung des Status ist
keine unabhängige Wahrheitsprüfung eines als gültig eingestuften Inhalts.

## Verbrauchserfassung

`complete()` bleibt textkompatibel. Die Adapter unterstützen zusätzlich ein
Antwortobjekt mit gemeldetem Modell und bekannten Tokenfeldern. Cache- und
Reasoning-Anteile bleiben getrennt, da sie je nach Protokoll Teilmengen oder
zusätzliche Eingabekomponenten darstellen. Nicht gemeldete Daten bleiben offen;
es werden keine Nullkosten oder tatsächlichen Upstream-Versuchszahlen erfunden.

Die Webruntime schreibt pro Workspace eine `usage.db`. Jeder Adapterversuch
wird vor dem SDK-Aufruf erfasst und danach aktualisiert. Kann der Anfang nicht
gespeichert werden, wird kein Request gesendet. Nach einem Abbruch bleiben
angefangene Versuche als ungeklärt erhalten. Der Zähler für neue Runs wird
zurückgesetzt; die dauerhafte Verbrauchshistorie früherer Runs bleibt bestehen.

Prompts, Antworttexte, Schlüssel und interne Fehlermeldungen gehören nicht in
diese Datenbank. Die Metadaten verwenden eine feste Feldliste. Parallele Aufrufe
teilen weiterhin das vorhandene atomare Aufrufbudget. Eine getrennte Quittung
`llm_usage` erhält gemeldete Werte, unvollständige Versuche und den ausdrücklichen
Preisstatus `not_priced`. Die Chatansicht bietet diese Angaben unter
**Verbrauch ansehen** an. `GET /api/runs/{goal_id}/usage` ist angemeldet und auf
den aktiven Workspace begrenzt. Gemeldete Modellnamen, bekannte Tokenwerte und
Hinweise auf fehlende Daten sind auch im Gesprächsexport enthalten.

Referenz für das kompatible Antwortformat:
[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Andere kompatible Anbieter können diese Felder weglassen oder eigene Aliase
melden. Eine gemeldete Modellbezeichnung bestätigt keine unabhängige Prüfinstanz.

## Gemeinsamer Upstream-Schutz

Zusätzlich zur bisherigen Nutzergrenze kann der Betreiber
`TANKAI_SHARED_UPSTREAM_SCOPE` (1–64 kleine Buchstaben, Ziffern, Punkt, Bindestrich,
Unterstrich) setzen. Dann teilen sich alle Nutzer, Workspaces, Modellaliase und
SDK-Protokolle dieselbe persistente Zulassung in der Auth-Datenbank. Standard:
zwei Zulassungen pro 60 Sekunden; konfigurierbar über
`TANKAI_SHARED_UPSTREAM_CALLS_PER_WINDOW` und
`TANKAI_SHARED_UPSTREAM_WINDOW_SECONDS`. Bei Ablehnung wird kein SDK-Aufruf und
kein Run-Budgetverbrauch ausgelöst. Mock-Aufrufe belasten diesen Pool nicht.

Das ist eine konservative Grenze vor dem Router, keine Bestätigung seiner
internen Versuche oder eine faire Warteschlange. Bei Limitüberschreitung bleibt
die bisherige explizite Ablehnung mit Retry-After erhalten. Der neue gemeinsame
Pool wird ohne bekannten Upstream-Bereich nicht automatisch aktiviert.

## Passender Ausführungsweg

Vollständig erkannte einfache Rechenaufgaben mit der Standard-Abnahme verwenden
den bestehenden `verified_single_step`-Pfad. Die Auswahl benötigt keinen
Modellaufruf. Plan-, Schritt- und Endprüfung bleiben erhalten; Synthese übernimmt
das bereits geprüfte Einzelergebnis. Zusätzliche Anforderungen, Recherche,
Dokumente, andere Aufgaben oder eine individuelle Abnahme nutzen den bisherigen
Planer. Einstellungen werden auch nach Fehlern zurückgesetzt. Die Auswahl wird
im gespeicherten Ergebnis als `execution_path` offengelegt.

## Offene Wissensaufbereitung

Die Chatnavigation bietet eine getrennte Übersicht aller offenen Aufbereitungen
des aktiven Workspace, unabhängig vom 20-Einträge-Verlauf. Die API
`GET /api/memory/work?offset=0` liefert maximal 20 Metadatensätze pro Seite, mit
maximalem Offset 10000. Sie liefert keine Prompts oder Wissensinhalte. Fertige
Aufbereitungen verschwinden aus der Liste. Der bestehende CSRF-geschützte
Wiederaufnahmepfad bleibt allein für lokale vorbereitete Einträge zuständig.
Seiten sind eine aktuelle Liste; gleichzeitige Zustandsänderungen können ihre
Position verschieben. Aktualisieren beginnt deshalb wieder auf Seite eins.

## Offline-Sicherung und isolierte Wiederherstellung

`python -m tankai.web.data_snapshot create DATENWURZEL NEUES_SNAPSHOT --offline`
setzt voraus, dass **alle** schreibenden Prozesse gestoppt sind. Die Funktion
ersetzt diese Betriebsvoraussetzung nicht durch eine eigene Sperre. Bekannte
Auth-, Workspace-, Verbrauchs- und Queue-Datenbanken werden mit SQLite-Backup
kopiert; Verlauf, Run-Archiv, Wissensindex und kaltes Archiv gehören ebenfalls
zum Bestand. Unbekannte Dateien, Verknüpfungen und Änderungen während der
Sicherung verhindern ein vollständig markiertes Snapshot.

`python -m tankai.web.data_snapshot verify SNAPSHOT` prüft vollständigen Bestand,
SHA-256, Datenbankintegrität und Fremdschlüssel. Die Prüfsummen erkennen
Beschädigungen, sie sind keine signierte Herkunftsbestätigung.
`python -m tankai.web.data_snapshot restore SNAPSHOT NEUE_DATENWURZEL` kopiert
ausschließlich in ein neues isoliertes Ziel. Bestehende Installationen werden
nicht ersetzt. Konfiguration und Zugangsdaten außerhalb der Datenwurzel
benötigen eine getrennte Sicherung. Externe Repository-/Worker-Arbeitsverzeichnisse
der Development-Queue sind ebenfalls kein Bestandteil dieser Datensicherung.

Snapshots enthalten private Nutzerdaten und Kontohashes. Unter Unix werden
Ordner mit 0700 und Dateien mit 0600 angelegt; unter Windows gelten zusätzlich
die ACLs des vom Betreiber gewählten privaten Zielordners. Keine öffentliche
Downloadroute. Ein lokaler Test stellt Anmeldung, Verlauf, Kurzzeitwissen,
Langzeitwissen, Index und Usage aus vollständigen Fixture-Daten wieder her.
Dies allein ist noch kein Wiederaufbau der echten Serverinstallation.

Am 22. Oktober 2026 wurde zusätzlich der Schreibbetrieb des privaten Core kurz
gestoppt und eine echte Sicherung in ein getrenntes privates Serververzeichnis
erstellt. Die isolierte Wiederherstellung erhielt ein bestehendes Konto samt
Passwort-Hash, einen Arbeitsraum, drei Gespräche und sieben Wissenseinträge.
Der Anmeldeablauf wurde zusätzlich mit einem Fixture-Konto ausschließlich im
wiederhergestellten Ziel geprüft. Der ursprüngliche Datenbestand blieb erhalten,
der normale Dienst wurde danach erfolgreich neu gestartet. Router-Volume und
Schlüsseldateien außerhalb der Core-Datenwurzel gehören nicht zu diesem Beleg.

## Weitere Arbeit aus dem Auftrag

### Ausführung nur nach bestandenem Plan

Nach der letzten vorgesehenen Planreparatur blockiert eine fehlgeschlagene
Planprüfung nun bereits vor dem ersten Spezialisten. Das gilt für serielle und
parallele Ausführung sowie den verkürzten Rechenpfad. Synthese, finale
Modellprüfung und optionale Wissensaufbereitung werden dann nicht aufgerufen.
Der Commander speichert stattdessen eine ausdrücklich nicht freigegebene Antwort
mit begrenzten Prüfhinweisen und Vorschlägen, übersprungenen Schritt-IDs,
tatsächlichem Verbrauch und dem Diagnoseereignis `reject_plan`. Schritte bleiben
unausgeführt, statt als fachlich fehlgeschlagene Ausführungen ausgegeben zu werden.

Die episodische Diagnose bleibt bei aktivem Langzeitgedächtnis als `unknown`
erhalten. Sie wird nicht in semantisches Wissen oder ein Verfahrensmuster
umgewandelt und erzeugt keinen Aufbereitungsauftrag. Simulationen bleiben
ausdrücklich Simulationen. Ein im erlaubten Reparaturfenster akzeptierter Plan
kann weiterhin normal ausgeführt werden. Sitzungs-/CSRF-geschützte HTTP-Tests
prüfen außerdem die gespeicherte Antwort im Verlauf.

Die Verbrauchserfassung ist die Grundlage, noch keine vollständige Geldbudget-
Reservierung. Vor kostenpflichtigen Versuchen müssen bestätigte Preisregeln,
konservative Reservierungen, gemeinsame Deckel und Router-Fallback-Grenzen
durchgesetzt werden. Die vorhandenen Vorgates unter Issue #25 bleiben relevant.

Offen bleiben die faire wartende Zulassung mit vollständigen Routerversuchen,
dauerhafte Chat-Aufträge einschließlich Schritt-Checkpoints und Abbruch sowie
die unabhängige Qualitätsabnahme am eingefrorenen Korpus. Der vollständige
Wiederherstellen der Produktionskonfiguration und des gesonderten Router-Volumes
bleibt ein weiterer Betriebsnachweis. Diese
offenen Punkte werden nicht aus erfolgreichen lokalen Tests abgeleitet.
