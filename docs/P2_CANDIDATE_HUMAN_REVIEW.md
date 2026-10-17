# P2 Candidate: unabhängige fachliche Prüfung

Der providerfreie P2-Candidate ist technisch reproduzierbar, aber fachlich noch nicht
freigegeben. Dieses Verfahren erzeugt eine lokale, manipulationsgebundene Arbeitsvorlage für
die acht menschlich zu bewertenden Kriterien des eingefrorenen Korpus. Es ändert weder den
Candidate noch die Baseline und aktiviert keinen Provider.

## Vorlage erzeugen

Das Ziel muss ein neuer Dateipfad sein. Vorhandene Dateien und Symlinks werden ohne
Überschreiben abgewiesen (Exitcode 2); auch eine bereits ausgefüllte Bewertung bleibt
dadurch erhalten. Für eine neue Vorlage einen anderen Dateinamen verwenden. Die exklusive
Dateierstellung verhindert auch ein Überschreiben bei gleichzeitigem Export.

```bash
python -m tankai.evaluation.cooperative_acceptance \
  --candidate evaluation/cooperative_acceptance_v1_candidate.json \
  --write-human-review-template /sicherer/lokaler/pfad/p2-review.json
```

Die Vorlage enthält die vollständigen Fallverträge, bereitgestellten Artefakte und exakten
Candidate-Ausgaben. Der Dokumentanalyse-Fall ist als `internal` klassifiziert. Die Datei darf
daher nur an einem dafür freigegebenen lokalen Speicherort abgelegt und nicht an externe
Dienste übertragen werden.

Eine unabhängige Person trägt eine stabile `review_id`, ihre `reviewer_identity`, einen
UTC-Zeitpunkt in `reviewed_at` und für jedes offene Kriterium `passed` oder `failed` mit einer
konkreten Begründung in `evidence` ein. Danach bestätigt sie
`independent_reviewer_attested=true`. Die Bestätigung ist eine prüfbare Erklärung im Receipt,
keine kryptografische Signatur und kein Ersatz für organisatorische Zugriffskontrollen.

## Abgeschlossenes Review prüfen

```bash
python -m tankai.evaluation.cooperative_acceptance \
  --candidate evaluation/cooperative_acceptance_v1_candidate.json \
  --human-review /sicherer/lokaler/pfad/p2-review.json
```

Die Prüfung schlägt geschlossen fehl, wenn Korpus, Source-Revision, Fallvertrag, Ausgabe,
Artefakt-Hash, Kriterienbeschreibung oder Gewicht verändert wurden. Ebenso abgewiesen werden
offene Urteile, fehlende Begründungen, unvollständige Kriterien, eine fehlende
Unabhängigkeitsbestätigung oder das Candidate-System selbst als Prüfer.
Leere Prüferkennungen (auch reine Leerzeichen) sind ungültig. Beim Vergleich mit der
Systemkennung werden Rand-Leerzeichen entfernt und Groß-/Kleinschreibung per Unicode
Casefold ignoriert. Das ist keine Identitätsverifikation: Andere Aliasnamen lassen sich
damit nicht erkennen; organisatorische Unabhängigkeit bleibt separat sicherzustellen.

Ein gültiges Review aktualisiert ausschließlich die menschlichen Kriterien im ausgegebenen
Vergleichs-Receipt. Automatische Vertragskriterien bleiben unverändert. Auch ein positives
Review macht den providerfreien Simulations-Candidate nicht automatisch pilotfähig;
`pilot_eligible` bleibt ohne die übrigen P2-/P0-Nachweise `false`.

Das Candidate-Feld `human_reviewed=true` ist selbst kein Prüfbeleg. Ohne zusätzliches,
vollständig validiertes `--human-review` wird dafür kein Vergleichs-Receipt ausgegeben.
Selbst deklarierte bestandene Human-Kriterien werden bei vorhandenem Review ausschließlich
durch dessen gebundene Urteile ersetzt. Ein unveränderter, noch nicht geprüfter Candidate
mit `human_reviewed=false` bleibt wie bisher ohne Review auswertbar.
