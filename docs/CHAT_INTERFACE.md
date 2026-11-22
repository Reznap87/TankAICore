# TankAI-Chatoberfläche

Die Chatoberfläche wird vom vorhandenen Python-Webserver unter `/` ausgeliefert.
HTML, CSS und JavaScript liegen in `tankai/web/chat.html`, `chat.css` und
`chat.js`. Der Server bindet CSS und JavaScript mit dem vorhandenen, pro Antwort
neu erzeugten CSP-Nonce ein. Zusätzliche CDN-Zugriffe sind nicht erforderlich.

Die Oberfläche nutzt die bestehenden gleichursprünglichen Endpunkte:

| Funktion | Endpunkt |
| --- | --- |
| Anmeldung und Session | `/api/auth/login`, `/api/auth/me`, `/api/auth/logout` |
| Workspace wechseln | `/api/workspaces/select` |
| Core- und Modellkonfiguration | `/api/health` |
| Letzte 20 Aufgaben und Antworten | `/api/history` |
| Direkte Live-Chatantwort (Standard) | `/api/chat` |
| Mehrstufige Aufgabe mit Prüfung (optional) | `/api/run` |

POST-Anfragen verwenden den CSRF-Token der Session. Workspace-Wechsel und
Abmeldung verwerfen den sichtbaren Verlauf; verspätete Antworten einer alten
Session werden nicht in die neue Ansicht übernommen. Texte aus dem Core werden
als Text ausgegeben. Eine neue Chat- oder Run-Antwort wird nur dargestellt, wenn
ihre `workspace_id` exakt zum aktiven angemeldeten Workspace passt; fehlende oder
abweichende Workspace-Herkunft wird fail-closed als Fehler angezeigt. Im Browser
wird nur das Erscheinungsbild gespeichert. Auch der vom Server geladene Verlauf
wird vollständig verworfen, sobald ein Eintrag keine exakt passende
`workspace_id` trägt; solche Einträge gelangen damit weder in die Seitenleiste
noch in Suche oder Ergebnisansicht.

Nach Login oder Registrierung öffnet die Oberfläche den Arbeitsbereich nur,
wenn die E-Mail im Auth-Nachweis der kanonischen Browser-Eingabe entspricht.
Für ASCII-Adressen wird die bereits getrimmte Eingabe dazu kleingeschrieben,
passend zur vorhandenen Normalisierung im Backend. Request-Payload und empfangene
E-Mail werden dabei nicht verändert; der Nachweis muss weiterhin exakt die
erwartete kanonische Adresse enthalten. Ein formal erfolgreicher Nachweis für
ein anderes Konto bleibt in der Anmeldeansicht und wird als Identitätsfehler
angezeigt. Dasselbe gilt für einen Auth-Nachweis ohne nichtleeren
`csrf_token` oder ohne aktive `workspace.id`: Die App öffnet erst, wenn die
folgende schreibende Chataktion an die belegte Sitzung und den belegten
Arbeitsraum gebunden werden kann. Dieselbe Strukturprüfung gilt beim
automatischen Wiederherstellen einer bestehenden Sitzung über `/api/auth/me`;
ein unvollständiger Restore-Receipt bleibt in der Anmeldeansicht.

Ein Workspace-Wechsel übernimmt den neuen Sitzungsnachweis nur, wenn dessen aktive
`workspace.id` exakt der angeforderten Auswahl entspricht und die Konto-E-Mail
unverändert bleibt. Ein Receipt für einen anderen Workspace oder ein anderes Konto
lässt den bisherigen Arbeitsraum aktiv und zeigt einen Fehler.

Das Browserfeld verwendet die native Prüfung von `type=email`. Der Frontend-
Vergleich implementiert kein allgemeines Unicode-Casefolding des Backends.
Im geprüften Edge werden beispielsweise `Straße@example.invalid` und
`a@straße.invalid` bereits vom E-Mail-Feld als ungültig abgewiesen. Ein
allgemeiner Unicode-Adressabgleich ist mit diesem Test nicht nachgewiesen.

