# Offline-Vorbereitung des Container-Snapshot-Piloten

Dieser separate Pilot bereitet das geordnete Anhalten für die native Cloudflare
Container API vor. Er ändert weder `src/cloudflare.ts`, `wrangler.jsonc` noch den
produktiven `Dockerfile`. Es gibt noch keinen nativen Snapshot-Controller und
keinen Cloudflare-Restore-Nachweis. Keine neue Domain, Ressource oder Secrets
sind für die lokalen Tests nötig.

## Warum ein Supervisor?

Cloudflare verlangt einen **laufenden Container** für `snapshotContainer({})`.
Der Snapshot enthält das beschreibbare Dateisystem, keine Prozesszustände und
keine gemounteten Dateisysteme. Würde der bisherige Webprozess als PID 1 beendet,
wäre auch der Container beendet. Der Pilot hält deshalb einen kleinen Supervisor
als PID 1 am Leben, während sein einziger Web-Unterprozess geordnet beendet wird.

Der neue Supervisor ist ausschließlich für einen isolierten Linux-Piloten mit
synthetischen Daten vorgesehen. Eine frische Root wird exklusiv unter
`/app/data/pilot` angelegt. Es gibt keine Übernahme bestehender Produktivdaten.
Der Webprozess bekommt eine feste Umgebung: Mock-Modelle, Hashing-Embedder,
Session-Authentifizierung, Registrierung und Entwicklungsqueue aus. Ambient
Provider-, Bootstrap-, Auth-State- und Proxy-Konfiguration wird nicht vererbt.
Das Image kopiert keine `.env`. **Dem Container selbst ebenfalls keine Secrets
übergeben:** Ein Dateisystem-Snapshot ist kein Secret-Management-Verfahren.

## Zustände und lokale Steuerung

| Zustand | Webprozess | Root-Lease | `snapshot_ready` |
| --- | --- | --- | --- |
| `running` | aktiv, Health geprüft | beim Webprozess | false |
| `quiescing` | SIGTERM; Drain läuft | beim Webprozess | false |
| `quiesced` | Exit 0, vollständig geerntet | beim Supervisor | true |
| `inspect` | wird nicht gestartet | beim Supervisor | false |
| `blocked` | kein automatischer Neustart | soweit erlangt behalten | false oder Fehler |

`quiesce` bestätigt erst, nachdem der konkrete Kindprozess mit Exit 0 beendet
ist, der Supervisor dieselbe vorhandene `RootLease` erwerben konnte und ein
begrenzter Digest der Root-Dateien erstellt wurde. Timeout, Nichtnull-Exit,
fremde Lease, Links, übergroße Inventare oder Änderungen nach dem Versiegeln
verhindern eine Erfolgsbestätigung. Ein Wiederholen nach erfolgreichem Quiesce
liest denselben Zustand erneut. Ein verlorener Client startet nichts neu.

Der Steuerkanal ist ein Unix-Socket in einem Verzeichnis mit Modus 0700; der
Socket hat Modus 0600. Eine exklusive OS-Sperre schützt vor einem zweiten
Supervisor und erlaubt das Entfernen eines alten Sockets nach einem Restore.
Es gibt nur `status` und `quiesce`, keine HTTP-Steuerroute, beliebigen Befehle,
Aktivierung oder Resume-Funktion. Der Betreiber erreicht ihn per Container-Exec:

```bash
python -m tankai.web.pilot_supervisor status
python -m tankai.web.pilot_supervisor quiesce
```

Ein späterer nativer Controller muss den Erfolgs-Receipt **vor** der Aufnahme
an den Snapshot-Handle und die Image-Version binden. Auf einer Kopie startet
standardmäßig `inspect`. Dieser Modus hält die Lease, liest den Root-Digest und
öffnet keinen HTTP-Port. Er attestiert nicht, wie die Kopie entstanden ist.
Der Controller muss Root-ID und Digest gegen den unabhängig gespeicherten
Aufnahme-Receipt vergleichen. `fresh` verweigert jede bereits vorhandene Root,
auch nach einem gewöhnlichen Container-Neustart. Ein Absturz führt daher nicht
zur automatischen Wiederaufnahme oder zum Anlegen einer leeren Ersatzdatenbank.

## Lokaler Lauf ohne Netzwerk oder Provider

```bash
docker build -f Dockerfile.pilot -t tankai-pilot:local .
python .github/scripts/pilot_container_smoke.py \
  --image tankai-pilot:local --scope local-pilot
```

Der Test startet unprivilegierte Container ohne Netzwerk, Ports, Mounts oder
Credentials. Er prüft echte Web-Readiness und Drain, fortbestehenden Supervisor,
versiegelte Root, eine Dateisystemkopie mit `docker commit`, identischen Digest
im Inspect-Modus sowie gesperrte Fresh- und Neustartversuche. Er entfernt nur
seine mit demselben Scope markierten Container und abgeleiteten Images. Der
CI-Workflow führt diesen Test zusätzlich zum unveränderten Produktions-Smoke aus.

Die Python-Tests prüfen außerdem einen echten Kindprozess, der erst **nach**
SIGTERM eine SQLite-Transaktion committed und dann seine Lease freigibt. Damit
ist belegt, dass die Bestätigung nicht vor dem letzten Commit erfolgt.

## Verbleibende Grenze zum Livebetrieb

Dies belegt einen lokalen Ablauf auf einem kooperativ gesperrten Dateisystem.
Die Lease sperrt keine fremden Raw-SQLite-Clients oder Administratoren. Der Digest
ist auf 256 Einträge und 32 MiB Root-Daten begrenzt, kein vollständiger
Container- oder semantischer Datenbank-Validator und kein Freshness-/Anti-Rollback-
Nachweis. Eine Wiederherstellung darf insbesondere unbekannte monetäre Holds
nicht freigeben; dafür gelten weiterhin die bestehenden Checkpoint-Verträge.

Noch erforderlich sind ein getrennter Durable-Object-Controller mit persistenter
Zustandsmaschine und Zugriffsschutz, native Aufnahme/Wiederherstellung im
berechtigten Cloudflare-Pilotkonto, Bindung von Image und Root-Receipt, Behandlung
unterbrochener API-Aufrufe, Snapshot-Aufbewahrung sowie ausdrücklich freigegebene
Aktivierung und Cutover. Ein `inspect`-Receipt allein erfüllt diese Gates nicht.
Für dauerhafte verschlüsselte Exporte besteht unabhängig davon der vorhandene
Application-Checkpoint-Pfad; dieser Supervisor ersetzt ihn nicht.

Primärquellen:

- [Native Container API](https://developers.cloudflare.com/containers/api/durable-object-container/)
- [Snapshots und Aufbewahrung](https://developers.cloudflare.com/containers/guides/snapshots/)
- [Migration zur nativen API](https://developers.cloudflare.com/containers/guides/migrate-to-durable-object-container-api/)
- [Getrennte Migration zur DO-Scheduling-Policy](https://developers.cloudflare.com/containers/guides/migrate-to-durable-object-scheduling-policy/)
