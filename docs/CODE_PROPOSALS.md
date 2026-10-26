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

### Gebundene Erstantwort direkt in die Queue einreichen

Owner und Admin können den ursprünglichen Codeauftrag, die Agentenantwort und
den unveränderten Operator-Prüfvertrag gemeinsam einreichen. Der lokale
Zwischenexport eines allgemeinen Worker-Jobs entfällt für diesen Pfad:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue.db --auth-db /srv/tankai/auth.db \
  --repository-base /srv/repositories --workspace-base /srv/worktrees \
  --state-base /srv/states enqueue-code-response \
  --actor-email owner@example.invalid --workspace-id EXISTING_WORKSPACE \
  --repository-id EXISTING_REPOSITORY --request code-request.json \
  --response code-response.json --checks checks.json
```

`checks.json` ist derselbe vertrauenswürdige Prüfvertrag wie beim Requestexport.
Die Antwort bleibt das begrenzte `CodeResponse`-Objekt und kann keine eigenen
Prüfungen oder Ausführungsbefehle hinzufügen. Nur Owner/Admin dürfen diese
lokale Operator-Schnittstelle verwenden, auch wenn die allgemeine Queue-Policy
Mitglieder für andere Jobs zulässt. Die Ausgabe nennt Job-ID, Requesthash und
Replaystatus; Quelltext und vollständiger Payload werden nicht ausgegeben.

Die Queue speichert einen versionierten internen `code_response`-Umschlag in
ihrem bestehenden `payload_json`. Der vorhandene Payloadhash bindet auch den
Request, die Antwort und die Operator-Prüfungen. Die SQL-Struktur und die
externen Gateway-v1-Formate bleiben unverändert; es entsteht kein zusätzlicher
HTTP-Endpunkt und keine neue Tokenberechtigung.

Die Identität aus Repository, Task, Agent und Basiscommit wird serverseitig
gebildet. Identische gleichzeitige Einreichungen durch verschiedene Admins
ergeben denselben Job. Andere Antworten oder Prüfverträge für dieselbe Identität
werden als Konflikt abgewiesen. Ein Replay kann den vorhandenen fertigen oder
fehlgeschlagenen Job zurückgeben; es startet keine neue Implementierung.
Aktuelle Zugriffsrechte und die Queue-Policy gelten auch für den Replayzugriff.

Beim Einreihen entstehen keine Worktrees oder Worker-Runs und der ProjectState
wird nicht verändert. Unter der Worker-Lease werden Berechtigung und Admission
erneut geprüft. Vor dem Pipeline-Start, der Workspace-Vorbereitung und dem
Anlegen des Runs wird die Antwort aus dem aktuellen Task-/Agentenvertrag,
bestätigten Projektcommit, Git-Kontext und Prüfvertrag erneut kompiliert. Ihr
Pipelinehash muss dem gespeicherten Job entsprechen. Eigene Dispatcher-Callbacks
dürfen diesen Pfad nicht ausführen; er nutzt den bestehenden Runner mit
verpflichtender Containerisolation und dessen Lease-/Fence-Kontrollen.
Die letzte Startprüfung läuft innerhalb derselben ProjectState-Transaktion,
die den Run speichert. Auch Änderungen während der Workspace-Bindung werden
dadurch vor dem Start eines Befehls abgewiesen.

Eine Erstantwort benötigt einen Task ohne bereits begonnenen Worker-Run.
Nach einer Unterbrechung mit gespeichertem Run wird sie nicht blind neu
implementiert. Dafür gelten die vorhandenen expliziten Wiederaufnahme- oder
Korrekturpfade; ein automatisches Recovery für diesen Erstantwort-Umschlag ist
noch nicht implementiert. Die neue Übergabe führt keine Inferenz aus und
übermittelt keine Daten an einen Modellprovider. Automatische Antworterzeugung,
Source-Grants und monetäre Admission bleiben eigenständige Voraussetzungen.

Der reale rootless-CI-Test reicht auch die absichtlich fehlerhafte Erstantwort
über diesen Umschlag ein, beobachtet ihren Worker-Testfehler und prüft das
idempotente Replay. Der anschließende Korrektur-/Neustarttest bleibt erhalten.
Der Receipt kennzeichnet diese zusätzlichen Prüfungen ausdrücklich mit
`initial_code_response_queued` und `initial_duplicate_same_job`.

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
unveränderten Commit. Bereits gespeicherte Implementierungs-, Test-, Review-,
Security- und QA-Belege müssen vollständig erfolgreich sein und exakt in der
ursprünglichen Befehlsreihenfolge vorliegen; zusätzliche oder umsortierte Belege
werden abgewiesen. Die Repository-Zugehörigkeit wird geprüft. Ein pro Agent
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

### Begrenzte Korrektur nach einem fehlgeschlagenen Worker-Test

`prepare-code-repair-request` erstellt einen eigenen gebundenen Korrekturauftrag
aus einem aktuellen blockierten Run. Voraussetzung sind ein gespeicherter
Implementierungscommit, vollständig bestandene Implementierungsbefehle und ein
konkret fehlgeschlagener Worker-Test. Runtime-/Integritätsfehler, offene oder
fehlgeschlagene spätere Gates und alte Runs ohne Vertragsfingerprint sind kein
Korrektureinstieg. Der bestätigte Task-Basisstand muss weiterhin aktuell sein.

```bash
python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  prepare-code-repair-request --repository /srv/tankai/repository \
  --previous-job failed-job.json --run RUN-failed-id \
  --edit-file src/service.py --context-file src/service.py \
  --context-file tests/test_service.py --output repair-request.json
