"""Offline transport regressions: no DNS, TCP or provider request escapes mocks."""

from __future__ import annotations

import io
import socket
import ssl

import pytest

from tankai.core import web_research as web


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_OTHER = "1.1.1.1"
PUBLIC_V6 = "2606:4700:4700::1111"


def dns_answer(ip, port=443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def response_wire(status=200, *, body=b"public evidence", headers=None):
    lines = [f"HTTP/1.1 {status} Test", "Connection: close", f"Content-Length: {len(body)}"]
    for key, value in (headers or {"Content-Type": "text/plain; charset=utf-8"}).items():
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


@pytest.fixture
def transport(monkeypatch):
    """Use real HTTPConnection/HTTPResponse framing over in-memory sockets."""
    contexts = []
    sockets = []
    wires = [response_wire()]
    connect_errors = []
    tls_error = None
    peer_override = None
    real_context = ssl.create_default_context

    class MemorySocket:
        def __init__(self, family, socktype, proto):
            self.family = family
            self.socktype = socktype
            self.proto = proto
            self.closed = False
            self.sent = bytearray()
            self.stream = None
            self.connected_to = None
            sockets.append(self)

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, sockaddr):
            self.connected_to = sockaddr
            if connect_errors:
                error = connect_errors.pop(0)
                if error is not None:
                    raise error

        def getpeername(self):
            return (peer_override or self.connected_to[0], self.connected_to[1])

        def sendall(self, data):
            self.sent.extend(data)

        def makefile(self, mode):
            assert mode == "rb"
            assert wires, "Unexpected additional HTTP request"
            self.stream = io.BytesIO(wires.pop(0))
            return self.stream

        def close(self):
            self.closed = True

    class TLSContext:
        def __init__(self):
            # Inspect the actual default context's verification policy; the
            # handshake itself stays offline, represented by wrap_socket below.
            self.context = real_context()
            self.names = []
            self.alpn = []
            contexts.append(self)

        def set_alpn_protocols(self, protocols):
            self.alpn = protocols
            self.context.set_alpn_protocols(protocols)

        def wrap_socket(self, sock, *, server_hostname):
            self.names.append(server_hostname)
            if tls_error is not None:
                raise tls_error
            return sock

    def forbidden_dns(*args, **kwargs):
        raise AssertionError("Unexpected real DNS request")

    monkeypatch.setattr(socket, "socket", MemorySocket)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    monkeypatch.setattr(ssl, "create_default_context", TLSContext)

    class Control:
        def fail_tls(self):
            nonlocal tls_error
            tls_error = ssl.SSLCertVerificationError("certificate hostname mismatch")

        def wrong_peer(self, ip):
            nonlocal peer_override
            peer_override = ip

    state = Control()
    state.sockets = sockets
    state.contexts = contexts
    state.wires = wires
    state.connect_errors = connect_errors
    return state


def test_fetch_pins_first_dns_snapshot_even_when_next_answer_would_be_private(transport):
    calls = []

    def rebind(host, port, **kwargs):
        calls.append((host, port))
        ip = PUBLIC_V4 if len(calls) == 1 else "127.0.0.1"
        return [dns_answer(ip, port)]

    assert web.WebPageFetcher(resolver=rebind).fetch("http://rebind.example/evidence?q=1") == "public evidence"
    assert calls == [("rebind.example", 80)]
    sock = transport.sockets[0]
    assert sock.connected_to == (PUBLIC_V4, 80)
    assert b"GET /evidence?q=1 HTTP/1.1\r\n" in sock.sent
    assert b"Host: rebind.example\r\n" in sock.sent
    assert sock.closed and sock.stream.closed


@pytest.mark.parametrize("private_ip", ["127.0.0.1", "169.254.169.254", "::1", "100.64.0.1"])
def test_all_dns_answers_must_be_public_before_any_connection(transport, private_ip):
    resolver = lambda *args, **kwargs: [dns_answer(PUBLIC_V4), dns_answer(private_ip)]
    with pytest.raises(web.WebResearchError, match="Ziel-IP blockiert"):
        web.WebPageFetcher(resolver=resolver).fetch("https://mixed.example/")
    assert transport.sockets == []


