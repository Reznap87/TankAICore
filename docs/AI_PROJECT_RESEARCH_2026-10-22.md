# GitHub-Recherche: kostenlose Zugänge und konkrete Entwicklungsschritte

Stand: 22. Oktober 2026. Öffentliche Repositories wurden über den GitHub-Connector
gelesen; Quoten und Verfügbarkeit wurden an offiziellen Anbieterseiten geprüft.
Es wurden keine Konten angelegt, Provider aktiviert, Zahlungsarten geändert oder
Modellabfragen zur Erkundung versendet. Kostenlose Zugänge sind begrenzte
Kontingente, keine belastbare Zusage unbegrenzter Rechenleistung.

## Die wichtigsten Ergebnisse

1. Für einen zusätzlichen kostenlosen Testzugang zuerst **Groq Free** prüfen.
   Ein separates Konto mit bestätigtem Free-Status und Modellfreigabe ist nötig.
2. **Cloudflare Workers AI Free** ist ein sinnvoller zweiter Testpfad, wenn das
   richtige Konto, der Free-Plan und ein dort erlaubtes Modell bestätigt sind.
3. **OpenRouter Free** ist ein weiterer Vergleichspfad; gemeinsam genutzte
   Kontingente, Fallbacks und Datenrichtlinien müssen explizit begrenzt werden.
4. **Ollama/llama.cpp** sind der erste Kandidat für eine eigene Inferenzstrecke
   und später eigene TankAI-Checkpoints. Hardware und Energie bleiben Kosten.
5. Für Produktfortschritt haben vergleichbare Qualitätsläufe und fortsetzbare
   Aufgaben Vorrang vor dem bloßen Anschluss immer weiterer Router.

## Anbieter und aktueller Status

| Zugang | An offizieller Quelle festgestellt | Konsequenz für TankAI |
|---|---|---|
| Groq Free | Modellabhängige Free-Limits; beispielsweise GPT-OSS-120B mit 30 Anfragen/min, 1000/Tag und 200.000 Tokens/Tag. Grenzen gelten organisationsweit; tatsächliche Kontogrenzen können abweichen. | Kleines, kontobezogenes Budget einrichten; nicht als Kontingent pro Schlüssel oder pro Nutzer addieren. |
| Cloudflare Workers AI | 10.000 Neurons pro Tag kostenlos. Im Free-Plan ist für Mehrverbrauch ein Upgrade nötig. Im Paid-Plan können Mehrkosten entstehen; einige Modelle benötigen grundsätzlich Bezahlung. | Nur bestätigter Free-Plan und erlaubte Modelle sind ein kostenloser Testpfad. Neurons sind keine Tokens. |
| OpenRouter Free | Die Preisseite nennt 50 Anfragen/Tag für Free; die API-Dokumentation beschreibt kontoweite Free-Zähler und zusätzliche Upstream-Limits. | Tagesrest vor längeren Aufgaben prüfen; ausschließlich bestätigte Free-Varianten und keine bezahlten Fallbacks. |
| Gemini API Free | Die Preistabelle unterscheidet je Modell Free und Paid; nicht jede Funktion steht kostenlos bereit. Datenverwendung und Zusatzwerkzeuge haben eigene Bedingungen. | Eigens bestätigtes Free-Projekt, erlaubte Modelle und Datenfreigabe nötig; kein allgemeines Gratisversprechen. |
| Mistral Free mode | Offizielle aktuelle Dokumentation bestätigt enthaltene Nutzung innerhalb der im Konto sichtbaren Limits. Sie belegt kein universelles Kontingent für jedes Modell. | Als bedingten Kandidaten behandeln, erst nach konkreter Kontofreigabe einplanen. |
| Hugging Face Inference Providers | Die frisch geöffnete offizielle Preisseite nennt für Free-Konten **keine** monatlichen Credits mehr. Ältere Suchtreffer und Listen nennen noch 0,10 USD. | Als Gratisquelle streichen; vorhandenes separates Guthaben wäre gesondert zu prüfen. |
| GitHub Models | Laut offizieller Dokumentation seit 30. Juli 2026 vollständig eingestellt. | Alte Gratis-Token-Empfehlungen hierfür nicht übernehmen. |
| AI Horde | Freiwillige Rechenleistung mit Queue und Kudos; anonyme Anfragen sind möglich, haben aber geringe Priorität. Fremde Worker können Prompts und Ergebnisse einsehen. | Nur öffentliche, unkritische Experimente; ungeeignet als Standardpfad für private Projekte. |

