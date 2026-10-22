# Eigenes TankAI-Modell: Entwicklungsprogramm

Stand: 22. Oktober 2026. Ergänzt die vorhandenen Abschnitte 10.1–10.5 zur
TankAI Model Family im Masterplan. Nutzerziel: ein eigenes Modell mit einem
Leistungsanspruch in der Klasse von **GPT-6.1 Sol** und **Kimi K3**.

## Leistungsziel und aktueller Nachweis

Das Ziel umfasst hochwertige allgemeine Antworten, Deutsch/Englisch, Denken,
Repositoryarbeit, sichere Werkzeugnutzung, Kontext über lange Aufgaben und später
multimodale Fähigkeiten. Parität darf erst nach vergleichbaren, unabhängigen
Messungen in den geprüften Bereichen behauptet werden. Ein besserer Router oder
eine umbenannte API ist kein eigener trainierter Modellcheckpoint.

Aktuell existiert kein in diesem Auftrag trainiertes TankAI-Modell. Die vorhandene
Orchestrierung und API-/Mock-Pipeline sind getrennt von eigenen Gewichten zu
bewerten. Der eingefrorene Korpus mit drei Aufgaben hat bisher keine bestandene
fachliche Produktabnahme geliefert; seine Simulation ist keine Referenzleistung
gegen ein Frontier-Modell.

