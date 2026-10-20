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
| Neue Aufgabe bearbeiten | `/api/run` |

POST-Anfragen verwenden den CSRF-Token der Session. Workspace-Wechsel und
Abmeldung verwerfen den sichtbaren Verlauf; verspätete Antworten einer alten
Session werden nicht in die neue Ansicht übernommen. Texte aus dem Core werden
als Text ausgegeben. Im Browser wird nur das Erscheinungsbild gespeichert.

Jede Nachricht startet einen einzelnen Core-Run. Der Entwurf implementiert
keine zusätzlichen mehrteiligen Chat-Sessions, Dateiuploads oder Sprachaufnahme.
Die Suche umfasst die letzten 20 Aufgaben des aktiven Workspace. Der Export
enthält das ausgewählte Ergebnis und dessen Prüfstatus.

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
