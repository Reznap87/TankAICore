# Redeploy-Runbook: TankAI-Webdienst auf einem einzelnen Host

**Status:** ausgeführt und verifiziert (zuletzt 2026-11-17).
**Zweck:** nachvollziehbarer, rollbarer Redeploy des Webdienstes auf dem
Single-Host-Betrieb, ohne die bestehende Daten- und Fencing-Verdrahtung zu
treffen.

> Host-spezifische Bezeichner (IP, Volumen-Namen, Container-Namen) sind bewusst
> hier nicht festgehalten. Sie stehen im Betrieb und gehören nicht in das
> öffentliche Repo.

---

## 1. Beobachteter Ist-Zustand

`DEPLOY.md` beschreibt einen **systemd-Dienst** mit Account `tankai` unter
`/opt/tankai`. Der tatsächliche Betrieb weicht davon ab:

- Der Webdienst läuft als **Docker-Container**, nicht als systemd-Unit.
- Der Code liegt im Container unter `/opt/tankai-reviewed` (User `10001:10001`,
  `read_only: true`, `cap_drop ALL`, `no-new-privileges`, `tmpfs /tmp`,
  `pids_limit`, `restart: unless-stopped`, Port nur auf **Loopback**).
- Daten liegen in einem named Volume unter `/app/data` (4 SQLite-Dateien:
  `auth.db`, `memory.db`, `ltm.db`, `usage.db`).
- Der Dienst hängt an einem Compose-Projekt, das auch den lokalen
  OpenAI-kompatiblen Router (`freellmapi`) betreibt. Der Router ist **nicht**
  Cutover-Ziel.
- Image wird **per Digest** referenziert; der Candidate erhält das Label
  `tankai.candidate-context`.
