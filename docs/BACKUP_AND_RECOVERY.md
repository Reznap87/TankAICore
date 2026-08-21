# Backup und Wiederherstellung

## Ziel

TankAI Core erzeugt reproduzierbare, prüfbare Quellcode-Snapshots. Laufzeitdaten,
Zugangsdaten, Datenbanken, Vektordateien, Git-Metadaten und Caches werden bewusst
nicht in ein öffentliches Release-Backup aufgenommen.

## Backup erstellen

```bash
python -m tankai.dev_orchestrator.release_cli build \
  --project-root . \
  --output-directory ../tankai-release \
  --version 1.7.0-release-backup
```

Erzeugt werden:

- `tankai-core-<version>.zip`
- `tankai-core-<version>.backup.json`
- `tankai-core-<version>.manifest.sha256`
- `tankai-core-<version>.SHA256SUMS`

## Backup prüfen

```bash
python -m tankai.dev_orchestrator.release_cli verify \
  --archive ../tankai-release/tankai-core-1.7.0-release-backup.zip \
  --checksums ../tankai-release/tankai-core-1.7.0-release-backup.SHA256SUMS
```

Die Prüfung validiert ZIP-Pfade, Symlinks, interne Metadaten, Dateianzahl,
Byteanzahl und jede SHA-256-Prüfsumme.

## GitHub

Die Workflows unter `.github/workflows/` führen Compile, Pytest und Self-Test aus.
Der manuelle Workflow `TankAI Core Release Backup` erzeugt anschließend die
geprüften Backup-Artefakte und speichert sie für 90 Tage als GitHub-Artefakt.

Ein GitHub-Repository muss dem verwendeten GitHub-Connector Schreibzugriff
erteilen. Ohne ein autorisiertes Repository kann TankAI keine Commits oder
Dateien zu GitHub übertragen.

## Google Drive

Für Drive sollen mindestens ZIP, externe Metadaten, Manifest und SHA256SUMS im
gleichen Release-Ordner gespeichert werden. Nach dem Upload müssen Dateigrößen
und SHA-256-Werte mit der lokalen `SHA256SUMS`-Datei verglichen werden.

## Nicht im Release-Backup

- `.env` und andere Secret-Dateien
- SQLite-Datenbanken und WAL-Dateien
- `legacy-global-state/`, `.tankai/`, `tenants/`, `data/`
- Vektor- und Run-Dateien (`*.npz`, `*.jsonl`)
- `.git/`, virtuelle Umgebungen und Caches

Diese Daten benötigen eine getrennte, verschlüsselte Betriebsdatensicherung mit
eigenem Aufbewahrungs- und Wiederherstellungskonzept.

## Publikationsnachweis

Ab Version `1.8.0-publication-ledger` wird nach dem lokalen Backup ein getrenntes Publikationsledger verwendet. Es unterscheidet zwischen lokal verifizierten Artefakten, tatsächlich nach Google Drive übertragenen Dateien und einem tatsächlich in GitHub vorhandenen Commit.

Die vollständige Bedienung und die Sicherheitsgrenzen stehen in [`RELEASE_PUBLICATION.md`](RELEASE_PUBLICATION.md).
