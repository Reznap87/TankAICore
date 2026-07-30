Architekturentscheidungsprotokoll 0001

Titel: Quellcode als Archiv (tankai-project.zip) im Repository
Status: accepted
Datum: 2026-07-30

Kontext
Der gesamte Projekt-Quellcode wird aktuell als ZIP-Archiv im Repo gespeichert. Das verhindert einfache Code-Suche, CI-Integration und inkrementelle PRs.

Entscheidung
Solange das Archiv vorhanden ist, legen wir eine Architekturgrundlage (dokumente, ADRs, Templates) im Repository ab, dokumentieren den Ist-Zustand und fordern auf, das Archiv zeitnah in entpackte Dateien zu überführen (ein Commit pro relevanter Komponente).

Folgen
- Kurzfristig: Dokumentation und Templates ermöglichen klare nächste Schritte.
- Mittelfristig: Archiv entpacken, CI/CD hinzufügen, modulare PRs.
