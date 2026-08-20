# ADR 0002: Quellcode entpackt im Repository

Date: 2026-08-20
Status: accepted
Supersedes: ADR 0001

## Context

Der Projektcode war nur als ZIP-Archiv vorhanden. Zusätzlich hatten ein
Platzhalter-Upload und eine fachfremde Digital-Clocks-Demo den sichtbaren
Repository-Stand verfälscht. Dadurch waren Code-Suche, Tests und CI nicht
zuverlässig möglich.

## Decision

Der wiederhergestellte TankAI-Core v0.5 wird vollständig entpackt verwaltet.
Generierte Datenbanken, Laufprotokolle, lokale Umgebungsdateien und
Self-Test-Ergebnisse werden über `.gitignore` ausgeschlossen. GitHub Actions
führt Kompilierung und Self-Test für Pull Requests aus.

## Consequences

- Quellcode ist direkt review- und testbar.
- Das ZIP-Archiv, der Platzhalter und die Digital-Clocks-Demo werden entfernt.
- Künftige Änderungen erfolgen als nachvollziehbare Pull Requests.