- Seit dem Merge der Budget-Linie (PR #114, `main` @ `c0f3b8d`) ist **`main`
  die Betriebslinie** — `tankai/core/monetary_budget.py` und
  `tankai/core/budget_backup.py` sind Teil von `main`.
- `/api/health` meldet `production_ready=false`,
  `development_queue_enabled=false`,
  `live_model_requests_require_budget=true`,
  `monetary_admission_configured=false`.

**Konsequenz:** Ein Redeploy ist **kein** „`docker compose up -d`" — es müssen
Backup, Rollback und die Cutover-/Volume-Verdrahtung berücksichtigt werden.

## 2. Cutover-Werkzeug

Das Werkzeug liegt **außerhalb dieses Repos** (Betriebs-Workspace). Bestandteile:

- `stage_budget_candidate.py` — baut das Candidate-Image aus
  `git archive <Ziel-Commit> tankai` + gepinnte SDK-Wheels und führt die
  isolierte Budget-/Recovery-Probe aus.
- `budget_runtime_preflight.py` — Inventur von Live-Core, Router und Candidate;
  schreibt das Preflight-Receipt.
- `private_budget_cutover.py` (`prepare`/`launch`/`status`) — erzeugt den Plan
  und startet das Umbuch-Staging auf dem Host.
- `private_budget_cutover_stage.py` — läuft **auf dem Host**: stoppt den alten
  Core, kopiert/verifiziert das Volume, schreibt das AES-GCM-Backup, startet den
  neuen Core, verifiziert, rollt bei Fehler zurück.
- `private_budget_final_verify.py` — unabhängige Nachverifikation (Paket-Hashes,
  Zugänge, Datenstände).
- `live_discovery.py` — **löst Live-Core, Router, Image und Quell-Volume dynamisch
  vom Host auf**. Damit entfällt die frühere manuelle Pin-Pflege vor jedem Lauf.

Reihenfolge: `stage` → `preflight` → `prepare` → `launch` → `final_verify`.

## 3. Sicherheits- und Erfolgs-Gates

- **G0 Backup:** AES-GCM-Snapshot des Volumes, Readback entschlüsselt verifiziert,
  4 DBs integritätsgeprüft (`integrity_check`, `foreign_key_check`).
- **G1 Build/Probe:** isolierte Budget-Gate-Probe + Recovery-Probe im Candidate.
- **G2 Image:** per Digest referenziert; `candidate-context`-Label wird gegen das
  Preflight-Receipt geprüft.
- **G3 Konfiguration:** `runtime_configuration_sha256` des alten Containers muss
  mit dem Preflight-Receipt übereinstimmen (sonst `configuration_changed`).
- **G4 Gesundheit:** `GET /api/health` → `ok=true`, `auth_required=true`,
  `registration_enabled=false`, Budget-Gate unverändert.
- **G5 Isolation:** anonyme `/api/history` und `/api/auth/me` → 401; altes Volume
  bleibt unverändert; kein aktiver HTTP-Client zum Stoppzeitpunkt.
- **G6 Kein Provider-Kontakt:** `provider_request_sent=false`.

## 4. Ablauf

1. **Vorbereitung:** Ziel-Commit festlegen (Regel: `main`). CI muss grün sein.
2. **Candidate bauen:** `stage_budget_candidate.py` → Receipt mit
   `candidate_image`.
3. **Preflight:** `budget_runtime_preflight.py` → Receipt (Live + Candidate).
4. **Plan:** `private_budget_cutover.py prepare` → Plan mit `old_id`,
   `old_volume_source`, `configuration_pin`, `image`, `context_pin`.
5. **Ausführen:** `private_budget_cutover.py launch <plan>` → das Staging auf dem
   Host erledigt Stop, Backup, Copy, Neustart, Verify und Rollback.
6. **Nachweis:** `private_budget_final_verify.py` → Receipt; zusätzlich
   Paket-Verifikation (SHA-256 gegen `git archive <Commit>`).
7. **Freigabe:** den alten Core erst nach positivem Gate löschen; den
   unmittelbaren Vorgänger als Rollback behalten.

## 5. Rollback

- Das Staging rollt **automatisch** zurück (alten Core starten, Candidate
  stoppen), wenn ein Gate fehlschlägt.
- Manuell: alten Core-Container starten, neuen stoppen. Da die Daten im Volume
  liegen, ist der Rollback **datenneutral**, solange G0 nicht in das Live-Volume
  geschrieben hat.

## 6. Nachweis der letzten Ausführung (2026-11-17)

- Ziel: `main` @ `c0f3b8d` (Merge der Budget-Linie, PR #114).
- Candidate `sha256:1e03af00…`; nach dem Umbuch `final_verify` **grün**:
  `reviewed_package_files_match=64`, `canonical_git_blob_content_match=true`,
  `original_account_credentials_preserved=true`, `budget_module_loaded=true`,
  `router_unchanged=true`, `old_core_stopped=true`, `provider_request_sent=false`.
- Health `ok=true`; Budget-Gate unverändert; Datenstände identisch.

## 7. Offene Punkte

- [ ] Unabhängiger Critic: derzeit `TANKAI_CRITIC_LLM` leer,
      `TANKAI_REQUIRE_INDEPENDENT_CRITIC=0`. Ohne Critic gilt
      `verification_ready=false` — bewusst, solange kein zweiter Anbieter bzw.
      zweites Modell hinterlegt ist.
- [x] RAM des Hosts (3809 MiB) liegt unter der in `host_readiness` dokumentierten
      Mindestanforderung (8192 MiB). **Betriebsentscheidung 2026-11-17: akzeptiert** —
      der Dienst läuft im Single-Host-Betrieb stabil; das Readiness-Gate gilt für
      Neuinstallationen, nicht für diesen Host.
- [x] `DEPLOY.md` hatte systemd und Docker ohne Zuordnung beschrieben; mit
      #116 sind die beiden Betriebsvarianten benannt und auf dieses Runbook
      verwiesen.
- [x] Alt-Volumes und gestoppte Core-Container bleiben nach jedem Umbuch liegen.
      Bereinigung über `prune_budget_volumes.py` (Dry-run standardmäßig, `--apply`
      zur Löschung; Volumes mit Container-Bezug werden nie angefasst).

## 8. Definition of Done

Redeploy gilt als erledigt, wenn: Ziel-Commit deployt und Health grün ist, ein
verifiziertes Backup existiert, die Paket-Verifikation die Dateien des
Ziel-Commits bestätigt, und die Doku zum tatsächlichen Betrieb passt.
