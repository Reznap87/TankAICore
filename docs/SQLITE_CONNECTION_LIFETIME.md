# Deterministischer Abschluss kurzlebiger SQLite-Verbindungen

Kontenspeicher, Development-Queue und externe Lease-Fences öffneten bisher je
Operation eine SQLite-Verbindung. `with connection` bestätigte oder verwarf
Transaktionen, schloss aber die Verbindung nicht. Das ist das dokumentierte
[Verhalten des Python-SQLite-Kontextmanagers](https://docs.python.org/3.12/library/sqlite3.html#how-to-use-the-connection-context-manager).
Datei-Handles und WAL-Ressourcen blieben bis zur Speicherbereinigung offen.
Unter Windows verhinderte dies unter anderem das Entfernen der temporären
Fence-Datenbanken am Ende des vollständigen Self-Tests.

Die drei internen `_connect()`-Funktionen sind jetzt Kontextmanager mit
explizitem `finally: connection.close()`. Die bestehende SQLite-Behandlung für
Commit und Rollback bleibt innerhalb dieses Abschlusses erhalten. Auch ein
Fehler beim Setzen der PRAGMAs schließt die bereits geöffnete Verbindung.
Jede bestehende Projektverwendung dieser privaten Funktionen erfolgt innerhalb
eines `with`-Blocks; es wird keine dauerhafte Verbindung zurückgegeben.

Die Änderung migriert keine Daten, ersetzt keine Datenbank und erneuert weder
Budgets noch Auftrags-/Lease-Freigaben. Die vorhandenen SQLite-Transaktionen,
Session-/Workspace-Bindungen, monotone Fence-Epochen und Vergleiche von
Lease-Token bleiben bestehen. Länger lebende Memory- und Geldbuchverbindungen
verwenden weiterhin ihren eigenen expliziten Abschlussvertrag.

`tests/test_sqlite_connection_lifetime.py` hält alle Testverbindungen absichtlich
als Python-Objekte am Leben, damit zufällige Garbage-Collection keinen fehlenden
Abschluss verdeckt. Vor der Korrektur scheiterten alle 13 Fälle. Sie prüfen
Schemaanlage, bestätigte Transaktionen, Rollback bei Fehlern, PRAGMA-Fehler vor
Kontexteintritt und tatsächliches Umbenennen/Entfernen einer Fence-Datenbank
bei weiterhin existierendem Store-Objekt. Der vollständige Windows-Self-Test
bestand nach der Korrektur einschließlich Development-Queue und Lease-Fencing.

Die Auth-Snapshot-Negativtests schließen jetzt auch ihre selbst manipulierten
SQLite-Verbindungen vor dem Lesen der Hauptdatei. Sonst konnten die absichtlich
erzeugten Views/Trigger noch ausschließlich im WAL stehen und der Test versandte
versehentlich eine ältere, kompatible Hauptdatei.

Beim anschließenden lokalen Produktionscontainer-Smoke-Test wurde außerdem eine
Windows-zu-Linux-Stdin-Abweichung reproduziert: `--password-stdin` entfernte nur
LF und behielt CR als unerlaubtes Randzeichen des künstlichen Passworts.
Die CLI entfernt jetzt genau einen Zeilenabschluss (LF oder CRLF), ohne führende
oder abschließende Leerzeichen des Passworts zu trimmen. Sieben Tests prüfen die
Zeilenenden, erhaltene Leerzeichen sowie Kontoanlage und Passwortwechsel samt
Widerruf alter Sessions mit ausschließlich synthetischen Zugangsdaten.

Linux-/Container-, Worker- und Web-Regressionen sind weiterhin am konkreten
Commit zu prüfen. Dieser lokale Ressourcenabschluss ist keine globale Writer-
Sperre, kein produktiver Restore und keine Freigabe für einen Deploy.