```

Die editierbaren Dateien werden ausdrücklich ausgewählt und müssen bereits im
fehlgeschlagenen Commit geändert worden sein. Sie müssen vollständig im begrenzten
Quellkontext stehen. Zusätzliche Test-/Kontextdateien bleiben unveränderbar.
Read-Access, geschützte Dateipfade und Größenlimits folgen dem normalen Request.
Der Quellkontext stammt aus `context_commit`, dem fehlgeschlagenen Commit.
`base_commit` bleibt die ursprüngliche bestätigte Task-Basis. Die Agentenantwort
verwendet diese Task-Basis und die Ausgangshashes des fehlgeschlagenen Codes.

Das Feedback nennt Testbefehl, Index und Exitcode sowie Hash/Bytezahl der
gespeicherten Ausgabe. Rohe Testausgaben werden nicht in den Korrekturauftrag
kopiert. Benötigte Testquellen müssen ausdrücklich als lesbarer Kontext
freigegeben werden. Die ursprünglichen Prüfbelege bleiben im ProjectState.

Eine Agentenantwort verwendet weiterhin das bestehende `CodeResponse`-Format.
Sie bindet sich an den Hash des Korrekturauftrags. Die drei nächsten Schritte
bleiben bewusst getrennt:

```bash
python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  prepare-code-repair-response --repository /srv/tankai/repository \
  --previous-job failed-job.json --request repair-request.json \
  --response repair-response.json --output repair-job.json

python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  authorize-code-repair --repository /srv/tankai/repository \
  --workspace-root /srv/tankai/worktrees --previous-job failed-job.json \
  --request repair-request.json --response repair-response.json

python -m tankai.dev_orchestrator.cli --state .tankai/project-state.json \
  run-pipeline --repository /srv/tankai/repository \
  --workspace-root /srv/tankai/worktrees --job repair-job.json