Standardmäßig sendet die Oberfläche eine Nachricht an das konfigurierte echte Hauptmodell. `/api/chat` führt genau einen Modellaufruf aus und kennzeichnet das Ergebnis als **nicht unabhängig geprüft**. Bei Mock-/Echo-Konfiguration verweigert der Endpunkt mit HTTP 503 die Antwort, statt eine Simulation als KI auszugeben. Session, CSRF, Workspace-Isolation, Provider-Limit und Verlauf werden weiterverwendet. **Ein konfigurierter Live-Provider allein genügt nicht:** wie `/api/run` benötigt `/api/chat` eine serverseitig geprüfte gemeinsame `OwnerPilot`-Geldfreigabe mit aktuellem Eigentümer-/Workspace-Recht, Tarifen, Token-Zählung und dauerhaftem Reservierungsbuch. Ohne diese Freigabe antwortet `/api/chat` mit HTTP 503 (`monetary_admission_required`) und führt **keinen** Modellaufruf aus. Ein freigegebener Direktchat verwendet genau einen kostenreservierten Hauptmodellaufruf, ohne den unabhängigen Critic tatsächlich aufzurufen. Ein gesperrter oder unsicherer Provider-Versuch bleibt im Ledger konservativ erfasst. Die Einstellungen erlauben alternativ den bestehenden mehrstufigen `/api/run`-Pfad. Ohne tatsächlich angeschlossenes Live-Modell funktioniert auch der neue Direktchat nicht.

**OpenRouter-Free (optional):** `TANKAI_LLM=openrouter` nutzt
`OPENROUTER_MODEL=openrouter/free` (oder eine ausdrücklich konfigurierte
`:free`-Modellvariante) über den festen
`https://openrouter.ai/api/v1`-Endpunkt. Dieser Modus benötigt einen
privaten `OPENROUTER_API_KEY` sowie beide getrennten Aktivierungsflags
`TANKAI_LIVE_PROVIDER_ENABLED=1` und
`TANKAI_OPENROUTER_FREE_ENABLED=1`. Standard ist **aus**.
Die auf echte bezahlte Modelle zugeschnittene `OwnerPilot`-Geldfreigabe
bleibt für diese Modelle verpflichtend; ausschließlich der explizite
OpenRouter-Free-Modus besitzt stattdessen eine dedizierte, persistente
serverweite Anfragefreigabe (maximal 20/min und vorsorglich 50/24h).
Pro Nachricht wird ein Free-Modell kontaktiert, kein kostenpflichtiger
Fallback und kein unabhängiger Critic. Die Antwort nennt die vom Provider
gemeldete `:free`-Modell-ID. Fehlendes Secret, gesperrte Freigabe,
fremde Workspace-Berechtigung oder erschöpfte Quote verhindern Live-Aufrufe.
**Datenschutz:** Beim späteren Aktivieren gehen neue Chat-Anfragen und
relevante vorherige Gesprächsauszüge an OpenRouter und den ausgewählten
Drittanbieter; einzelne kostenlose Anbieter können Eingaben/Ausgaben zum
Modelltraining verwenden. Ohne bewusste Aktivierung erfolgt kein solcher
Versand. Ein echter Provider-/Browser-End-to-End-Test ist weiterhin
ein gesonderter Nachweis, kein Ergebnis der Offline-CI.

Eine neue direkte Antwort wird außerdem nur gerendert, wenn ihr eigener Payload
eine vom Provider-Response bestätigte Hauptmodellidentität in `main_llm` und einen nicht leeren
Antworttext in `final_answer` enthält. Fehlt einer dieser per-Run-Nachweise,
bleibt die Antwortkarte verborgen und die Oberfläche meldet den Fehler; die
globale Health-Anzeige kann ihn nicht ersetzen. Damit wird eine formal
erfolgreiche HTTP-Antwort ohne benutzbare Modellantwort nicht als Live-Erfolg
dargestellt.

