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
wird nur das Erscheinungsbild gespeichert.

Standardmäßig sendet die Oberfläche eine Nachricht an das konfigurierte echte Hauptmodell. `/api/chat` führt genau einen Modellaufruf aus und kennzeichnet das Ergebnis als **nicht unabhängig geprüft**. Bei Mock-/Echo-Konfiguration verweigert der Endpunkt mit HTTP 503 die Antwort, statt eine Simulation als KI auszugeben. Session, CSRF, Workspace-Isolation, Provider-Limit und Verlauf werden weiterverwendet. Die Einstellungen erlauben alternativ den bestehenden mehrstufigen `/api/run`-Pfad. Ohne tatsächlich angeschlossenes Live-Modell funktioniert auch der neue Direktchat nicht.

Jede Nachricht startet einen einzelnen Core-Run. Der Entwurf implementiert
keine zusätzlichen mehrteiligen Chat-Sessions, Dateiuploads oder Sprachaufnahme.
Die Suche umfasst die letzten 20 Aufgaben des aktiven Workspace. Der Export
enthält das ausgewählte Ergebnis, dessen Ausführungsmodus sowie die vom jeweiligen
Run gemeldete Identität des Hauptmodells und eines tatsächlich verwendeten
Prüfmodells. Der direkte Chat kennzeichnet das Prüfmodell ausdrücklich als
`nicht verwendet` und behauptet keine Critic-Unabhängigkeit, weil dieser Pfad nur
das Hauptmodell aufruft. Fehlende Herkunftsdaten eines geprüften Runs werden als
`nicht belegt` angezeigt; insbesondere gilt die Health-Anzeige nicht als Nachweis
dafür, welches Modell eine einzelne Antwort erzeugt hat. Gemischte Ausführung wird
ausdrücklich als nicht verifizierte Live-Antwort markiert.

`Core verbunden` bestätigt eine erfolgreiche Health-Antwort mit dem vom Core
gemeldeten Live-Modus. Es ersetzt keinen erfolgreichen Modellaufruf.
Simulationen, gemischte Ausführung und fehlende Freigabe sind gesondert markiert.

Die dauerhafte Aufbewahrung von Konten und Verlauf hängt weiterhin vom
konfigurierten Server-Speicher ab; diese Änderung ergänzt keinen persistenten
Cloudflare-Containers-Speicher.

Die Änderung benötigt ein neues Deployment des bestehenden Core-Containers.
Die eigenständige private Sites-Vorschau wird dadurch nicht umgeleitet. Der
bestehende Workflow `deploy-cloudflare.yml` bleibt der Produktionsweg; DNS,
Cloudflare-Token, Live-Provider und Deployment-Schutzregeln bleiben bestehen.