```

Bei einem abweichenden Python-Interpreter denselben `--python-executable`-Wert
für Vorbereitung und Reservierung verwenden. Exportieren allein verändert
keinen Zustand. `authorize-code-repair` reserviert nach erneuter Prüfung des
sauberen Worktrees atomar einen von **höchstens zwei** Korrekturversuchen pro
Task und öffnet den bestehenden Task über dessen bisherigen Rework-Vertrag.
Eine zwischenzeitliche Zustandsänderung verhindert eine Teilreservierung.
Ein bereits reservierter identischer Auftrag verbraucht keinen weiteren Versuch;
nach dem Start liefert sein erneuter Aufruf nur den gespeicherten Run-Bezug zurück.

Tests, Gates und Isolation stammen unverändert aus dem früheren Job. Der neue
Worker prüft den reservierten Job- und Vertragsfingerprint sowie den exakten
Startcommit vor der Ausführung. Er übernimmt die Reservierung in seinen eigenen
Run und bewahrt den vorherigen blockierten Run. Auch ein nach der Reservierung
fehlgeschlagener Start verbraucht den reservierten Versuch; es gibt keine
automatische Erstattung oder unbegrenzte Wiederholung. Die vollständige kumulative
Dateiliste des neuen Commits bleibt für Tests, Review und Wiederaufnahme gespeichert.

Die neue Aktivierung ist ein lokaler Operatorpfad für vertraute Hostaufträge.
Containeraufträge können als Korrektur vorbereitet werden; das lokale
`authorize-code-repair` weist sie weiterhin ab. Die Aktivierung erfolgt über den
unten beschriebenen Queue-Worker. Die Isolation wird nicht herabgesetzt. Queue-Admission und bestehende
Gateway-v1-Formate bleiben unverändert. Es gibt weiterhin keinen automatischen
Modellaufruf, keine Datenübertragung und keinen eigenständig antwortenden
Reparaturagenten. Korrekturantworten liefert ein ausdrücklich beteiligter Agent.

### Containerkorrektur über die bestehende Queue

Ein Workspace-Owner oder -Admin kann denselben Korrekturauftrag mit dem bestehenden
Queue-CLI einreihen. Repository-, Worktree- und State-Pfade stammen aus der zuvor
registrierten Repository-Bindung, nicht aus der Agentenantwort:

```bash
python -m tankai.dev_orchestrator.queue_cli \
  --queue-db /srv/tankai/queue.db --auth-db /srv/tankai/auth.db \
  --repository-base /srv/tankai/repositories \
  --workspace-base /srv/tankai/worktrees --state-base /srv/tankai/states \
  enqueue-code-repair --actor-email owner@example.com \
  --workspace-id WORKSPACE-ID --repository-id REPOSITORY-ID \
  --previous-job failed-job.json --request repair-request.json \
  --response repair-response.json --python-executable python3
