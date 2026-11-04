# Gemeinsamer Checkpoint des geschlossenen Besitzerpiloten

Dieser Entwurf verbindet Konten, Budget, Quellenfreigaben und lokale
Workspace-Daten in **einer authentifizierten Generation**. Er ist eine lokale
Betreiberfunktion und keine HTTP-Route, Migration oder Produktionsfreigabe.
`production_ready`, `dispatch_authorized` und die Aktualitätsbestätigung bleiben
false. Der Restore erzeugt ausschließlich einen neuen, für den Web-Start
gesperrten Prüfstand. Eine Aktivierungsfunktion ist bewusst nicht enthalten.

## Unterstützter Bereich

Ein neuer verwalteter Datenordner, eine Web-Anwendung, lokales Dateisystem,
Session-Anmeldung, Hashing-Embedder mit 384 Dimensionen, lokale Konten und ein
gemeinsames Geldbuch. Der Export verlangt den beendeten Web-Prozess bzw. dessen
vollständig abgeschlossenen `AppContext.close()` nach HTTP-Drain. Die Anwendung
und der Export nehmen dieselbe exklusive, nicht wartende Betriebssystem-Sperre.
Ein zweiter beteiligter Web-Prozess wird ebenfalls abgewiesen.

Unter Windows wird `msvcrt.locking(..., LK_NBLCK, 1)` verwendet, unter POSIX
`flock(..., LOCK_EX | LOCK_NB)`. Die Verfahren entsprechen der
[Python-Windows-Dokumentation](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)
und der [Python-POSIX-Dokumentation](https://docs.python.org/3/library/fcntl.html#fcntl.flock).
Sie gelten für teilnehmende Prozesse und lokale Dateisysteme. Alte Programmstände,
direkte Datenbank-/Dateischreiber, Administratoren und andere Hosts werden
dadurch nicht global gesperrt. Es gibt keine verteilten Fencing-Token.

Erfasste Dateien:

- `auth.db`: Konten, Passwort-Hashes, Rollen, Mitgliedschaften, Sessions,
  Agent-Zugänge und Audit-/Rate-Daten des vorhandenen Schemas.
- `monetary.sqlite`: vollständiges bestehendes Geldbuch inklusive aktiver
  Aufträge, unbekannter Versuche, Reservierungen, Quoten und Preisstopps.
- `sources/<workspace-uuid>.json`: alle gespeicherten Quellenfreigaben, mit
  unabhängig vorgegebenen SHA-256-Pins und passender Besitzer-/Mandantenbindung.
- `tenants/<tenant-uuid>/workspaces/<workspace-uuid>/`: `memory.db`, `ltm.db`,
  optional vorhandene `vectors.npz`, `runs.jsonl`, `web_history.jsonl` und
  `cold/memory_archive.jsonl`. Existierende Workspace-Dateien benötigen beide
  Datenbanken. Leere zulässige Verzeichnisse werden ebenfalls erfasst.

Unbekannte Dateien oder Verzeichnisse, CSV-/Development-Queue-Zustände und
ausgelagerte Speicher werden abgewiesen. Diese Version integriert PR #102 nicht
still in ihren Vertrag. Modellgewichte, Provider-Schlüssel, `.env`, Uploads und
Cloudflare-Zustände gehören nicht in diesen Datenordner. Das Verfahren ist kein
Backup solcher Ressourcen. Bestehende unverwaltete Ordner werden nicht adoptiert
und behalten ihre bisherige Startkonfiguration.

## Betreiber-API

Die Funktionen liegen in `tankai.web.application_checkpoint`, die Wurzelverwaltung
in `tankai.web.application_root`. Es gibt keine automatische Suche, Erzeugung oder
Änderung produktiver Schlüssel. `requirements-backup.txt` stellt die optionale
AES-GCM-Abhängigkeit bereit; gewöhnlicher Web-Start benötigt sie nicht.

```python
from pathlib import Path
from tankai.web.application_root import initialize_root
from tankai.web.application_checkpoint import (
    create_checkpoint, verify_checkpoint, restore_checkpoint,
)

# Nur für einen noch nicht existierenden, separat vorbereiteten Pilotordner.
metadata = initialize_root(Path("/private/new-pilot-data"))
# Danach Konten, Geldbuch und geprüfte Quellen im unterstützten Layout einrichten.
# TANKAI_DATA_ROOT darauf setzen; TANKAI_AUTH_DB muss dort auth.db sein.
# Nach Ende der beteiligten Web-Anwendung:
trusted = dict(
    expected_root_id=metadata["root_id"],
    expected_policy=independently_approved_policy,  # vorhandenes PilotPolicy-Objekt
    expected_source_pins=independently_approved_source_pins,
)
receipt = create_checkpoint(
    Path("/private/new-pilot-data"), Path("/private/backups/generation.enc"),
    operator_key_32_bytes, **trusted,
)
# Den zurückgegebenen encrypted_sha256 separat und vertrauenswürdig festhalten.
verify_checkpoint(
    Path("/private/backups/generation.enc"), operator_key_32_bytes,
    expected_sha256=independently_pinned_encrypted_digest, **trusted,
)
restored = restore_checkpoint(
    Path("/private/backups/generation.enc"), Path("/private/new-restore-inspection"),
    operator_key_32_bytes,
    expected_sha256=independently_pinned_encrypted_digest, **trusted,
)
assert restored["state"] == "staged-restore"
assert restored["web_startup_authorized"] is False
```

Die Platzhalter sind absichtlich keine automatisch freigegebenen Vorgaben.
Verzeichnisse müssen privat und gegen fremde lokale Schreiber geschützt sein.
Root-ID, komplette Budgetpolitik, Quellenpins, Schlüssel und verschlüsselter
Dateipin müssen aus unabhängig freigegebener Betreiberkonfiguration kommen.
Ein Hash neben einer beliebigen Sicherungsdatei beweist diese Freigabe nicht.

## Erfassung und Wiederherstellung

Nach Erwerb der Root-Sperre werden **alle** erfassten SQLite-Dateien gleichzeitig
mit `BEGIN IMMEDIATE` gegen weitere SQL-Schreibtransaktionen reserviert.
Separate lesende Verbindungen erzeugen begrenzte SQLite-Backup-Abbilder samt
bestätigten WAL-Seiten. Die Abbilder werden als eigenständige Datenbanken
normalisiert. Das kann den normalen SQLite-WAL-Checkpoint der Quelle auslösen;
es ist keine Zusage identischer physischer Hauptdateibytes. Die logischen
Quellkonten, Zugänge, Historien und Geldzustände werden nicht zurückgesetzt.

Bestandsliste, reguläre Dateitypen, Linkzahl, Datei-/Verzeichnisidentitäten und
Nicht-SQLite-Inhalte werden vor Abschluss erneut geprüft. Links, Junctions,
Hardlink-Aliase, unbekannte Dateien und verwaiste Sidecars verhindern den Export.
SQLite-Struktur, vorhandenes Auth-Schema, vollständiger Geldbuchvertrag,
Workspace-Tabellen/-Spalten, Zuordnungen, Quellenfreigaben und JSONL-Objekte
werden validiert. NPZ wird ohne Pickle und erst nach ZIP-/NPY-Größen- und
Shape-Prüfung geladen (höchstens 4096 Vektoren je Workspace). Diese Prüfung ersetzt keine inhaltliche Qualitätsbewertung
gespeicherter Erinnerungen oder Antworten.

Der AES-256-GCM-Umschlag authentifiziert Root-ID, Budgetpolitik und Quellenpins
als Kontext. Ein frischer Nonce, Generation-ID, UTC-Zeit und eine gemeinsame
Dateiliste mit Größen und SHA-256 gehören zum Format. Begrenzungen: 256
Nutzdateien, 8 MiB je Datei, 32 MiB Nutzinhalt, 46 MiB verschlüsselte Datei.
Die dauerhaft gespeicherte Sicherung enthält Ciphertext; private temporäre
SQLite-Kopien werden bei der Validierung verwendet. Die Sicherung wird exklusiv
neu angelegt, synchronisiert, erneut gelesen, entschlüsselt und validiert.

Der Restore validiert vollständig vor der ersten Zielanlage. Das Ziel muss neu
sein; existierende Ordner werden nicht zusammengeführt oder ersetzt. Ein
Pending-Marker sperrt einen abgebrochenen Stand. Dateien werden exklusiv erzeugt,
synchronisiert und einschließlich Identitäten und Sidecar-Abwesenheit erneut
geprüft. Auf POSIX werden auch die neuen Verzeichniseinträge synchronisiert;
Windows-Verzeichnismetadaten-Dauerhaftigkeit bleibt wie im Geldbuch-Backup eine
Dateisystem-/Betreibergrenze. Es gibt keinen bewiesenen Schutz gegen Stromausfall
auf diesem PC oder Dateimanipulation nach dem abschließenden Abgleich.

Alle alten Sessions und Agent-Token werden ausschließlich in der restaurierten
Kopie widerrufen. Konten und Passwort-Hashes bleiben für eine spätere erneute
Anmeldung erhalten. Die abweichende restaurierte Auth-Datei erhält einen eigenen
Hash im Receipt. Das Geldbuch wird unverändert übernommen: keine Freigabe
unbekannter Kosten, kein Auftragabschluss, keine Verlängerung und kein erneuter
Provider-Versand. Der Restore erhält eine neue Root-ID und `staged-restore`;
`AppContext.from_env` verweigert seinen Web-Start.

## Nachweise und offene Betriebsarbeit

`tests/test_application_checkpoint.py` verwendet reale HTTP-Anfragen, zwei
synthetische Mandanten, SQLite, Betriebssystem-Sperren und AES-GCM. Ein frischer
Interpreter prüft die restaurierten Konten, Workspace-Isolation, Erinnerungen,
Vektoren, kalten Archive und Historien sowie Replay-Sperre und unbekannte
Geldreservierung. Weitere Fälle prüfen Prozessgrenzen, WAL, laufende
Schreibtransaktionen, Manipulation, falsche Freigaben, unvollständige Bestände,
Nichtüberschreiben und einen blockierten Teil-Restore. Es werden keine echten
Provider kontaktiert.

Noch erforderlich vor realem Umschalten: Linux-/Container-CI des konkreten
Commits, private persistente Zielhost-Speicherung, gesondert verwahrter
Wiederherstellungsschlüssel und Snapshot-Pin, zeitlich vollständiger Nachweis
aller Schreiber, Klärung späterer/unsicherer Aktivitäten und ein expliziter
Cutover-Vertrag mit Host-Abnahme. Der lokale Dateipin beweist keine Aktualität
und verhindert kein absichtliches Zurückspielen eines alten gültigen Checkpoints.
