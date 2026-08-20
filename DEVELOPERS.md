# Entwicklung

Der TankAI-Core liegt vollständig entpackt im Repository. Das frühere
`tankai-project.zip` und die versehentlich eingecheckte Digital-Clocks-Demo
gehören nicht mehr zum aktiven Projektstand.

## Lokale Einrichtung

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m tankai.selftest
```

Der Self-Test verwendet ausschließlich das Mock-LLM und Hashing-Embeddings. Es
sind keine externen API-Schlüssel erforderlich.

## Web-UI

```bash
TANKAI_LLM=mock TANKAI_EMBEDDER=hashing python -m tankai.web.server
```

Die Anwendung ist danach unter `http://127.0.0.1:8765` erreichbar.

## Qualitätsregeln

- Änderungen über Pull Requests gegen `main` einreichen.
- `python -m tankai.selftest` muss erfolgreich durchlaufen.
- Keine `.env`, API-Schlüssel, Datenbanken oder Laufprotokolle committen.
- Architekturänderungen als ADR unter `.tankai/ADR/` dokumentieren.
