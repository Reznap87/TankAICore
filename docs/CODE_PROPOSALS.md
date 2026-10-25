# Begrenzte Codevorschläge von Programmieragenten

Ein Programmieragent kann konkrete UTF-8-Dateiänderungen als `CodeProposal` liefern.
Die neue lokale Vorbereitung übersetzt sie in den bestehenden `WorkerPipelineJob`.
Sie ruft kein Modell auf, ändert keinen Projektzustand, reiht keinen Auftrag ein
und startet keine Ausführung. Die automatische Generierung aus freiem Zieltext
bleibt der nächste separate Adapterbaustein.

Die lokale Request-/Response-Erweiterung bereitet dafür inzwischen einen
vollständigen Auftrag mit Ziel und ausdrücklich ausgewähltem Quellkontext vor.
Eine Antwort eines Programmieragenten kann anschließend an diesen Auftrag
gebunden und in denselben Worker-Job übersetzt werden. Das ist ein lokaler
Datenaustauschvertrag; er richtet keinen Modellzugang ein und versendet keine Daten.

## Vertrag

Das Proposal enthält ausschließlich Version, Task-ID, exakten Basis-Commit und
Dateiänderungen. Berechtigungen, Testbefehle, Gates, Images und Ressourcen stammen
aus dem gespeicherten Auftrag beziehungsweise einem getrennten Operatorvertrag.
Der Vorschlag kann diese Angaben nicht selbst festlegen.

```json
{
  "schema_version": 1,
  "task_id": "CODE-1",
  "base_commit": "REPLACE_WITH_EXACT_40_CHARACTER_COMMIT",
  "files": [
    {
      "path": "src/helper.py",
      "expected_sha256": null,
      "content": "def double(value: int) -> int:\n    return value * 2\n"
    }
  ]
}
```

`null` bedeutet: Diese Datei muss neu sein. Beim Ersetzen ist der SHA-256 der
aktuellen exakten Dateibytes im Worker-Arbeitsbereich erforderlich. Der Empfänger
prüft alle Ausgangsdateien vor dem ersten Schreibvorgang und prüft jede Datei beim
Schreiben erneut. Vorhandene Dateien werden ohne passenden Hash nicht ersetzt.
Einrückung, Unicode und abschließende Zeilenumbrüche bleiben unverändert.

Der erste Vertrag ist bewusst klein: höchstens acht Dateien und insgesamt
8192 Bytes kanonisches Proposal-JSON. Bestehende Dateien sind auf 1 MiB begrenzt.
Löschen, Binärdateien, Symlinks, Junctions, Hardlinks, Git-Metadaten und nicht
portable Pfade gehören nicht zu diesem Vertrag. Dateipfad-Aliase und doppelte
JSON-Schlüssel werden zurückgewiesen.

## Vorbereitung per CLI

Das maschinenlesbare Proposal-Schema ist auch ohne vorhandenen Zustand lesbar:

```bash
python -m tankai.dev_orchestrator.cli code-proposal-schema
```

Ein Owner/Operator stellt `checks.json` im bestehenden Format bereit:

```json
{
  "test_commands": [{"argv": ["python3", "-m", "pytest", "-q"], "timeout_seconds": 30}],
  "gates": {
    "reviewer_agent_id": "EXISTING_REVIEWER_ID",
    "review_commands": [{"argv": ["python3", "-m", "pytest", "-q"], "timeout_seconds": 30}],
    "qa_agent_id": "EXISTING_QA_ID",
    "qa_commands": [{"argv": ["python3", "-m", "pytest", "-q"], "timeout_seconds": 30}]
  },
  "isolation": {
    "image": "tankai-worker@sha256:REPLACE_WITH_APPROVED_DIGEST",
    "memory_mb": 512,
    "cpus": 1,
    "pids_limit": 128,
    "user": "1000:1000"
  }
}
```

Beispielkennungen und Image-Digest durch den tatsächlichen freigegebenen Vertrag
ersetzen. Alle Pflicht-Testbefehle des Tasks und Agenten müssen enthalten sein.
Ein identischer Testlauf unter mehreren Rollen ist kein Nachweis unabhängiger
fachlicher Modellbewertung.

```bash
python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  prepare-code-proposal --agent EXISTING_CODER_ID \
  --proposal proposal.json --checks checks.json --output prepared-job.json
```

Task und zugewiesener Agent müssen aktiv sein und dieselbe Revision wie der
Vorschlag verwenden. Alle Dateien müssen sowohl im Task- als auch im
Agentenschreibbereich liegen. Ausschlüsse haben Vorrang.
Die Ausgabedatei wird exklusiv mit eingeschränktem Dateimodus neu angelegt;
bestehende Dateien und Symlink-Ziele werden nicht überschrieben.