Die offiziellen Quellen bestätigen beide gewünschten Vergleichsmodelle:
[OpenAI: GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
und [MoonshotAI: Kimi K3](https://github.com/MoonshotAI/Kimi-K3).
Moonshot beschreibt Kimi K3 als offenes multimodales MoE-Modell mit 2,8 Billionen
Gesamtparametern und 104 Milliarden aktivierten Parametern. Daraus folgt kein
lokal bestätigter Leistungswert und keine kostenfreie Trainierbarkeit. Öffentliche
Gewichte, Trainingscode, Trainingsdaten und Nutzungsrechte sind getrennte Fragen.

## Stufen und Abschlusskriterien

| Stufe | Konkretes Ergebnis | Abschlussbedingung |
|---|---|---|
| M0 – Messlatte | Versionierte Aufgaben und getrennte Referenzläufe gegen die benannten Modelle. | Identische Aufgaben und Toolrechte; Modellversion, Laufzeit, Versuche, Fehler und Kosten dokumentiert. Referenzaufrufe erst nach Zugang und Kostenfreigabe. |
| M1 – Eigene Baseline | Lokaler, offen lizenzierter Basiskandidat auf tatsächlich vorhandener Hardware. | Nachgewiesene RAM-/VRAM-Eignung, reproduzierbarer Digest, passende Lizenz und vollständiger Evaluationslauf. Keine pauschale Modellauswahl nur nach Parameterzahl. |
| M2 – Eigener Checkpoint | Freigegebener Instruction-/Tool-/Recovery-Korpus und begrenztes SFT-/Adaptertraining. | Datenherkunft und Rechte geklärt, Tests strikt getrennt, Trainingsbudget und Abbruchgrenzen gesetzt, echter gespeicherter Gewichts-/Adapterartefakt mit Hash. |
| M3 – Belegte Verbesserung | Unabhängig bewerteter Vergleich Basismodell gegen TankAI-Checkpoint. | Gewinn in vorab gewählten Bereichen, keine unvertretbare Safety-/Deutsch-/Code-Regression, Kosten pro erfolgreich erledigter Aufgabe ausgewiesen. |
| M4 – Auslieferung | Modellkarte, signierte Artefakte, Servingprofil und Rollback. | Getrennter Inferenzhost, Shadow-/Canary-Abnahme und kontrollierte Rückkehr zur letzten Version. |
| M5 – Breiter Frontier-Vergleich | Größerer unabhängiger Holdout über Denken, Code, Tools, lange Aufgaben und Multimodalität. | Belastbare Ergebnisse je Bereich einschließlich Unsicherheit, Ausfällen und Budget; keine globale Gleichwertigkeit aus einzelnen Demonstrationen ableiten. |

Diese Stufen sind geplant, nicht als abgeschlossen markiert. Der erste eigene
Checkpoint kann auf einem offen lizenzierten Basismodell aufbauen; das gibt uns
eigene überprüfbare Anpassungen und Gewichte. Ein vollständig von null trainiertes
Basismodell bleibt ein gesondertes langfristiges Forschungsprogramm unter dem
vorhandenen Startgate, keine kurzfristige kostenlose Zusage.

## Daten- und Bewertungsvertrag

- Private Gespräche, Serverdateien und Run-Verläufe werden nicht automatisch zu
  Trainingsmaterial. Erforderlich sind ausdrücklich freigegebene Daten,
  dokumentierte Herkunft und eine passende Trainingslizenz.
- Externe Modellantworten sind nur nach Prüfung der jeweiligen Bedingungen und
  Datenrechte als Trainingssignale zulässig. Ein kostenloser API-Zugang erteilt
  diese Rechte nicht automatisch.
- Train, Development und unangetasteter Test-/Holdout-Bestand müssen getrennt
  sein. Deduplikation und Kontaminationsprüfung erfolgen vor Optimierung.
- Korrektheit im Code wird durch Tests und Artefakte bewertet; Werkzeugaktionen
  durch tatsächliche Receipts; offene Antworten durch unabhängige Referenzen und
  verblindete menschliche Prüfung. Selbsteinschätzungen des trainierten Modells
  reichen nicht aus.
- Referenzen werden auf eine konkrete Modellversion und feste Rechte gebunden.
  Rohmodellleistung und Leistung der vollständigen TankAI-Orchestrierung werden
  separat verglichen. Gleiche Antwortqualität bei unbeschränkten zusätzlichen
  Tool-/Modellaufrufen ist kein fairer Rohmodellvergleich.

## Bereits ausführbare Vorbereitung

`evaluation/model_development_v1.json` enthält zwölf eigene synthetische Aufgaben:
je zwei für Denken, Code, Sprache, Quellenbindung, sichere Werkzeugentscheidungen
und Wiederaufnahme. Dieser öffentliche Entwicklungssatz ist vom Training
ausgeschlossen. Er ist **kein geheimer Holdout** und prüft noch keine echte
Werkzeugausführung, große Kontextfenster oder Multimodalität. Diese Prüfungen und
ein unabhängig verwalteter Holdout bleiben für M0/M5 offen.

Die Vorbereitung ist ohne Provider-Zugang und ohne Training ausführbar:

```sh
python -m tankai.evaluation.model_program manifest
python -m tankai.evaluation.model_program prepare-run --model-identity gpt-6.1-sol --track raw_model --output reference-pending.json
```

Das Manifest bindet die Aufgaben einschließlich Rubrik an einen kanonischen
SHA-256-Wert. Änderungen erzeugen einen anderen Wert. Vorbereitete Protokolle
enthalten ausschließlich offene Bewertungen, unbekannte Kosten und den Status
`prepared_not_executed`; sie autorisieren keine Aufrufe und ersetzen keine
vorhandenen Dateien. Modell- und TankAI-Systemvergleich sind getrennte Spuren.
Diese Vorlagen erfassen noch keine unabhängige Durchführung und erzeugen weder
Qualitätsscores noch Gleichwertigkeitsbehauptungen. Die genaue Referenzversion
und Ausführungsrechte sind vor einem späteren Lauf festzuhalten.

## Gemessene Ressourcen und nächste Abnahme

Die reine Leseprüfung vom 22. Oktober ergab lokal rund 16 GB RAM, zwölf logische
CPU-Kerne und Intel Iris Xe; auf dem privaten Server rund 4 GB RAM und zwei
logische Kerne. Auf beiden wurde keine nutzbare NVIDIA-/CUDA-Ausstattung
nachgewiesen. Die Windows-Angabe `AdapterRAM` wird ausdrücklich nicht als
verlässlicher Gesamt-VRAM-Wert benutzt. Freier Speicher ist eine Momentaufnahme,
kein zugewiesenes Trainingsbudget.

Diese Ausstattung ist keine Basis für ein Frontier-Training. Ein kleiner
quantisierter Basiskandidat kann nach Modell-, Lizenz- und Speicherprüfung für
eine lokale erste Inferenzmessung infrage kommen; CPU-/Shared-GPU-Geschwindigkeit
ist erst zu messen. Der Anwendungsserver bleibt vom künftigen Trainings- und
Inferenzhost getrennt. Zeitfenster, rechtmäßig verfügbare Trainingsdaten und
Trainingsbudget sind weiterhin offen. Es wurde keine GPU-Instanz gebucht, kein
Modellgewicht heruntergeladen und kein Training gestartet.

Nächster Gate: geeigneten lizenzierten Basiskandidaten und Ressourcenprofil
festlegen, dann die erste echte Inferenzmessung und einen unabhängigen Holdout
vorbereiten. Kostenfreies API-Routing ist ein paralleler Produktpfad und ersetzt
dieses Modellentwicklungsprogramm nicht.