Jede Nachricht startet einen einzelnen, budgetierten Core-Run. Der
Direktchat unterstützt nun **begrenzte Folgefragen**: Der Browser übermittelt
optional `reply_to` als 32-stellige Run-ID, aber niemals ungeprüfte frühere
Dialogtexte. Der Server liest die Referenz ausschließlich aus dem Verlauf des
aktiven, authentifizierten Workspaces und rekonstruiert bis zu vier vorherige
Frage-/Antwort-Paare. Je Paar werden höchstens 700 Zeichen der Frage und 900
Zeichen der Antwort als Kontext für den nächsten Hauptmodellaufruf genutzt.
Nur echte, gespeicherte Direktchat-Ergebnisse werden akzeptiert; fehlende,
fremde oder nicht passende Referenzen führen vor jedem Modellaufruf zu
HTTP 409 (`conversation_unavailable`). Die Prüfung von Session, CSRF,
Workspace-Mitgliedschaft, OwnerPilot und Geldreservierung bleibt unverändert
erforderlich. Die letzte Antwort und ihr `reply_to` werden dauerhaft
gespeichert. `Neuer Chat` startet wieder ohne Verweis.

Dies ist ein begrenzter Gesprächsfaden auf Basis einzelner Runs, **keine
unbegrenzte Chat-Session**: Nur die letzten 100 serverseitigen Workspace-Einträge
stehen für die Rekonstruktion zur Verfügung; ältere Verweise können mit 409
abgewiesen werden. Die Oberfläche stellt verfügbare frühere Dialogzüge dar,
aber ein vollständig darüber hinausgehender Langzeitkontext, Dateiuploads
oder Sprachaufnahme sind nicht implementiert.
Die Suche umfasst die letzten 20 Aufgaben des aktiven Workspace. Der Export
enthält das ausgewählte Ergebnis, dessen Ausführungsmodus sowie die vom jeweiligen
Run gemeldete Identität des Hauptmodells und eines tatsächlich verwendeten
Prüfmodells. Der direkte Chat kennzeichnet das Prüfmodell ausdrücklich als
`nicht verwendet` und behauptet keine Critic-Unabhängigkeit, weil dieser Pfad nur
das Hauptmodell aufruft. Fehlende Herkunftsdaten eines geprüften Runs werden als
`nicht belegt` angezeigt; insbesondere gilt die Health-Anzeige nicht als Nachweis
dafür, welches Modell eine einzelne Antwort erzeugt hat. Gemischte Ausführung wird
ausdrücklich als nicht verifizierte Live-Antwort markiert.

`Core verbunden` bestätigt eine erfolgreiche Health-Antwort nur dann, wenn der
Core zugleich den Live-Modus und eine nicht leere Hauptmodellidentität meldet.
Bei angemeldeten Konten muss der Health-Nachweis außerdem exakt die E-Mail des
aktiven Kontos und die unveränderliche `workspace_id` des aktiven Workspace tragen;
ein bloß gleicher Workspace-Name genügt nicht. Ein Nachweis für ein anderes Konto
oder einen anderen Workspace wird fail-closed als unterbrochene Verbindung
angezeigt und kann deshalb keinen fremden Live-Status übernehmen.
Ein formal live wirkender Health-Payload ohne `llm` bleibt bei
`Modellstatus unbekannt` und zeigt keinen Live-Chat an. Diese Anzeige ersetzt
weiterhin keinen erfolgreichen Modellaufruf. Das Backend meldet
`direct_chat_available=true` jetzt nur für nicht simulierte Hauptmodelle mit
explizit vorhandenem und aktuell autorisiertem OwnerPilot. Dies ist kein
Nachweis über freie Restbudgets oder die Erreichbarkeit der Provider-API;
ein kontrollierter echter Login → `/api/chat` → Modellantwort-Smoke ist separat nötig. Simulationen, gemischte Ausführung
und fehlende Freigabe sind gesondert markiert.

Die dauerhafte Aufbewahrung von Konten und Verlauf hängt weiterhin vom
konfigurierten Server-Speicher ab; diese Änderung ergänzt keinen persistenten
Cloudflare-Containers-Speicher.

Die Änderung benötigt ein neues Deployment des bestehenden Core-Containers.
Die eigenständige private Sites-Vorschau wird dadurch nicht umgeleitet. Der
bestehende Workflow `deploy-cloudflare.yml` bleibt der Produktionsweg; DNS,
Cloudflare-Token, Live-Provider und Deployment-Schutzregeln bleiben bestehen.
