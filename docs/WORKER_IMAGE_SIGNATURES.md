# Signierte Worker-Images

Stand: 22. Oktober 2026. Rootless-Queue-Executor prüfen nun vor jedem
Containerstart eine Cosign-Signatur. Die bestehende Image-Allowlist und der
unveränderliche Digest bleiben zusätzlich verbindlich.

## Vertrauen und Ausführung

Der Betreiber setzt `TANKAI_WORKER_IMAGE_TRUST_POLICY` auf eine absolute JSON-Datei
außerhalb aller untrusted Worker-Workspaces. Beispiel einer noch einzurichtenden
Richtlinie; der Binärhash muss zur tatsächlich geprüften lokalen Installation passen:

```json
{
  "schema_version": 1,
  "cosign_binary": "/usr/local/bin/cosign",
  "cosign_sha256": "<SHA256-DER-GEPRUEFTEN-COSIGN-BINAERDATEI>",
  "rules": [{
    "repository": "ghcr.io/reznap87/tankaicore-worker",
    "identity": "https://github.com/Reznap87/TankAICore/.github/workflows/worker-image-sign.yml@refs/heads/main",
    "issuer": "https://token.actions.githubusercontent.com"
  }]
}
```

Datei und Binärprogramm gehören dem Betreiber und dürfen von Jobautoren nicht
veränderbar sein. Die absolute Binärdatei wird vor jedem Aufruf gegen ihren
freigegebenen Hash geprüft. Blatt-Symlinks, Workspace-Pfade, doppelte JSON-Schlüssel,
unbekannte Optionen und mehrdeutige Regeln werden zurückgewiesen. Die übrige
Host-Dateisystem-, Elternpfad- und Runtime-Integrität bleibt Betreiberverantwortung.

Ein Job liefert ausschließlich seine OCI-Referenz mit Digest. Er kann weder
Signierer, Trustroot, Prüfer noch Abschaltoptionen auswählen. Cosign erhält eine
exakte Zertifikatsidentität und einen exakten OIDC-Aussteller; keine Wildcard und
keine abgeschaltete Claim-, TLS- oder Transparenzprüfung. Der Adapter prüft
zusätzlich die JSON-Claims gegen Repository und Digest. Er unterstützt die
bekannten alten Cosign-Claims sowie das aktuelle `cosign/sign/v1`-Format.
Fehler, leere/ungültige Ausgabe, fehlende Freigabe, manipulierte Binärdatei,
Zeitüberschreitung oder Lease-Verlust blockieren den Start.

Signaturen werden nicht gecacht: Entzug einer Betreiberfreigabe gilt beim nächsten
Schritt. Das Prüfen unterliegt einer 45-Sekunden-Grenze und dem Lease-Guard.
Provider-Zugangsdaten, Docker-Auth, OIDC-Tokens und Proxy-/TLS-Overrides des
Worker-Prozesses werden nicht übernommen. Ein isoliertes temporäres Home verhindert
das Einlesen von Workspace-Konfiguration. Dieser Pfad unterstützt damit derzeit
öffentlich lesbare Registries; private Registry-Anmeldung benötigt eine gesonderte
begrenzte Read-only-Credential-Lösung. Netzwerkzugriff auf Registry und Sigstore ist
auf dem Runner nötig; der ausgeführte Worker-Container bleibt `--network none`.

`require_rootless=True` erzwingt Signaturen auch dann, wenn ein Aufrufer
`require_image_signatures=False` setzt. Bestehende lokale Entwicklungs-Executor
ohne Rootless-Pflicht behalten ihre Kompatibilität; dort ist
`require_image_signatures=True` ausdrücklich zu setzen. Ein solcher lokaler
Entwicklungspfad ist keine produktive Queue-Freigabe. Der Runtime-Doctor prüft
weiterhin das Laufzeitsicherheitsprofil, keine Signatur ohne konkrete Image-Referenz.

## Eigenes Image bauen und signieren

Der neue Workflow **Build and sign reviewed worker image** ist ausschließlich
manuell und auf `main` ausführbar. Er verlangt den exakten geprüften Commit,
ein per Digest gebundenes Python-Basisimage und eine ausdrückliche Release-Eingabe.
Er baut das Worker-Image, veröffentlicht es in GHCR, signiert dessen exakten Digest
mit GitHub OIDC und verifiziert ihn unmittelbar mit der erwarteten Workflowidentität.
Verwendete Actions und die Cosign-Version sind fixiert. Der Job erhält nur die
notwendigen Lese-, Package-Schreib- und OIDC-Rechte; Pull Requests veröffentlichen
keine Images.

Anschließend müssen Betreiber-Richtlinie, öffentlich lesbare Registry-Freigabe
und Workspace-Image-Allowlist nach Review zu diesem Digest passen. Eine Signatur
ist kein Beweis für fehlerfreien oder harmlosen Code; Review, Tests, Ressourcen-,
Mount-, Netzwerk- und Fence-Grenzen bleiben aktiv.

Der Workflow wurde in diesem Auftrag vorbereitet, aber **nicht ausgelöst**.
Es wurde kein neues TankAI-Worker-Image veröffentlicht oder als produktiv signiert
behauptet. Die private Development-Queue bleibt deaktiviert. Zur Betriebsabnahme
gehören ein eigener signierter Release-Digest und eine Probe auf einem dedizierten
Rootless-Runner mit echter Betreiber-Richtlinie.

## Nachweise

Offline-Tests decken korrekte CLI-Parameter, isolierte Umgebung, alte und neue
Claims, falsches Repository/Digest, unzulässige Referenzen, Binary-Manipulation,
ungültige Richtlinien, Timeouts, Lease-Verlust, Entzug einer Freigabe und das
Nichtstarten eines Containers bei fehlender Signatur ab.

Zusätzlich wurde Cosign 3.1.4 von der offiziellen Veröffentlichung geladen und
gegen deren veröffentlichte SHA-256-Prüfsumme kontrolliert. Mit dem echten
Cosign-Programm und TankAIs Adapter wurde das öffentlich signierte Cosign-Image
geprüft. Signierer und Aussteller entsprechen der offiziellen
Sigstore-Installationsdokumentation. Das ist ein echter Signaturnachweis für
dieses Referenzimage, kein Nachweis eines eigenen veröffentlichten TankAI-Images.

Primärquellen:

- [Signaturprüfung und Digest-Claims](https://docs.sigstore.dev/cosign/verifying/verify/)
- [Installation und offizielle Sigstore-Identität](https://docs.sigstore.dev/cosign/system_config/installation/)
- [Cosign-Versionen](https://github.com/sigstore/cosign/releases)
- [Signieren von Containern](https://docs.sigstore.dev/cosign/signing/signing_with_containers/)
