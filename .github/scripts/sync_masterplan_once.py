from pathlib import Path

path = Path("TANKAI_MASTERPLAN.md")
text = path.read_text(encoding="utf-8")
marker = "\n1. Ergebnis, das entstehen muss\n"
if marker not in text:
    raise SystemExit("masterplan section-1 marker not found; refusing rewrite")
_, historical = text.split(marker, 1)

current = '''TANKAI – VERBINDLICHER MASTERPLAN

Version: 5.6.0
Projektlinie: TankAI Web → TankAI Core → TankAI-Modellfamilie → TankBot/TankStation
Statusdatum: 28. August 2026
Leitentscheidung: Webprodukt zuerst, eigener Modellstack schrittweise, jede Überlegenheit messbar

0. Verifizierter Projektstand und Ausführungsvertrag am 28. August 2026

Dieser Abschnitt ist der aktuelle Reality Contract und damit die alleinige aktuelle
Statusquelle dieses Dokuments. Die historischen Produkt-, Release- und Entwicklungsabschnitte
ab Abschnitt 1 bleiben als Entwicklungsnachweis unverändert erhalten. Historische Statussätze
sind keine aktuelleren Behauptungen als dieser Reality Contract.

Der Reality Contract beendet die Arbeit nicht. Sobald ein Stand durch Commit, CI, Receipt und
externen Check gebunden ist, wird das nächste sichere Gate aktiv.

0.1 Verbindlicher Grundsatz

REALITÄT VOR PLANUNG.

TESTS VOR BEHAUPTUNGEN.

RECEIPTS VOR STATUSMELDUNGEN.

EIN GATE IST EIN ÜBERGANG UND KEIN ENDPUNKT.

EIN TOOLBLOCKER IST KEIN TASK-ENDE.

EINE FRÜHERE AUTORISIERUNG GILT NICHT AUTOMATISCH FÜR EINEN NEUEN COMMIT ODER EINEN NEUEN
PRODUKTIONSMUTATIONSSCHRITT.

Der verbindliche Arbeitsfluss lautet:

Realität -> Task -> Code -> Test -> Receipt -> Gate -> nächster Task.

Nach jedem bestandenen Gate wird das nächste Gate ausdrücklich benannt. Nach jedem
fehlgeschlagenen Gate wird ein konkreter Reparatur-Task aktiv. Ein externer Blocker stoppt nur
den davon abhängigen Schritt.

0.2 Aktive Source of Truth

Aktives Core-Repository: Reznap87/TankAICore, Branch main.

Aktueller verifizierter main-Commit:

d7edb12b764310f00804c724ad6d3b4bbc96b54a

Zugehöriger Git-Tree:

f318d8ca72500b03f19635af0834c36d151ab232

Commit-Titel:

fix(deployment): validate Cloudflare Containers access (#21)

Verbindlicher Releasevertrag: TankAI-Core-1.10.0-module-ownership.

Verbindlicher Zustandsvertrag: ProjectState Schema 6.

main ist durch einen aktiven GitHub-Ruleset geschützt. Der Pull-Request-Pfad ist erzwungen,
Force-Pushes und Branch-Löschung sind blockiert. Die verpflichtenden GitHub-Actions-Checks für
den Merge-Pfad sind:

test

cloudflare

Die alte Reality-Contract-Identität 50547cf1d20a4ae8273774c2c14f0a4849b23a84 mit Tree
7814a1977e631dd45492ce4c1c1b2a38286bc645 ist nur historisch und darf nicht mehr als aktueller
Source-of-Truth-Stand interpretiert werden.

0.3 Aktuell verifizierter Betriebs- und Deploymentstatus

Verifiziert sind:

main-Commit d7edb12b764310f00804c724ad6d3b4bbc96b54a,

Git-Tree f318d8ca72500b03f19635af0834c36d151ab232,

Core-Version 1.10.0-module-ownership,

ProjectState Schema 6,

GitHub-Ruleset-Schutz für main mit den Pflichtchecks test und cloudflare,

TankAI Core CI Run #32 für exakt d7edb12b764310f00804c724ad6d3b4bbc96b54a: completed/success,

ops.container.web_runtime_smoke: VERIFIED,

der Production-Deploy-Gate ist fail-closed, prüft den exakten main-SHA, Ruleset-/CI-Zustand und
verweigert einen Deploy bei nicht erfüllten Voraussetzungen,

der Deploy-Gate ist Ruleset-aware und verwendet die effektiven Branch-Regeln statt nur die alte
Branch-Protection-Zusammenfassung,

Production Preflight #2, Run 33208563241, auf exakt
d7edb12b764310f00804c724ad6d3b4bbc96b54a: completed/success,

der Preflight-Schritt Verify Cloudflare token, account, and Containers access: PASS,

die Production-Secrets CLOUDFLARE_API_TOKEN und CLOUDFLARE_ACCOUNT_ID waren für die Jobs
vorhanden; ihre Werte wurden nicht in Receipts übernommen,

Issue #14 zu Production-Preflight-Governance: CLOSED/completed,

Deploy to Cloudflare #4, Run 33209014964, für exakt
d7edb12b764310f00804c724ad6d3b4bbc96b54a: completed/success,

Cloudflare Worker-Upload: PASS,

Container-Image-Build: PASS,

Cloudflare-Registry-Push: PASS,

Container-Anwendung tankai-core-tankaicontainer: CREATED,

Container Application ID: a0378ad1-191f-4114-ab2f-9c2c906484a0,

Container-Image-Digest:
sha256:9caef24aa0c09c29a8fdfbb5cf24c09611f753ffe8a370911551e64748356917,

Cloudflare Worker Version ID: ba23e917-33f5-46c5-9316-b03ec33bc852,

Custom Domains tankaicore.com und www.tankaicore.com: deployed,

externer HTTPS-Healthcheck https://tankaicore.com/api/health: live mit ok=true und
version=1.10.0-module-ownership,

Issue #20 zum Cloudflare-Container-Authorization-Blocker: CLOSED/completed.

Der frühere Unauthorized-Fehler im Container-Deploy ist damit behoben. Der Cloudflare-Account
ist für den Containerpfad freigeschaltet und der verwendete Token besitzt den benötigten
Containerzugriff.

0.4 Explizite Grenzen des aktuellen Produktionsstands

Der erfolgreiche Infrastruktur-Deploy ist kein Beleg dafür, dass jede TankAI-Produktfähigkeit
allgemein produktionsreif ist.

Der externe Healthcheck meldet production_ready=false. Das ist im aktuellen Releasevertrag
bewusst so vorgesehen, solange öffentlicher Webdienst und privilegierter Development-Runner als
getrennte Prozesse und Dienstkonten behandelt werden müssen.

Die aktuell deployte Cloudflare-Containerkonfiguration setzt TANKAI_LLM=mock und
TANKAI_EMBEDDER=hashing. Damit ist die öffentlich erreichbare Web-/Container-Runtime real und
verifiziert, aber dieser Receipt behauptet keinen produktiven Live-LLM-Providerbetrieb.

Ebenfalls nicht aus dem Deploy-Receipt ableitbar sind:

eine aktivierte persistente Development-Queue,

ein produktiver privilegierter Development-Runner,

eine allgemeine Product-Readiness-Freigabe,

ein realer OpenAI-/Anthropic-/xAI-/Gemini-Produktionslauf,

eine aktivierte produktive Suchproviderkette,

eine Autorisierung für einen weiteren Production-Deploy oder eine andere neue
Produktionsmutation.

0.5 Status- und Ausführungsvertrag

VERIFIZIERT bedeutet: durch aktuelle Git-Objekte, Code, Tests, Receipts oder einen externen
Live-Check belegt.

AKTIV bedeutet: der nächste primäre Ausführungsschritt.

BEREIT bedeutet: ausführbar, sobald seine technischen Vorgates erfüllt sind.

OFFEN bedeutet: noch nicht umgesetzt oder noch nicht ausreichend geprüft.

TOOLBLOCKER bedeutet: ein Werkzeug oder Zugriff fehlt; sichere Fallbacks und unabhängige Tasks
bleiben verpflichtend.

EXTERN BLOCKIERT bedeutet: von einem externen System, Zugriff oder einer fremden Entscheidung
abhängig.

AUTORISIERUNG AUSSTEHEND bedeutet: technisch vorbereitbar, aber die konkrete irreversible
Aktion ist nicht freigegeben.

NICHT BEHAUPTET bedeutet: es liegt kein ausreichender Receipt für eine positive Statusaussage
vor.

DEPLOYED bedeutet: der konkrete, exakt benannte Commit wurde durch einen erfolgreichen
Deployment-Receipt in die benannte Zielumgebung ausgerollt. DEPLOYED ist nicht synonym mit
vollständiger Product Readiness.

FERTIG oder BEENDET darf nur für einen klar abgegrenzten Task verwendet werden. Der Gesamtplan
bleibt aktiv, solange TankAI weiterentwickelt wird.

0.6 Abgeschlossene Gates dieser Produktionslinie

Die folgenden Gates sind für den oben gebundenen Stand abgeschlossen:

ops.container.web_runtime_smoke -> VERIFIED,

ops.production.preflight_readonly -> VERIFIED,

ops.production.cloudflare_deploy -> VERIFIED,

ops.production.external_health -> VERIFIED.

Der produktionsnahe Container wurde nicht nur gebaut, sondern gestartet und über den
verpflichtenden Runtime-Smoke geprüft. Der Read-only-Preflight prüfte vor dem mutierenden Deploy
den GitHub-/Cloudflare-Zugriff. Der eigentliche Cloudflare-Deploy erhielt eine separate exakte
Autorisierung und wurde anschließend durch den öffentlichen Health-Endpunkt extern bestätigt.

0.7 Aktives Gate

Das nächste aktive Gate ist:

ops.production.live_provider_readiness

Begründung: Die produktive Web-/Container-Infrastruktur ist live, die aktuelle
Containerkonfiguration nutzt aber weiterhin TANKAI_LLM=mock. Der nächste sinnvolle Schritt ist
deshalb nicht ein weiterer blinder Deploy, sondern die kontrollierte Vorbereitung eines echten
serverseitigen Providerbetriebs.

Dieses Gate ist zunächst lesend und vorbereitend. Es muss mindestens klären:

welche Live-Provider für Hauptmodell und unabhängigen Critic freigegeben werden sollen,

welche serverseitigen Secret-Namen und Modell-IDs dafür benötigt werden, ohne Secret-Werte in
Logs oder Receipts zu schreiben,

welche Kosten-, Token-, Laufzeit- und Rate-Limits vor dem ersten Live-Providerlauf gelten,

ob die geforderte Trennung von Hauptmodell und Critic tatsächlich erfüllbar ist,

welcher Suchprovider und welche Evidence-Regeln für produktive Recherche gelten,

welcher minimale kontrollierte Live-Smoke-Test nach der Konfiguration ausgeführt wird,

welcher Rollback den Stand wieder auf den verifizierten Mock-/Hashing-Betrieb oder den vorherigen
Worker-/Containerstand zurückführt.

Das Gate selbst autorisiert weder das Setzen neuer Secret-Werte noch einen weiteren
Production-Deploy noch kostenpflichtige Modellaufrufe.

0.8 Getrennte Autorisierungsgrenzen

Die für Deploy #4 erteilte Freigabe war an exakt
d7edb12b764310f00804c724ad6d3b4bbc96b54a gebunden und ist durch diesen Deploy verbraucht.

Jeder spätere Production-Deploy benötigt erneut:

den dann aktuellen exakten main-SHA,

erfolgreiche verpflichtende CI für genau diesen SHA,

einen aktuellen Production Preflight,

die erforderlichen Runtime-/Provider-Receipts,

einen klaren Rollback-Pfad,

eine neue ausdrückliche Deploy-Autorisierung für genau diesen Stand.

Ein Merge, erfolgreiche CI, ein bestandener Preflight, ein früherer erfolgreicher Deploy oder eine
vorhandene Produktionsumgebung ist keine automatische Autorisierung für den nächsten Deploy.

Auch das Setzen oder Austauschen produktiver Provider-Secrets und das Aktivieren kostenpflichtiger
Live-Provider sind eigene Produktionsmutationen und werden nicht aus diesem Dokument abgeleitet.

0.9 Toolblocker-Vertrag

Wenn GitHub, eine Container-Runtime, Cloudflare, ein Provider oder eine externe Plattform nicht
erreichbar ist, wird der genaue Blocker mit Zeitpunkt und Fehlermeldung dokumentiert.

Danach werden in dieser Reihenfolge geprüft:

ein repositorygestützter oder lokaler Fallback,

ein deterministischer alternativer Runner,

die Implementierung oder Härtung der fehlenden Automatisierung,

ein unabhängiger sicherer Code-, Test- oder Dokumentationstask.

Ein Toolblocker darf ein einzelnes Gate offenhalten. Er darf den Gesamtplan nicht als beendet
markieren, solange ein sicherer ausführbarer Task existiert.

0.10 Unmittelbare Ausführungsreihenfolge

1. Diesen Reality Contract über einen geschützten Pull Request nach main bringen und Issue #19
   nach grünem CI und Merge als completed schließen.

2. Für ops.production.live_provider_readiness einen read-only Provider-/Secret-/Kosten-/Rollback-
   Receipt erzeugen; keine Secret-Werte offenlegen und noch keinen neuen Deploy ausführen.

3. Erst nach einer ausdrücklichen Entscheidung die benötigten serverseitigen Provider-, Critic-
   und optionalen Search-Secrets konfigurieren.

4. Danach vollständige CI, Runtime-Smoke und Production Preflight für den dann aktuellen exakten
   main-SHA wiederholen.

5. Einen weiteren Production-Deploy nur nach neuer exakter Autorisierung ausführen.

6. Nach einem Live-Provider-Deploy einen begrenzten externen Funktionstest mit Kostenlimit,
   unabhängiger Fehlerprüfung und vollständigem Receipt ausführen. Erst dieser Nachweis darf einen
   realen produktiven Modellpfad als VERIFIED markieren.

0.11 Produktlinie und historische Vision

Die verbindliche Zielrichtung bleibt:

TankAI Web -> TankAI Core -> TankAI Model Family -> TankBot/TankStation.

Web bildet Bedienung, Identität, Beobachtung und sichere Auslösung kontrollierter Abläufe ab.

Core bildet den deterministischen Steuerungs-, Zustands-, Capability-, Ownership- und
Prüfvertrag.

Die Model Family beschreibt die schrittweise kontrollierte Auswahl und Entwicklung eigener
Router-, Critic-, Core-, Code- und weiterer spezialisierter Modelle.

TankBot und TankStation sind Produktausprägungen auf Basis der belegten Web-, Core- und
Modellverträge.

Diese Vision bestimmt die Richtung. Sie ist keine Behauptung, dass jede Ebene heute bereits
implementiert oder produktiv betrieben wird.

0.12 Reality-Contract-Versionshistorie

5.6.0, 28. August 2026:

Current Source of Truth auf main d7edb12b764310f00804c724ad6d3b4bbc96b54a / Tree
f318d8ca72500b03f19635af0834c36d151ab232 gebunden; Ruleset und Pflichtchecks dokumentiert;
Runtime-Smoke und Production Preflight als VERIFIED gebunden; erfolgreicher Cloudflare-Deploy #4
mit Container-/Worker-Receipt und externem Healthcheck dokumentiert; production_ready=false und
TANKAI_LLM=mock ausdrücklich als verbleibende Produktgrenzen ausgewiesen; nächstes Gate auf
ops.production.live_provider_readiness gesetzt.

5.5.0, 24. August 2026:

Vorheriger Reality Contract vor Runtime-Smoke, Production-Preflight und Live-Cloudflare-Deploy.
Die darin gebundene Commit-/Tree-Identität ist historisch und nicht mehr aktuelle Source of Truth.
'''

path.write_text(current + marker + historical, encoding="utf-8")
