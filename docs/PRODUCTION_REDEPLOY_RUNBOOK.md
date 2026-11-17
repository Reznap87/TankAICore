# Redeploy-Runbook: TankAI-Webdienst auf einem einzelnen Host

**Status:** Planung/Runbook — **noch nicht freigegeben für die Ausführung.**
**Zweck:** nachvollziehbarer, rollbarer Redeploy des Webdienstes auf dem
Single-Host-Betrieb, ohne die bestehende Daten- und Fencing-Verdrahtung zu
treffen.

> Host-spezifische Bezeichner (IP, Volumen-Namen, Container-Namen) sind bewusst
> hier nicht festgehalten. Sie stehen im Betrieb und gehören nicht in das
> öffentliche Repo.

---

## 1. Beobachteter Ist-Zustand (Stand der Prüfung)

Die Dokumentation (`DEPLOY.md`) beschreibt einen **systemd-Dienst** mit Account
`tankai` unter `/opt/tankai`. Der tatsächlich beobachtete Betrieb weicht davon ab:

- Der Webdienst läuft als **Docker-Container** (Compose-Projekt eines
  FreeLLMAPI-Testaufbaus), nicht als systemd-Unit.
- Quelle ist ein **lokaler Git-Checkout** dieses Repos mit **uncommitteten
  Änderungen** an `tankai/core/llm.py`, `core/loop.py`, `core/long_term_memory.py`,
  `agents/synthesizer.py` und `docker-compose.freellmapi.yml`.
- Container: nicht-root (`10001:10001`), `read_only: true`, `cap_drop ALL`,
  `no-new-privileges`, `tmpfs /tmp`, `pids_limit`, `restart: unless-stopped`,
  Port nur auf **Loopback**.
- Image wird **per Digest** referenziert.
- Es existieren **zwei** Daten-Volumes und ein Budget-Cutover-Mechanismus
  (Label `tankai.budget-cutover`, `tankai.candidate-context`) sowie ein
  `rollback.py`-Werkzeug außerhalb dieses Repos.
- `/api/health` meldet `production_ready=false`,
  `development_queue_enabled=false`,
  `live_model_requests_require_budget=true`,
  `monetary_admission_configured=false`.

**Konsequenz:** Ein Redeploy ist **kein** „`docker compose up -d`" — es müssen
Backup, Rollback und die Cutover-/Volume-Verdrahtung berücksichtigt werden.

## 2. Vor dem ersten Schritt zwingend klären

1. **Uncommittete lokale Änderungen:** Wollen sie in den Redeploy einfließen
   (Commit/Branch) oder gehen sie verloren? Ohne Klärung **nicht** überschreiben.
2. **Cutover-Werkzeug:** Wer erzeugt Container/Volumes/Labels? Der Ablauf muss von
   derselben Stelle stammen, nicht aus diesem Runbook heraus improvisiert werden.
3. **Daten-Anker:** Wo liegt die authoritative Auth-Datenbank, und wer hält ein
   verifiziertes Backup (Receipt/Hash)?
4. **Ziel-Stand:** Welcher Commit soll deployt werden? (`main` @ `24b9f38` zum
   Prüfzeitpunkt; PRs #108–#112 enthalten die Sicherheitskorrekturen.)

## 3. Sicherheits- und Erfolgs-Gates

- **G0 Backup:** frischer, integritätsgeprüfter Daten-Snapshot; Aufbewahrung
  außerhalb des Live-Volumes.
- **G1 Build:** `python -m compileall -q tankai tests` und `python -m pytest -q`
  auf dem Ziel-Stand (lokal oder in CI).
- **G2 Image:** per Digest referenzieren; Image-ID notieren.
- **G3 Gesundheit:** `GET /api/health` → `ok=true`, erwartete Version,
  `auth_required=true`, `registration_enabled=false`.
- **G4 Rollback-Artefakt:** der bisherige Container/die vorherige Image-ID muss
  schnell wieder startbar sein.
- **G5 Kein Provider-Kontakt:** Redeploy allein darf keine bezahlte Anfrage
   auslösen (`TANKAI_LIVE_PROVIDER_ENABLED`/Budget unverändert).

## 4. Ablauf (Schema — vom Cutover-Werkzeug zu parametrisieren)

1. **Sperrfrist/Kommunikation:** Nutzer:innen informieren (Single-Host).
2. **G0:** Snapshot/Backup der Auth-Datenbank und der übrigen
   `/app/data`-Inhalte; Receipt mit Hash prüfen.
3. **Vorbereitung:** Ziel-Checkout prüfen; lokale Änderungen klären (Frage 1);
   Build und Tests (G1/G2).
4. **Neuen Container** mit **eigenem Namen** und demselben Volume starten;
   Health-Gate G3 abwarten. **Den alten Container nicht sofort löschen.**
5. **Vergleich:** Health-Werte, Auth-Funktionalität (Login möglich), Queue-Zustand
   (`development_queue_enabled` unverändert `false`).
6. **Freigabe:** erst nach positivem Gate den alten Container entfernen; sonst
   **Rollback** (G4): alten Container starten, neuen stoppen.
7. **Dokumentation:** deployte Image-ID, Commit, Zeitpunkt, Receipt in den
   Betriebsnachweis eintragen.

## 5. Rollback

- Stoppen des neuen Containers, Starten des vorherigen Containers (gleiches
  Volume). Da der Datenbestand im Volume liegt, ist der Rollback **datenneutral**,
  solange G0 nicht in das Live-Volume geschrieben hat.
- Health-Check erneut bis `ok=true`.
- Vorherige Image-ID notieren und im Betrieb halten, bis der nächste Gate grün ist.

## 6. Offene Punkte (vor Ausführung zu beantworten)

- [ ] Wer betreibt das Budget-Cutover-Werkzeug, und wie wird es angepasst?
- [ ] Sind die zwei Daten-Volumes A/B oder Rückfällen? Wer entscheidet die Seite?
- [ ] Wie ist der unabhängige Critic zu konfigurieren (derzeit
      `TANKAI_CRITIC_LLM` leer, `TANKAI_REQUIRE_INDEPENDENT_CRITIC=0`)?
- [ ] RAM des Hosts liegt unter der in `host_readiness` dokumentierten
      Mindestanforderung — ist das für den Betrieb akzeptiert oder Teil der
      nächsten Maßnahme?
- [ ] DEPLOY.md beschreibt systemd, real läuft Docker: Doku an den Betrieb
      anpassen (oder Betrieb auf die Doku verlegen).

## 7. Definition of Done

Redeploy gilt als erledigt, wenn: Ziel-Commit deployt und Health grün ist, ein
verifiziertes Backup existiert, der Rollback einmal probeweise durchgesielt
wurde, und die Doku zum tatsächlichen Betrieb passt.