Die Antwort unterscheidet `prepared=true`, `submitted=false` und `executed=false`.
Der exportierte Job ist unverändert mit dem bisherigen `run-pipeline` und der
normalen Queue-Admission verwendbar. Deren Host-/Runtime-, Mandanten-, Image-,
Ressourcen-, Lease-, Review-, QA- und gegebenenfalls Security-Gates bleiben Pflicht.
Die Default-CLI nimmt `python3` im freigegebenen Image an; ein Operator kann den
tatsächlichen Interpreter über `--python-executable` angeben.

## Ausführung und Nachweise

### Ziel und Quellkontext für einen Programmieragenten

```bash
python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  prepare-code-request --agent EXISTING_CODER_ID --repository /srv/project \
  --context-file src/helper.py --context-file tests/test_helper.py \
  --checks checks.json --output code-request.json

python -m tankai.dev_orchestrator.cli code-response-schema
```

Der Auftrag enthält das gespeicherte Ziel, Abnahmekriterien, Pflicht-Testbefehle,
Schreibbereiche, Hashbindungen des Task-/Agentenvertrags und des Operator-Prüfvertrags
sowie den exakten Inhalt der ausgewählten Dateien am Basis-Commit. Es findet weder
eine Verzeichnissuche noch ein Auslesen uncommitteter oder ungetrackter Dateien statt.
Lokale Git-Objekte werden ohne Replace-Refs und mit deaktiviertem Lazy-Fetch gelesen.
Fehlende Objekte verhindern den Export. Quelldateien sind Daten, keine zusätzlichen
Handlungsfreigaben für den Programmieragenten.

`AgentSpec.read_access` muss den Dateipfad über einen `repository/<glob>`-Eintrag
freigeben. Der bestehende Standard `repository/**` umfasst Repositorydateien.
Bekannte Credential- und Laufzeitdateien wie `.env`, private Schlüssel und
Datenbanken werden zusätzlich ausgeschlossen. Das ist kein vollständiger
Inhaltsscanner. Pro Datei gelten 8 KiB, insgesamt 32 KiB Quellkontext und höchstens
acht Dateien; der gesamte Request ist auf 64 KiB begrenzt. Symlinks werden am
Git-Modus erkannt, unabhängig davon, wie das Host-Dateisystem sie auscheckt.

Die Datenklasse ist standardmäßig `internal`. `--data-class public` ist nur eine
Operator-Klassifikation; sie erteilt keine externe Übertragungsfreigabe und ersetzt
keinen Source-Grant oder Kostenvertrag. Das vollständige Request-JSON enthält
Quelltext und benötigt denselben Schutz wie diese Quelldateien.

Die Antwort ist ein einzelnes JSON-Objekt nach `CodeResponse`, mit Version 1,
unverändertem `request_sha256` und einem `proposal` nach dem obigen Vertrag.
Markdown- oder Shell-Anweisungen werden nicht ausgeführt. Die Referenz ist ein
Integritätsbezug, keine Signatur oder neue Autorisierung.

```bash
python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  prepare-code-response --agent EXISTING_CODER_ID --repository /srv/project \
  --request code-request.json --response code-response.json \
  --checks checks.json --output prepared-job.json
```

Vor dem Jobexport werden der aktuelle aktive Task-/Agentenvertrag und die
Operator-Prüfungen mit dem gespeicherten Request verglichen. Die Quelldateien werden
erneut aus dem gebundenen Git-Stand gelesen. Ein anderer Request, geänderte Vorgaben,
manipulierter Kontext oder falsche Ausgangshashes verhindern die Vorbereitung.
Bestehende Dateien dürfen nur ersetzt werden, wenn ihr vollständiger Inhalt im
Request enthalten war; neue Dateien bleiben an die bisherigen Schreibbereiche gebunden.

Die Ausführung verlangt weiterhin exakte Ausgangsbytes. Ein Checkout mit
Zeilenumbruchkonvertierung, Smudge-Filter oder anderer Inhaltsumwandlung kann von
den Git-Blobs abweichen; dann blockiert die bestehende Hashprüfung. Der
Request-/Response-Pfad normalisiert diese Abweichung nicht stillschweigend.
Der Ausführungsarbeitsbereich muss die gebundenen Quellbytes bereitstellen.

### Empfänger und Worker

Die erzeugte Implementierungsanweisung ist ein fester Python-Stdlib-Empfänger.
Das Proposal wird als gebundener Dateninhalt in der vorhandenen Befehlsumgebung
übergeben. Es erzeugt keine modellbestimmten Shell-Kommandos und benötigt keinen
Git-Zugriff im Container. Das bestehende Maskieren der Git-Metadaten bleibt erhalten.
Die Grenze von 8 KiB hält auch diesen Transport für kleine Aufträge begrenzt.

Der Empfänger meldet nur Proposal-Hash und Anzahl geschriebener Dateien. Der
exportierte Job enthält trotzdem den gesamten Quelltext als Base64-Daten und muss
entsprechend seiner Datenklasse geschützt werden; Base64 ist keine Verschlüsselung.
Ein Hash ist ein Integritätsbezug und keine zusätzliche Berechtigung oder Signatur.

