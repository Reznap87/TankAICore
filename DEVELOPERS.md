Diese Datei erklärt, wie man als Entwickler*in mit dem aktuellen Repository arbeitet, solange der Quellcode als Archiv vorliegt.

Voraussetzungen
- Git, unzip, eine aktuelle Go/Python/Node-Umgebung falls benötigt (abhängig vom Projektinhalt).

Schnelleinrichtung
1. Repository klonen:
   git clone https://github.com/Reznap87/TankAICore.git
2. Archiv entpacken:
   unzip tankai-project.zip -d tankai-project
3. Ins Projektverzeichnis wechseln und Abhängigkeiten installieren (siehe README in entpacktem Projekt).

Architektur- und Governance-Dokumentation
- Alle Architekturgrundlagen befinden sich in .tankai/

Code-Qualität und Tests
- Ziel: Automatisierte Tests, Linter und Sicherheits-Scans via CI (GitHub Actions). Noch nicht implementiert.

Contribution
- Bitte PRs gegen main. Verwende Templates in .tankai/TEMPLATES/.