```

Die normale Admission prüft freigegebenes Image, Container-Isolation, RAM, CPU,
PIDs, Laufzeit, Queue-Kapazität und Nutzerrate. Auch Metadaten des früheren Jobs
dürfen keine Inline-Credential-Umgebungsvariablen enthalten. Ein neuer, intern
versionierter Queue-Umschlag speichert Auftrag, Antwort, bisherigen Job und den
daraus abgeleiteten Worker-Job im vorhandenen `payload_json`. Die Prüfsumme bindet
den gesamten Umschlag. Normale Queue-Jobs und ihre bisherigen Prüfsummen behalten
ihr Format; es gibt keine zusätzliche Zustandsdatenbank oder SQL-Schemamigration.
Gateway v1 erhält dadurch keinen neuen Web-Endpunkt.

Der Idempotenzschlüssel wird aus Repository, Eltern-Run und Korrekturversuch
abgeleitet. Derselbe Inhalt liefert auch bei einem anderen berechtigten Admin
denselben Queue-Job. Eine andere Antwort für denselben Versuch wird abgewiesen.
Einreichen allein verändert weder Taskstatus noch Korrekturzähler. Der CLI-Beleg
enthält Kennungen und Status; er gibt keinen vollständigen Quellkontext aus.

Der Worker benötigt für Korrekturen ebenfalls `--auth-db`. Nach aktueller Lease,
externem Fence und Heartbeat prüft er die Berechtigung des Einreichers und die
Admission erneut. Die rootless Container-Runtime und eine Inventur ohne verbliebene
verwaltete Repository-Container sind Pflicht. Vorhandene Container werden nicht
automatisch entfernt. Erst dann reserviert der Worker unter dem bestehenden
Agentenlock den Korrekturversuch atomar; der Fence wird innerhalb dieser
Zustandstransaktion noch einmal geprüft.

Die Reservierung enthält die Queue-Job-ID. Andere Queue-Jobs oder das lokale
Pipeline-CLI können sie nicht übernehmen. Der Dispatcher verwendet ausschließlich
den kontrollierten, containerpflichtigen Worker-Runner. Ein alternativer
Ausführungs-Callback darf diesen Pfad nicht ersetzen. Alle Ausführungsphasen
bleiben an den aktuellen Fence und die vorhandene Abbruchkontrolle gebunden.

Nach einer Unterbrechung verwendet eine neue Lease desselben Jobs die bestehende
Reservierung erneut. Ein bereits gestarteter Run wird ab seinem gespeicherten
Commit fortgesetzt. Ein fertig geprüfter Run kann das noch offene Queue-Ergebnis
liefern, ohne seine Implementierung erneut auszuführen. Neue Lease-Versuche und
Codekorrekturversuche bleiben getrennte Zähler mit ihren jeweiligen Limits.
Ein Abbruch vor einem sicher gespeicherten Implementierungscommit wird weiterhin
abgewiesen; lokale Änderungen bleiben erhalten. Fehlgeschlagene Vorbedingungen
werden als Queue-Fehler gespeichert und benötigen die entsprechende vorhandene
Operator-Recovery, statt eine neue Antwort oder Freigabe zu erfinden.

Die Regressionen führen reale Git-, State-, Auth-, Queue- und Fence-Abläufe mit
einem aufzeichnenden Container-Testexecutor aus. Sie decken Wiederöffnung der
Datenbanken, Unterbrechungen nach Reservierung/Commit und vor Queue-Abschluss,
Rollenentzug, Payload-Manipulation, verwaiste Container und verlorene Fences ab.
Diese Prüfungen sind kein Nachweis echter rootless Container-Korrektur oder eines
öffentlichen Dauerbetriebs. Modellzugang und automatische Korrekturantworten
bleiben eigenständige Integrationsschritte.

### Reale rootless Abnahme in CI

Der separate CI-Job `rootless-worker` verwendet auf einem Ubuntu-24.04-Runner
den installierten Podman als unprivilegierter Hostbenutzer. Er baut das vorhandene
`Dockerfile.worker` auf einem beim Pull aufgelösten Python-Basisdigest und fixiert
den fertigen Worker anschließend auf seine unveränderliche Image-ID. Runtime-
Profil und tatsächlich verwendete Image-Identitäten stehen im Prüfbeleg.

Rootless Podman bildet den Host-Dateieigentümer über
`--userns=keep-id:uid=<Worker-UID>,gid=<Worker-GID>` auf den konfigurierten
Nicht-root-Benutzer im Container ab. Hostdateien werden nicht rekursiv umgechownt.
Die bestehende Schreibbereichsauswahl und die unveränderbaren Mounts bleiben
erhalten. Die lokale Podman-Version wird über `Client.Version` gelesen;
`Server.Version` bleibt für Docker vorgesehen. Siehe die offiziellen
[Podman-UID-Mappings](https://docs.podman.io/en/v4.9.3/markdown/podman-run.1.html#userns-mode)
und [Versionsfelder](https://docs.podman.io/en/v4.9.3/markdown/podman-version.1.html).

Der Live-Smoke nutzt den normalen Dispatcher mit echtem Container-Executor,
eine private synthetische Auth-/Queue-/Fence-/Git-Umgebung und feste vier
Unit-Tests. Er verlangt zunächst einen echten Testfehler und anschließend eine
Korrektur. Ein Worker-Prozess endet nach gespeichertem Implementierungscommit;
erst nach beobachtetem Prozessende und leerer Containerinventur lässt der Test
seine private Fixture-Lease ablaufen. Ein neuer Prozess muss denselben Run und
Commit mit weiter genau einem Korrekturversuch erfolgreich abschließen.

Ein technischer Review-Befehl prüft im echten Container Nicht-root-UID/GID,
fehlende Capabilities, `NoNewPrivs`, schreibgeschütztes Root-Dateisystem,
maskierte Git-Metadaten, abwesende Hostumgebung, alleinige Loopback-Schnittstelle,
256 MiB Speicherlimit, 64 PIDs, ein CPU-Kontingent und 1024 offene Dateien.
Testdateien und README dürfen nicht schreibbar sein. Alle Schutzprüfungen sind
Pflicht; ein fehlender Controller oder ein ignoriertes Limit ergibt keinen Pass.

Nur ein erfolgreicher `rootless-worker`-Lauf mit dem Artefakt
`tankai-rootless-worker-receipt` gilt als reale Abnahme dieses Pfads. Der Beleg
enthält den geprüften Commit, Runtime, Images, tatsächliche Schutzwerte,
Queue-/Run-Kennungen, Wiederanlauf und unveränderte Testhashes. Cleanup entfernt
ausschließlich verwaltete Container mit der in der privaten Fixture gespeicherten
Repository-ID. Es gibt keinen globalen Prune und kein rekursives Löschen von
Hostverzeichnissen. Der Test veröffentlicht keine Anwendung und ruft kein Modell auf.
