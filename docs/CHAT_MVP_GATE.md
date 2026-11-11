# TankAI Chat-MVP: überprüfbarer Live-Nachweis

**Ziel:** Nicht „CI grün“ oder „Container läuft“, sondern ein erfolgreiches
Login und ein tatsächlicher freigegebener, nicht simulierter Chat-Run mit
nachvollziehbaren Modellidentitäten.

## Prüfschritte

1. Registrieren/Anmelden über die Weboberfläche (Registrierungs-PR #105 ist
   derzeit unabhängig und benötigt einen eigenen Release). Das Probe-Receipt
   akzeptiert die Anmeldung nur, wenn Antwort und Folgezustand auf genau das
   angeforderte Konto und einen konkreten Workspace gebunden sind.
2. Authentifizierte `/api/health`-Antwort mit demselben Konto und Workspace:
   `execution_mode=live`,
   `live_provider=true`, `critic_independent=true`,
   `verification_ready=true`, eindeutige Haupt- und Critic-Modellidentität.
3. Separat autorisierter `/api/run`: `status=completed`,
   `execution_mode=live`, `release_ready=true`,
   `verification_passed=true`, nichtleere Antwort und unveränderte
   Modellidentitäten.
4. Anschließend im realen Browser prüfen, dass genau diese Antwort im Chat
   lesbar erscheint. Das CLI-Skript beweist **nicht** die Browser-Darstellung.

## CLI: zuerst ohne Modellaufruf

Nutzername und Passwort nur über private Umgebungsvariablen bereitstellen:

```bash
export TANKAI_SMOKE_EMAIL='...'
# Passwort ausschließlich privat setzen; nicht in Shell-History oder CI-Ausgabe schreiben.
python scripts/chat_mvp_probe.py --base-url https://tankaicore.com
```

Der Standardmodus meldet sich an, prüft nur den authentifizierten Health-
Vertrag und gibt ein minimales JSON-Receipt **ohne Zugangsdaten** aus.
`live_answer_verified=false` bleibt in diesem Modus immer bestehen.

Ein echter Run kann mehrere interne Modellaufrufe auslösen und kostenpflichtig
sein. Dieses Skript begrenzt **nicht** die Providerrechnung. Nur nach
separater Autorisierung, konfigurierten serverseitigen Aufruf-/Kostenlimits
und überprüfter Readiness:

```bash
export TANKAI_ALLOW_BILLED_SMOKE=1
python scripts/chat_mvp_probe.py --run --approve-provider-cost
```

Die beiden Freigaben sind ein zusätzlicher Schutz vor unbeabsichtigten
Modellaufrufen, keine Kostenobergrenze. Das Skript ändert keine Secrets,
Provider, DNS-Konfiguration, Produktion oder Deployment und startet keinen
Server. Die aktuellen Python-Tests verwenden ausschließlich synthetische
Antworten; sie sind ausdrücklich kein Live-Nachweis.

## Trennlinie zur Realität

Zum unabhängigen Lesecheck am 11. November 2026 lief der
Cloudflare-Container `tankai-core-tankaicontainer`. Der aktive Worker
`tankai-core` hatte nur die Bindings `TANKAI_CONTAINER`,
`TANKAI_AUTH_STATE`, `TANKAI_AUTH_STATE_TOKEN` und
`TANKAI_BOOTSTRAP_OWNER`. Ein Live-Provider war nicht konfiguriert.
Insbesondere gilt bei fehlendem `TANKAI_LIVE_PROVIDER_ENABLED=1`
im vorhandenen Worker-Code der Mock-Pfad.

**Offene Release-Voraussetzung:** Provider und unabhängigen Critic
explizit auswählen und korrekt konfigurieren, Zahlungsrisiko und
serverseitige Budgets freigeben, aktuelle Produktions-Readiness und
Produktions-Smoketests bestehen. Ohne diese Voraussetzungen kein Live-
Kandidat und keine Behauptung, TankAI sei nutzbar.