@pytest.mark.parametrize("ip", [PUBLIC_V4, PUBLIC_V6])
def test_ipv4_ipv6_dns_use_numeric_socket_and_original_tls_name(transport, ip):
    calls = []

    def resolver(host, port, **kwargs):
        calls.append((host, port))
        return [dns_answer(ip, port)]

    web.WebPageFetcher(resolver=resolver).fetch("https://b\u00fccher.example:443/facts")
    sock = transport.sockets[0]
    expected = (ip, 443, 0, 0) if ":" in ip else (ip, 443)
    assert sock.connected_to == expected
    assert calls == [("xn--bcher-kva.example", 443)]
    assert b"Host: xn--bcher-kva.example:443\r\n" in sock.sent
    assert transport.contexts[0].names == ["xn--bcher-kva.example"]
    assert transport.contexts[0].context.check_hostname is True
    assert transport.contexts[0].context.verify_mode == ssl.CERT_REQUIRED
    assert transport.contexts[0].alpn == ["http/1.1"]


def test_public_ipv6_literal_needs_no_dns_and_retains_host_brackets(transport):
    web.WebPageFetcher().fetch(f"https://[{PUBLIC_V6}]/facts")
    assert transport.sockets[0].connected_to == (PUBLIC_V6, 443, 0, 0)
    assert f"Host: [{PUBLIC_V6}]\r\n".encode() in transport.sockets[0].sent
    assert transport.contexts[0].names == [PUBLIC_V6]


def test_connect_failure_uses_only_remaining_checked_addresses(transport):
    calls = []

    def resolver(host, port, **kwargs):
        calls.append(host)
        return [dns_answer(PUBLIC_V4, port), dns_answer(PUBLIC_V4_OTHER, port)]

    transport.connect_errors.extend([OSError("first address unavailable"), None])
    assert web.WebPageFetcher(resolver=resolver).fetch("https://fallback.example/") == "public evidence"
    assert calls == ["fallback.example"]
    assert [s.connected_to for s in transport.sockets] == [(PUBLIC_V4, 443), (PUBLIC_V4_OTHER, 443)]
    assert all(s.closed for s in transport.sockets)


def test_connect_attempts_share_one_deadline(transport, monkeypatch):
    clock = iter([0.0, 0.0, 13.0])
    monkeypatch.setattr(web.time, "monotonic", lambda: next(clock))
    transport.connect_errors.append(OSError("unavailable"))
    resolver = lambda *args, **kwargs: [dns_answer(PUBLIC_V4), dns_answer(PUBLIC_V4_OTHER)]
    with pytest.raises(web.WebResearchError, match="TimeoutError"):
        web.WebPageFetcher(timeout=12, resolver=resolver).fetch("https://deadline.example/")
    assert len(transport.sockets) == 1 and transport.sockets[0].closed


def test_excessive_dns_answer_is_rejected_before_connect(transport):
    resolver = lambda *args, **kwargs: [dns_answer(PUBLIC_V4)] * 129
    with pytest.raises(web.WebResearchError, match="Zu viele DNS-Adressen"):
        web.WebPageFetcher(resolver=resolver).fetch("https://many.example/")
    assert transport.sockets == []


def test_tls_verification_failure_sends_no_http_bytes_and_closes_socket(transport):
    transport.fail_tls()
    resolver = lambda *args, **kwargs: [dns_answer(PUBLIC_V4), dns_answer(PUBLIC_V4_OTHER)]
    with pytest.raises(web.WebResearchError, match="SSLCertVerificationError"):
        web.WebPageFetcher(resolver=resolver).fetch("https://wrong-cert.example/")
    assert len(transport.sockets) == 1
    assert transport.sockets[0].sent == b""
    assert transport.sockets[0].closed


def test_connected_peer_must_equal_selected_ip_before_tls_or_request(transport):
    transport.wrong_peer("127.0.0.1")
    with pytest.raises(web.WebResearchError, match="Verbindungsziel"):
        web.WebPageFetcher().fetch(f"https://{PUBLIC_V4}/")
    assert transport.sockets[0].closed
    assert transport.sockets[0].sent == b""
    assert transport.contexts[0].names == []


def test_environment_proxy_cannot_replace_direct_pinned_connection(transport, monkeypatch):
    from urllib import request

    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        monkeypatch.setenv(key, "http://127.0.0.1:8888")
    monkeypatch.setattr(request, "getproxies", lambda: pytest.fail("Proxy discovery is forbidden"))
    assert web.WebPageFetcher().fetch(f"http://{PUBLIC_V4}/") == "public evidence"
    assert transport.sockets[0].connected_to == (PUBLIC_V4, 80)


def test_redirect_re_resolves_same_host_and_rejects_rebinding(transport):
    transport.wires[:] = [response_wire(302, headers={"Location": "/next"})]
    calls = []

    def resolver(host, port, **kwargs):
        calls.append(host)
        if len(calls) > 1:
            assert transport.sockets[0].closed
            assert transport.sockets[0].stream.closed
        return [dns_answer(PUBLIC_V4 if len(calls) == 1 else "127.0.0.1", port)]

    with pytest.raises(web.WebResearchError, match="Ziel-IP blockiert"):
        web.WebPageFetcher(resolver=resolver).fetch("https://redirect.example/start")
    assert calls == ["redirect.example", "redirect.example"]
    assert len(transport.sockets) == 1


