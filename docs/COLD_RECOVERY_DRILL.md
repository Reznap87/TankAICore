# Gemeinsamer, isolierter Wiederherstellungs-Probelauf

`scripts/cold_recovery_drill.py` prüft den gemeinsamen Kalt-Restore eines
**ausschließlich künstlichen** TankAI-Datenbestands. Es ist Testwerkzeug und
kein produktives Backupformat, Live-Sicherungsdienst oder Server-Restore.
Der Standard-Webstart lädt oder aktiviert dieses Werkzeug nicht.

## Was tatsächlich passiert

1. Ein eigener Prozess erstellt in einem neuen temporären Ordner zwei künstliche
   Konten in getrennten Mandanten und Arbeitsräumen. Echte HTTP-Anmeldung und
   simulierte Chatläufe erzeugen Verlauf, Run-Journal und Langzeitdaten.
   Zusätzlich werden bekannte Kurzzeit-, Vektor- und Cold-Archive-Einträge,
   eine Quellenfreigabe und eine gemeinsame Geldbuchhaltung angelegt.
2. Der Schreibprozess beendet sich vollständig. Nur in diesem eigenen,
   künstlichen Bestand werden danach alle SQLite-WAL-Daten eingecheckt und
   die Datenbanken geschlossen. Keine anderen bekannten Writer existieren.
3. Genau 15 erwartete Dateien werden vollständig erfasst: Konto- und
   Geldbuchdatenbank, Quellenfreigabe sowie je Arbeitsraum Memory-, LTM-,
   Vektor-, Run-, Chatverlaufs- und Cold-Archive-Datei. Eine zweite Inventur
   muss dieselben Bytes liefern. Links, SQLite-Begleitdateien, leere/unerwartete
   Dateien oder überschrittene Grenzen verhindern den Probelauf.
4. Das gemeinsame Abbild wird ohne Kompression mit AES-256-GCM verschlüsselt.
   Der zufällige 32-Byte-Testschlüssel bleibt im Elternprozess; Formatkennung,
   frischer Nonce und Authentifizierungstag sind vom Budget-Produktionsformat
   getrennt. Ein unabhängiges SHA-256-Inventar und ein Ciphertext-Pin bleiben
   beim Testablauf. Kontodaten, Schlüssel und Tokens stehen nicht im Bericht.
5. Die künstlichen Originaldaten werden entfernt. Erst danach wird die
   zurückgelesene verschlüsselte Datei authentifiziert, vollständig gegen das
   Inventar geprüft und in einen **neuen** Ordner zurückgespielt. Vor dem ersten
   Schreiben müssen alle Komponenten und Pfade passen. Bestehende Ziele werden
   nicht ersetzt. Nach dem Schreiben müssen sämtliche Datei-Bytes übereinstimmen.
6. Zwei getrennte, frisch gestartete Prüfprozesse öffnen ausschließlich die
   wiederhergestellten Daten und führen dieselben Funktionsprüfungen aus.

Der Ablauf belegt damit einen zusammengehörigen kalten Datenstand. Er hängt
nicht von einer noch offenen Quellverbindung, einem In-Memory-Cache oder den
ursprünglichen Dateien ab. Er ist kein Nachweis eines laufenden Hot-Backups.

## Funktionale Abnahme

- Beide ursprünglichen Testpasswörter funktionieren; Besitzerrollen und IDs
  stimmen. Kopierte Sessions und Agententokens werden im Test-Restore explizit
  widerrufen und bleiben danach unbrauchbar. Das ist ein Schritt dieses
  Probelaufs, keine neu eingebaute automatische Produkt-Recoveryfunktion.
- Beide Konten sehen ihren eigenen Chatverlauf; der Wechsel in den jeweils
  anderen Mandanten wird über echtes HTTP mit 403 abgewiesen.
- Run-Journal und Verlauf passen zusammen. Kurzzeitspeicher, semantischer
  Volltext, Vektor-ID und vollständiger Inhalt des Cold-Archivs bleiben erhalten.
- Die Quellenfreigabe wird mit unabhängigem Pin geladen und an die
  Besitzer-/Mandanten-/Arbeitsraum-Identität des Piloten gebunden.
- Bestätigte Kosten, volle offene Reservierung, Auftrags-/Versuchszähler und
  aktive Sperre stimmen exakt mit dem ursprünglichen Geldbuch überein.
  Wiederholung der alten Versuch-ID erteilt keine Versandfreigabe. Ein neuer
  Besitzerauftrag bleibt mit 429 blockiert; ein fremder Besitzer erhält 403.
- Beide geschützten Provider-Methoden dürfen nicht aufgerufen werden. Python-
  Audit-Hooks sperren externe DNS-/Socket-Verbindungen; nur der lokale
  HTTP-Testserver ist zugelassen. Dies ersetzt keine Betriebssystem-Netztrennung.

Die Preise, Uhrzeit, Quellen, Konten, Tokens und Antworten sind synthetisch.
Der anfängliche Chat verwendet ausdrücklich Simulation, keine Inferenz.

## Wiederholen

In einer Entwicklungsumgebung mit `requirements-dev.txt`:

```sh
python scripts/cold_recovery_drill.py --report /pfad/zu/neuem-drill-bericht.json
python -m pytest tests/test_cold_recovery_drill.py -q
```

Ohne `--report` erscheint nur das Ergebnis auf stdout. Ein vorhandener Bericht
wird nicht überschrieben. Das Werkzeug hat keine Option für einen produktiven
Quellordner, einen Server, ein Passwort oder einen gespeicherten Schlüssel.
Testdaten, Testschlüssel und Sicherungsdatei werden nach dem Lauf entfernt;
der Ergebnisbericht enthält nur Prüfstatus, Größen und Prüfsummen.

Die Fehlertests weisen falschen Schlüssel/Pin, manipulierten Ciphertext, Nonce
oder Tag, falsches Format, abgeschnittene/erweiterte Hüllen, fehlende Konten,
ein altes Geldbuch, geänderte Quellen, Zusatzdateien und unsichere Pfade vor dem
Anlegen des Zielordners zurück. Ein bestehender Zielordner bleibt unverändert.

## Was noch nicht belegt ist

Der Probelauf gilt für den hier getesteten Core-Datenvertrag. Die getrennte
Konten-/CSV-Arbeit aus PR #102 wird nicht heimlich übernommen und ist nicht
Bestandteil dieses 15-Dateien-Nachweises. Andere Arbeitsräume oder zusätzliche
Dienste müssten ausdrücklich in einen weiteren Datenvertrag aufgenommen werden.

Der echte Server benötigt weiterhin einen geprüften Wartungs-/Writer-Drain,
geschützte Schlüsselaufbewahrung, getrennte Sicherungsablage, Wiederanlaufregeln
und eine Abnahme unter seinen realen Speicherbedingungen. Auch Schutz gegen
veraltete Sicherungen, Stromausfälle und den Wiederanlauf alter Writer ist damit
nicht belegt. Tarif-/Tokenzähler, Providerkonfiguration und externe Pins sind im
Probelauf unabhängige Testvorgaben; reale Konfiguration wird nicht gesichert.

Ein erfolgreicher lokaler oder CI-Probelauf ist kein Produktions-Restore und
keine Freigabe für kostenpflichtige Aufrufe oder den öffentlichen Piloten.
