AGENT Contract - Standard

Zweck
Das Agent Contract beschreibt Erwartungen an einen Agenten-Modul, seine Schnittstellen und Rechte.

Felder
- name: Eindeutiger Agentenname
- purpose: kurze Beschreibung der Aufgabe
- inputs: Erwartete Eingaben (messages, sensors, configs)
- outputs: Erwartete Ausgaben (actions, logs, telemetry)
- permissions: externe Services/Provider die verwendet werden dürfen
- memory: Art und Umfang des persistenten Speichers
- failure_modes: erwartete Fehler und Fallback-Verhalten
- observability: Metriken & Logs die geliefert werden müssen

Beispielimplementierung muss einheitliche Init/Run/Shutdown-Schnittstellen bereitstellen.
