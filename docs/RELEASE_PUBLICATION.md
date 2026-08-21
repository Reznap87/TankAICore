# Release-Publikation und Connector-Receipts

TankAI `1.8.0-publication-ledger` trennt drei Zustände strikt:

1. **lokal gebaut und geprüft**,
2. **extern hochgeladen**,
3. **vollständig veröffentlicht**.

Eine erfolgreiche lokale ZIP-Prüfung ist kein Beleg dafür, dass dieselben Bytes in Google Drive liegen oder dass derselbe Commit in GitHub veröffentlicht wurde. Das Publikationsledger dokumentiert diese Übergänge mit einer SHA-256-Hashkette.

## Publikationsplan erzeugen

```bash
python -m tankai.dev_orchestrator.publication_cli plan \
  --release-directory /srv/tankai/releases/1.8.0 \
  --ledger /srv/tankai/releases/1.8.0/publication-ledger.json \
  --version 1.8.0-publication-ledger \
  --commit 0123456789abcdef0123456789abcdef01234567 \
  --branch main \
  --drive-target drive-main=GOOGLE_DRIVE_FOLDER_ID \
  --github-target github-main=OWNER/REPOSITORY
```

Der Plan:

- inventarisiert alle vorhandenen Release-Artefakte,
- speichert Dateigröße, MIME-Typ und SHA-256,
- berechnet eine releasebezogene Identität,
- legt Zieltypen und Ziel-IDs fest,
- erzeugt das erste Ereignis der Hashkette,
- speichert keine Zugangsdaten und keine lokalen absoluten Hostpfade.

Das Ledger selbst wird nicht als zu spiegelndes Artefakt aufgenommen. Dadurch entsteht keine zirkuläre Prüfsummenabhängigkeit.

## Google-Drive-Receipt eintragen

Nach dem Upload müssen Remote-ID, Remote-URL, Remote-Größe und eine vom Provider gelieferte Prüfsumme geprüft werden.

```bash
python -m tankai.dev_orchestrator.publication_cli record-artifact \
  --ledger /srv/tankai/releases/1.8.0/publication-ledger.json \
  --release-directory /srv/tankai/releases/1.8.0 \
  --target-id drive-main \
  --artifact tankai-project-1.8.0-publication-ledger.zip \
  --remote-id GOOGLE_DRIVE_FILE_ID \
  --remote-url https://drive.google.com/file/d/GOOGLE_DRIVE_FILE_ID/view \
  --remote-size 123456 \
  --remote-digest-algorithm sha256 \
  --remote-digest REMOTE_SHA256
```

Unterstützte Remote-Digests:

- `sha256`
- `sha1`
- `md5`

`md5` dient hier ausschließlich zur Übertragungsprüfung gegen die von Google Drive gemeldete Dateiprüfsumme. Die lokale Release-Identität bleibt SHA-256-basiert.

Ein Receipt wird abgelehnt, wenn:

- das Ziel nicht im Plan existiert,
- das Artefakt nicht im Plan existiert,
- Größe oder Prüfsumme abweichen,
- die URL nicht zu `drive.google.com` oder `docs.google.com` gehört,
- die Remote-ID nicht in der URL vorkommt,
- bereits ein Receipt für dieselbe Ziel-/Artefaktkombination existiert.

## GitHub-Commit-Receipt eintragen

```bash
python -m tankai.dev_orchestrator.publication_cli record-source \
  --ledger /srv/tankai/releases/1.8.0/publication-ledger.json \
  --target-id github-main \
  --commit 0123456789abcdef0123456789abcdef01234567 \
  --branch main \
  --remote-url https://github.com/OWNER/REPOSITORY/commit/0123456789abcdef0123456789abcdef01234567
```

Das Receipt wird nur akzeptiert, wenn:

- Repository, Branch und Commit exakt dem Plan entsprechen,
- die URL auf `github.com` liegt,
- die URL auf das im Plan festgelegte Repository und den exakten Commit verweist.

## Status und lokale Integrität prüfen

```bash
python -m tankai.dev_orchestrator.publication_cli status \
  --ledger /srv/tankai/releases/1.8.0/publication-ledger.json \
  --release-directory /srv/tankai/releases/1.8.0
```

Die Prüfung umfasst:

- Ledger-Schema,
- Release-ID,
- fortlaufende Ereignisnummern,
- vollständige SHA-256-Hashkette,
- eindeutige Receipts,
- Ziel- und URL-Bindung,
- lokale Dateigrößen und SHA-256-Werte,
- Remote-Receipt-Digests gegen die aktuellen lokalen Dateien,
- Vollständigkeit aller als erforderlich markierten Ziele.

`ok=true` bedeutet: Ledger und lokale Artefakte sind konsistent.

`complete=true` bedeutet zusätzlich: Alle erforderlichen Drive-Artefakte und der geplante GitHub-Commit besitzen gültige Receipts.

## Sicherheitsgrenzen

Die Hashkette erkennt einzelne Änderungen und unterbrochene oder umsortierte Ereignisse. Ein Angreifer mit vollständigem Schreibzugriff könnte jedoch ein komplett neues Ledger erzeugen. Deshalb muss die finale Ledger-Datei zusätzlich:

- mit einer extern gespeicherten SHA-256-Prüfsumme versehen werden,
- in mindestens einem getrennten Speicherziel abgelegt werden,
- zusammen mit dem Release-Commit und dem Upload-Beleg archiviert werden.

Connector-Receipts ersetzen keine kryptografische Signatur des Git-Commits und keine signierte Container- oder Release-Attestation.