Quellen zu den Zeilen: [Groq-Limits](https://console.groq.com/docs/rate-limits),
[Workers-AI-Preise](https://developers.cloudflare.com/workers-ai/platform/pricing/),
[OpenRouter-Preise](https://openrouter.ai/pricing/),
[OpenRouter-Limits](https://openrouter.ai/docs/api_reference/limits),
[Gemini-Preise](https://ai.google.dev/gemini-api/docs/pricing),
[Mistral-Nutzung](https://docs.mistral.ai/admin/billing-usage/usage-limits),
[Hugging-Face-Preise](https://huggingface.co/docs/inference-providers/pricing),
[GitHub-Models-Einstellung](https://docs.github.com/en/github-models),
[AI-Horde-FAQ](https://github.com/Haidra-Org/AI-Horde/blob/main/FAQ.md).

## GitHub-Projekte und übernehmbare Möglichkeiten

Die letzte Spalte ist eine begründete Empfehlung für TankAI, keine bereits
implementierte Integration oder unabhängig bestätigte Leistungsbewertung.

| Projekt | Nützlicher Ansatz | Konkreter Einsatz bei TankAI |
|---|---|---|
| [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) | Bereits vorhandene Bündelung von Modellzugängen. | Vorhandenen Router weiter kontrollieren; tatsächliche Fallbacks und gemeinsame Kontingente offenlegen, statt beworbene Free-Mengen blind zu summieren. |
| [LiteLLM](https://github.com/BerriAI/litellm) | Einheitliche API, Routing, Cooldowns und Quoten über mehrere Deployments. | Quotenpool, Retry-After und gesundheitsabhängiges Routing als Entwurfsvorlage. Ein zusätzlicher kompletter Router erhöht zunächst die Komplexität. |
| [Ollama](https://github.com/ollama/ollama) | Lokale Modellverwaltung und Inferenz-API. | Einen eigenen kleinen, offen lizenzierten Baseline-Kandidaten messen; später Adapter-/Modellcheckpoint bereitstellen. Keine neue GPU-Miete ohne Budget. |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | Quantisierte Inferenz und kompatibler Server auf mehreren Hardwareklassen. | RAM-, Latenz- und Qualitätsprofil für eine ressourcenschonende lokale TankAI-Variante prüfen. |
| [vLLM](https://github.com/vllm-project/vllm) | Batching, KV-/Prefix-Caching und parallele Inferenz. | Für einen getrennten Inferenzhost bei nachgewiesener Last untersuchen. Keine notwendige Zusatzkomponente des heutigen kleinen Control-Servers. |
| [LangGraph](https://github.com/langchain-ai/langgraph) | Dauerhafte Zustände und Wiederaufnahme langer Agentenabläufe. | Chat-Schritte mit Checkpoints, idempotenten Seiteneffekten und Abbruch weiterentwickeln; bestehende Orchestrierung gezielt erweitern. |
| [Promptfoo](https://github.com/promptfoo/promptfoo) | Wiederholbare Modell-/Promptvergleiche und CI-Evaluation. | Den vorhandenen eingefrorenen Korpus für echte Kandidatenvergleichsläufe und Regressionen nutzen; Bewertungskosten ebenfalls begrenzen. |
| [Letta](https://github.com/letta-ai/letta) | Zustandsbehaftete Agenten mit Gedächtnis. | Provenienz, Trennung von Erinnerung und verifiziertem Wissen sowie Konsolidierung vergleichen. Das Hauptrepository verweist inzwischen auf [letta-code](https://github.com/letta-ai/letta-code); alte V1-Anleitungen sind historisch. |
| [Sigstore/Cosign](https://github.com/sigstore/cosign) | Signieren und Identitätsprüfung von Softwareartefakten. | In diesem Paket konkret übernommen: verpflichtende Signaturprüfung im produktiven Rootless-Queue-Pfad plus manueller signierender Releaseworkflow. |
| [Awesome Free LLM APIs](https://github.com/mnfst/awesome-free-llm-apis) | Breites Verzeichnis möglicher Zugänge. | Für Entdeckung nutzen; technische oder finanzielle Zusagen immer am Anbieter bestätigen. Die Recherche zu GitHub Models und Hugging Face zeigt den Aktualitätsbedarf. |

LiteLLMs Fähigkeiten sind zusätzlich in der
[offiziellen Routing-Dokumentation](https://docs.litellm.ai/docs/routing) beschrieben.
Ein fremdes Open-Source-Projekt stellt weder kostenlose Hardware noch einen
automatischen Qualitätsnachweis für TankAI bereit.

## Verbindliche nächste Arbeitspakete

**A – Vergleichbare Qualität:** Den bestehenden Korpus um repräsentative Aufgaben
für Deutsch, Code, Werkzeugverwendung und lange Arbeit erweitern. Kandidaten unter
denselben Aufgaben, Werkzeugrechten und Kosten-/Zeitgrenzen vergleichen.
Fehler, Abbrüche und fehlende Daten gehören in das Ergebnis; keine Selbstabnahme.

**B – Verlässliche kostenlose Zulassung:** Kontingente pro tatsächlichem
Providerkonto und Modellpool führen. Ablaufdatum der Preis-/Quotenquelle,
Tokenreservierung, Tagesrest, Router-Fallbacks und Retry-After berücksichtigen.
Unbekannte Kosten blockieren autonome Aufrufe. Keine Kontenvermehrung oder
Schlüsselrotation zur Umgehung von Anbietergrenzen.

**C – Aufgaben fortsetzen:** Plan, abgeschlossene Schritte und ausstehende
Seiteneffekte dauerhaft sichern. Nach Unterbrechung keine ganze Aufgabe blind
wiederholen. Gedächtnis- und Development-Queue-Checkpoints sind vorhandene
Bausteine, noch keine vollständig fortsetzbare Chat-Ausführung.

**D – Eigene Modellgewichte:** Den bestehenden Model-Family-Plan mit einem
messbaren Tank Core-Kandidaten beginnen. Lizenz, Trainingsdaten, Ressourcen und
unabhängige Holdouts klären; erst danach einen begrenzten Trainingslauf buchen.
Frontier-Vergleich und Produktentwicklung stehen im ergänzten Modellprogramm.
