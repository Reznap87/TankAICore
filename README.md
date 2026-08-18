# Digital Clocks — Mehrere Zeitzonen (Single File)

Kurzanleitung
1. Speichere `digital-clocks.html` lokal.
2. Öffne die Datei im Browser (Doppelklick oder `npx http-server`).
3. Funktionen:
   - Suche + Dropdown zur Auswahl von IANA‑Zeitzonen.
   - Eigene Zeitzone hinzufügen.
   - Drag&Drop + ▲/▼ Buttons zum Umordnen (Reihenfolge wird in localStorage gespeichert).
   - Tastatur: Karte auswählen (Tab) → `J`/`K` oder Pfeil `↑`/`↓` zum Verschieben.
   - Export / Import (JSON) und Copy/Paste der Konfiguration.
   - 12h/24h Umschalter und Theme Toggle (Dunkel/Hell).
   - Sekundenanzeige (kleiner pulsierender Punkt).
4. Persistenz: localStorage keys:
   - `digital-clocks.zones`
   - `digital-clocks.hour12`
   - `digital-clocks.theme`

Hosting
- GitHub Pages: lege das HTML in ein Repository (z. B. `index.html`) und aktiviere Pages auf dem `main` branch → die Seite ist online.
- Alternativ lokal: `npx http-server` oder `python -m http.server 8000`.

Barrierefreiheit
- Karten sind fokussierbar (`tabindex="0"`).
- Drag‑Handle ist als Button zugänglich und hat ARIA‑Labels.
- Tastatursteuerung (J/K / Pfeile) zum Umordnen.

Anpassungen / Erweiterungen
- Reorder via touch gestures, i18n, serverseitige Persistenz (API), oder GitHub Pages Deployment help? Sag kurz welche Erweiterung du möchtest.

Lizenz
- MIT (siehe LICENSE)