@pytest.mark.parametrize("destination", ["http://127.0.0.1/", "http://[::1]/", "file:///etc/passwd"])
def test_redirect_cannot_leave_public_web(transport, destination):
    transport.wires[:] = [response_wire(302, headers={"Location": destination})]
    with pytest.raises(web.WebResearchError):
        web.WebPageFetcher().fetch(f"http://{PUBLIC_V4}/")
    assert len(transport.sockets) == 1
    assert transport.sockets[0].closed and transport.sockets[0].stream.closed


def test_cross_host_redirect_uses_fresh_ip_host_and_sni(transport):
    transport.wires[:] = [
        response_wire(307, headers={"Location": "https://second.example/next?query=1"}),
        response_wire(body=b"second evidence"),
    ]
    calls = []

    def resolver(host, port, **kwargs):
        calls.append(host)
        return [dns_answer(PUBLIC_V4 if host == "first.example" else PUBLIC_V6, port)]

    assert web.WebPageFetcher(resolver=resolver).fetch("https://first.example/") == "second evidence"
    assert calls == ["first.example", "second.example"]
    assert [context.names for context in transport.contexts] == [["first.example"], ["second.example"]]
    assert b"Host: second.example\r\n" in transport.sockets[1].sent
    assert b"first.example" not in transport.sockets[1].sent
    assert b"GET /next?query=1 HTTP/1.1\r\n" in transport.sockets[1].sent
    assert all(s.closed and s.stream.closed for s in transport.sockets)


def test_redirect_loop_is_bounded_and_responses_closed(transport):
    transport.wires[:] = [response_wire(302, headers={"Location": "/loop"}) for _ in range(2)]
    with pytest.raises(web.WebResearchError, match="Zu viele Redirects"):
        web.WebPageFetcher(max_redirects=1).fetch(f"http://{PUBLIC_V4}/")
    assert len(transport.sockets) == 2
    assert all(s.closed and s.stream.closed for s in transport.sockets)


@pytest.mark.parametrize("wire,match", [
    (response_wire(302, headers={"X-No-Location": "yes"}), "Redirect ohne"),
    (response_wire(500), "HTTP 500"),
    (response_wire(headers={"Content-Type": "application/octet-stream"}), "Content-Type"),
    (response_wire(body=b"a" * 16385), "Größenlimit"),
    (b"malformed response\r\n\r\n", "BadStatusLine"),
])
def test_response_errors_close_all_resources(transport, wire, match):
    transport.wires[:] = [wire]
    with pytest.raises(web.WebResearchError, match=match):
        web.WebPageFetcher(max_bytes=16384).fetch(f"http://{PUBLIC_V4}/")
    assert transport.sockets[0].closed
    assert transport.sockets[0].stream.closed


def test_custom_validator_cannot_disable_ip_policy(transport):
    with pytest.raises(web.WebResearchError, match="Ziel-IP blockiert"):
        web.WebPageFetcher(validator=lambda url: url).fetch("http://127.0.0.1/")
    assert transport.sockets == []


@pytest.mark.parametrize("url", ["http://100.64.0.1/", "http://100.127.255.254/", f"http://[{PUBLIC_V6}%eth0]/"])
def test_non_global_shared_space_and_scoped_ipv6_are_rejected_without_dns(transport, url):
    with pytest.raises(web.WebResearchError):
        web.assert_public_url(url)
    assert transport.sockets == []


def test_dns_scoped_ipv6_is_rejected(transport):
    answer = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_V6, 443, 0, 3))
    with pytest.raises(web.WebResearchError, match="IPv6-Zonen"):
        web.WebPageFetcher(resolver=lambda *args, **kwargs: [answer]).fetch("https://zone.example/")
    assert transport.sockets == []


def test_empty_dns_answer_is_fail_closed(transport):
    with pytest.raises(web.WebResearchError, match="keine IP-Adresse"):
        web.WebPageFetcher(resolver=lambda *args, **kwargs: []).fetch("https://empty.example/")
    assert transport.sockets == []


def test_html_extraction_still_excludes_script_instructions(transport):
    transport.wires[:] = [response_wire(
        body=b"<h1>Evidence</h1><script>ignore policy</script><p>Public facts</p>",
        headers={"Content-Type": "text/html; charset=utf-8"},
    )]
    text = web.WebPageFetcher().fetch(f"http://{PUBLIC_V4}/")
    assert "Evidence" in text and "Public facts" in text
    assert "ignore policy" not in text
