# Gepinnter öffentlicher Seitenabruf

Stand: 22. Oktober 2026. Gilt für `WebPageFetcher`, nicht für beliebige
Provideradapter oder vom Betreiber ergänzte Transportimplementierungen.

## Verbindung und Weiterleitungen

Jede URL wird auf HTTP/HTTPS, erlaubte Webports, fehlende Zugangsdaten und einen
zulässigen Host geprüft. Alle DNS-Adressen müssen global und außerhalb der
zusätzlich ausgeschlossenen privaten, lokalen, reservierten und Multicastbereiche
liegen. CGNAT/shared address space sowie IPv6-Zonen sind ebenfalls ausgeschlossen.
Mehr als 128 DNS-Antworten verhindern den Abruf.

Der Transport bewahrt die geprüften numerischen Adressen auf und verbindet einen
IPv4-/IPv6-Socket direkt damit. Beim Verbinden erfolgt keine zweite DNS-Auflösung.
Die gemeldete Gegenstelle muss zur gewählten Adresse passen. Höchstens acht bereits
geprüfte Adressen werden innerhalb eines gemeinsamen Verbindungszeitlimits
versucht. Die DNS-Auflösung selbst hat weiterhin das Verhalten des Systemresolvers;
das konfigurierte Socket-Timeout ist keine vollständige Deadline für DNS und eine
langsam übertragene gesamte Antwort.

Für HTTPS bleiben der ursprüngliche, gegebenenfalls IDNA-normalisierte Hostname
als TLS-Servername und die Standard-Zertifikats-/Hostprüfung erhalten. HTTP `Host`
bezieht sich ebenfalls auf den URL-Host. ALPN beschränkt den Transport auf HTTP/1.1.
Proxyeinstellungen aus Umgebung oder System werden beim Seitenabruf nicht gelesen.
Eine Umgebung, die nur über einen Unternehmensproxy ins Web darf, muss diesen
direkten Abruf ausdrücklich ermöglichen oder einen getrennt geprüften Transport
bereitstellen; stilles Ausweichen über einen Proxy erfolgt nicht.

Weiterleitungen werden explizit verarbeitet, standardmäßig höchstens fünfmal.
Jedes neue Ziel wird erneut validiert, aufgelöst und gepinnt, auch beim gleichen
Hostnamen. Die vorherige Antwort und Verbindung werden vor dem nächsten Hop
geschlossen. Content-Type- und Größenlimits sowie die HTML-Textfilter bleiben
erhalten. Ein zusätzlicher URL-Validator kann die Regeln verschärfen, aber nicht
die IP-Prüfung abschalten.

## Nachweise und Grenzen

Die Offline-Tests verwenden echte HTTPConnection-/HTTPResponse-Verarbeitung mit
Speichersockets und einem kontrollierten TLS-Wrapper. Sie prüfen einen DNS-Wechsel
zu localhost, gemischte öffentliche/private Antworten, IPv4/IPv6, Host und TLS-SNI,
Zertifikatsfehler vor HTTP-Versand, falsche Gegenstellen, Proxyeinstellungen,
Redirect-Rebinding, fremde Hosts, Redirect-Schleifen, Ressourcenfreigabe,
Größenlimits und das gemeinsame Verbindungszeitlimit. Die Standard-TLS-Konfiguration
wird auf `CERT_REQUIRED` und Hostprüfung untersucht. Das ist kein echter externer
TLS-Handshake und keine Betriebsabnahme sämtlicher Netzwerkumgebungen.

IP-Pinning bindet den Socket an die geprüfte IP, nicht an eine unveränderliche
Betreiberidentität oder an die Wahrheit des Seiteninhalts. HTTP bleibt
unverschlüsselt. Egress-Firewalls und die Sicherheit lokaler Netzwerk-/NAT-Routen
bleiben eigene Betriebsebenen. Ein öffentlicher Server kann weiterhin feindliche
Inhalte liefern; vorhandene Prompt-/Quellenfilter bleiben deshalb erforderlich.
Die fest verdrahteten Such-API-Endpunkte und deren Credential-/Redirectregeln
wurden durch diese Änderung nicht auf einen anderen Provider umgestellt.

Primärquellen:

- [Python Socket-Adressen und connect](https://docs.python.org/3/library/socket.html#socket.socket.connect)
- [Python TLS-Standardkontext und Servername](https://docs.python.org/3/library/ssl.html#ssl.SSLContext.wrap_socket)
- [Python HTTPConnection und ALPN-Hinweise](https://docs.python.org/3/library/http.client.html#http.client.HTTPSConnection)