Mehrere Dateischreibvorgänge sind keine atomare Gesamttransaktion. Ein I/O-Fehler
oder eine Unterbrechung kann einen teilweise bearbeiteten Worktree hinterlassen.
Dann greifen der Fehlerzustand und die vorhandene Recovery; das Proposal nicht
blind erneut anwenden oder den Arbeitsstand löschen. Der Empfänger ersetzt keine
Sandbox und keinen Schutz gegen fremde gleichzeitig schreibende Hostprozesse.
Untrusted Ausführung bleibt an die bestehende isolierte Runtime gebunden.

Die Regressionen prüfen reale Dateiänderungen und echte Git-Worktrees: Ein
korrekter Vorschlag erreicht die vorhandenen technischen Gates; ein falsches
Ergebnis scheitert am Pflicht-Test und erreicht weder Review noch QA. Queue-Tests
prüfen weiterhin Image-/Isolationsadmission und Idempotenz. Dies ist ein Nachweis
der Übergabe und Ausführung, kein neuer Qualitäts- oder Live-Provider-Receipt für P2.

### Einen unterbrochenen Worker fortsetzen

Neue Worker-Runs speichern einen SHA-256-Bezug auf den vollständigen Job,
den Taskvertrag und die beteiligten Agentenverträge im vorhandenen ProjectState.
Die zusätzliche optionale Run-Angabe benötigt keine zweite Zustandsdatenbank.
Historische Runs ohne diesen Bezug bleiben lesbar und werden für die neue
Wiederaufnahme abgewiesen. Ein erneutes `run-pipeline` ersetzt keinen vorhandenen Run.

Für einen vertrauten lokalen Hostauftrag lässt sich ein unterbrochener Lauf ab
seinem **gespeicherten Implementierungscommit** fortsetzen:

```bash
python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  resume-pipeline --repository /srv/tankai/repository \
  --workspace-root /srv/tankai/worktrees --job prepared-job.json \
  --run RUN-existing-id
```

Der ursprüngliche Executor und seine Kindprozesse müssen beendet sein. Die
Wiederaufnahme verlangt denselben Auftrag, dieselben Tests und Prüfer,
unveränderte Vorgaben, denselben gespeicherten Worktree und einen sauberen,
unveränderten Commit. Die Repository-Zugehörigkeit wird geprüft. Ein pro Agent
gehaltener Betriebssystem-Lock verhindert gleichzeitig laufende lokale Executor;
der Lock wird beim Prozessende freigegeben. Seine Datei bleibt in den Git-Metadaten.

Implementierungsbefehle werden nicht wiederholt. Vor einer bereits gespeicherten
Einreichung laufen die lokalen Worker-Tests erneut. Nach der Einreichung werden
nur die noch offenen technischen Gates ausgeführt; gespeicherte Freigaben
benötigen passende erfolgreiche Befehlsbelege. Ein bereits fertig geprüfter Run
wird nach erneuter Vertrags-/Commitprüfung ohne weitere Ausführung oder
Zustandsänderung zurückgegeben. Run-ID und Implementierungscommit bleiben gleich.

Diese Wiederholung setzt lokale, wiederholbare Prüfbefehle voraus. Sie garantiert
keine Exactly-once-Wirkung für externe Aktionen in solchen Befehlen. Eine
Unterbrechung vor dem gespeicherten Commit, ein fehlgeschlagener/blockierter Run,
abweichender HEAD oder veränderte/unversionierte Dateien führen zum Abbruch.
Auch Prüf-Caches nach einem harten Absturz können deshalb eine manuelle Sichtung
erfordern. Der neue Pfad löscht oder verwirft solche Dateien nicht.

Container-Wiederaufnahme über die Python-Schnittstelle verlangt zusätzlich eine
aktive Ausführungskontrolle für Queue/Fence, die registrierte Repository-ID und
eine erfolgreiche Inventur ohne verbliebenen Container desselben Runs. Verwaiste
Container werden über die vorhandene Recovery geprüft. Das lokale CLI besitzt
diese Queue-Bindung nicht und darf einen unfertigen Container-Run daher nicht
fortsetzen. Ein fertiges Ergebnis bleibt rein lesend abrufbar. Die ursprüngliche
Isolation kann nicht auf Hostausführung herabgesetzt werden. Die Queue-Dispatcher
werden durch diese Ergänzung noch nicht automatisch auf Wiederaufnahme umgestellt.

Die Tests decken mehrere persistierte Übergänge, einen tatsächlichen Prozess-Exit
mit anschließendem CLI-Neustart, unveränderte Run-Identität, konkurrierende
Prozesse, Vertrags-/Dateiänderungen und verweigerte Containerumgehungen ab.
Containerprüfungen dieser neuen Wiederaufnahme verwenden einen Testexecutor;
ein realer Container-Neustart oder ein öffentlicher Dauerbetrieb ist damit noch
nicht belegt. Automatische Inferenz und Reparatur nach fehlgeschlagenen Tests
sind weiterhin eigene Ausbauschritte.
