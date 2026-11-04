"""Shutdown must finish admitted HTTP work before closing application storage."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import signal
import socket
import threading
import time
from types import SimpleNamespace
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest

from tankai.web.server import ThreadedHTTPServer, _shutdown_handler
from tankai.web import server as web_server
from tankai.core.request_dispatch import MonetaryDispatch
from tankai.core.monetary_budget import MonetaryBudget
from test_request_dispatch import sdks, NOW
from test_web_owner_pilot import accounts, ledger, provider_factory, pilot, login, serving, calls


def blocked_server():
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            entered.set()
            assert release.wait(5), 'test handler was not released'
            body = b'completed-before-storage-close'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    app = SimpleNamespace(close=closed.set)
    server = ThreadedHTTPServer(('127.0.0.1', 0), Handler, app=app)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    return server, serving, entered, release, closed


def fetch(server):
    with urlopen(f'http://127.0.0.1:{server.server_address[1]}/', timeout=5) as response:
        return response.read()


def test_server_close_waits_for_the_response_before_closing_application():
    server, serving, entered, release, closed = blocked_server()
    with ThreadPoolExecutor(max_workers=2) as pool:
        response = pool.submit(fetch, server)
        try:
            assert entered.wait(2)
            server.shutdown()
            closing = pool.submit(server.server_close)
            assert not closed.wait(0.1), 'application storage closed while its HTTP work was active'
            assert not closing.done()
            release.set()
            assert response.result(timeout=2) == b'completed-before-storage-close'
            closing.result(timeout=2)
            assert closed.is_set()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


def test_drain_timeout_never_closes_application_or_claims_success():
    server, serving, entered, release, closed = blocked_server()
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(fetch, server)
        try:
            assert entered.wait(2)
            with pytest.raises(TimeoutError):
                server.drain(timeout=0.01)
            assert not closed.is_set()
            assert server.draining is True
            release.set()
            assert response.result(timeout=2) == b'completed-before-storage-close'
            server.drain(timeout=1)
            assert not closed.is_set()  # Quiescence alone must not close storage.
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


def test_new_connections_receive_retryable_503_without_entering_handler():
    server, serving, entered, release, closed = blocked_server()
    try:
        assert server.drain(timeout=1)['scope'] == 'this-server-only'
        with pytest.raises(HTTPError) as rejected:
            fetch(server)
        assert rejected.value.code == 503
        assert rejected.value.headers['Retry-After'] == '1'
        assert rejected.value.headers['Cache-Control'] == 'no-store'
        assert json.loads(rejected.value.read())['error_code'] == 'server_draining'
        assert not entered.is_set() and not closed.is_set()
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_close_timeout_leaves_storage_open_and_can_be_retried_after_work_finishes():
    server, serving, entered, release, closed = blocked_server()
    server.drain_timeout = 0.01
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(fetch, server)
        try:
            assert entered.wait(2)
            server.shutdown()
            with pytest.raises(TimeoutError):
                server.server_close()
            assert not closed.is_set()
            release.set()
            assert response.result(timeout=2) == b'completed-before-storage-close'
            server.drain_timeout = 1
            server.server_close()
            assert closed.is_set()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


def test_idle_unparsed_socket_cannot_hold_storage_open_indefinitely():
    server, serving, entered, release, closed = blocked_server()
    server.request_timeout = 0.05
    connection = socket.create_connection(server.server_address, timeout=2)
    try:
        # Sending an incomplete header admits a worker without entering do_GET.
        connection.sendall(b'GET / HTTP/1.1\r\nX-Incomplete: ')
        server.shutdown()
        server.drain_timeout = 1
        server.server_close()
        assert closed.is_set() and not entered.is_set()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), 601])
def test_invalid_timeout_cannot_silently_begin_shutdown(value):
    server, serving, entered, release, closed = blocked_server()
    try:
        with pytest.raises(ValueError):
            server.drain(timeout=value)
        assert not server.draining and not closed.is_set()
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_repeated_shutdown_signals_cannot_interrupt_a_drain():
    server, serving, entered, release, closed = blocked_server()
    requested = threading.Event()
    handler = _shutdown_handler(server, requested)
    try:
        handler(signal.SIGTERM, None)
        assert requested.is_set() and server.draining
        handler(signal.SIGINT, None)
        handler(signal.SIGTERM, None)
        serving.join(timeout=2)
        assert not serving.is_alive()
        assert not closed.is_set()
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_failed_bind_does_not_close_a_callers_existing_application(monkeypatch):
    server, serving, entered, release, closed = blocked_server()
    try:
        def failed_bind(self):
            raise OSError('synthetic bind failure')
        monkeypatch.setattr(HTTPServer, 'server_bind', failed_bind)
        with pytest.raises(OSError):
            ThreadedHTTPServer(server.server_address, BaseHTTPRequestHandler, app=server.app)
        assert not closed.is_set()
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_rejected_slow_headers_cannot_block_the_accept_loop_indefinitely():
    server, serving, entered, release, closed = blocked_server()
    server.begin_draining()
    slow = socket.create_connection(server.server_address, timeout=2)
    try:
        slow.sendall(b'GET / HTTP/1.1\r\nPartial: ')
        started = time.monotonic()
        server.shutdown()
        assert time.monotonic() - started < 2.5
        server.server_close()
        assert not entered.is_set() and closed.is_set()
    finally:
        slow.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_request_thread_registration_cannot_be_missed_before_handler_start(monkeypatch):
    from socketserver import ThreadingMixIn
    server, serving, entered, release, closed = blocked_server()
    start_entered, permit_start = threading.Event(), threading.Event()
    original = ThreadingMixIn.process_request
    def delay_start(self, *args):
        start_entered.set()
        assert permit_start.wait(3)
        return original(self, *args)
    monkeypatch.setattr(ThreadingMixIn, 'process_request', delay_start)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(fetch, server)
        try:
            assert start_entered.wait(2)
            assert not entered.is_set()
            with pytest.raises(TimeoutError):
                server.drain(timeout=0.01)
            assert not closed.is_set()
            release.set()
            permit_start.set()
            assert response.result(timeout=2) == b'completed-before-storage-close'
        finally:
            release.set()
            permit_start.set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


@pytest.mark.parametrize('uncertain', [False, True])
def test_shutdown_preserves_real_http_owner_run_and_durable_money_state(
        accounts, ledger, provider_factory, monkeypatch, uncertain):
    admitted, release, close_started, app_closed = (threading.Event() for _ in range(4))
    original_execute = MonetaryDispatch.execute
    def pause_admitted_send(dispatch, **kwargs):
        original_send = kwargs['send_once']
        def send(parameters):
            if not admitted.is_set():
                assert ledger.snapshot()['held_micro_eur'] == 2000
                admitted.set()
                assert release.wait(8), 'admitted fixture was not released'
                if uncertain:
                    raise TimeoutError('synthetic uncertain upstream only')
            return original_send(parameters)
        kwargs['send_once'] = send
        return original_execute(dispatch, **kwargs)
    monkeypatch.setattr(MonetaryDispatch, 'execute', pause_admitted_send)
    app = web_server.AppContext.from_env('127.0.0.1', owner_pilot=pilot(accounts, ledger))
    original_close = app.close
    def observed_close():
        app_closed.set()
        original_close()
    monkeypatch.setattr(app, 'close', observed_close)
    server = ThreadedHTTPServer(('127.0.0.1', 0), web_server.Handler, app=app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    client, csrf = login(base)
    def close_server():
        close_started.set()
        server.server_close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        response = pool.submit(client.post, '/api/run', {'goal': 'Shutdown persistence fixture'}, csrf=csrf)
        try:
            assert admitted.wait(5)
            runtime = next(iter(app.runtimes._runtimes.values()))
            server.shutdown()
            closing = pool.submit(close_server)
            assert close_started.wait(1)
            assert not app_closed.wait(0.1) and not runtime._closed
            release.set()
            status, _, result = response.result(timeout=10)
            closing.result(timeout=10)
            assert app_closed.is_set() and runtime._closed
            if not uncertain:
                assert status == 200 and result['monetary_admission']['active_jobs'] == 0
                assert calls(provider_factory) == 5
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        state = reopened.snapshot()
        assert state['jobs'] == 1
        assert state['active_jobs'] == int(uncertain)
        assert state['held_micro_eur'] == (2000 if uncertain else 10_000)
    sends_before_restart = calls(provider_factory)
    with serving(pilot(accounts, ledger)) as (_, new_base):
        fresh, _ = login(new_base)
        code, _, preflight = fresh.get('/api/pilot/preflight')
        assert code == 200
        assert preflight['admission_checks_passed'] is (not uncertain)
        if uncertain:
            assert 'concurrent_job_limit' in preflight['blockers']
        else:
            code, _, history = fresh.get('/api/history')
            assert code == 200 and history[0]['goal'] == 'Shutdown persistence fixture'
        assert calls(provider_factory) == sends_before_restart


@pytest.mark.parametrize('close_fails', [False, True])
def test_main_drains_on_termination_restores_handlers_and_never_reports_false_success(
        monkeypatch, capsys, close_fails):
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    events = []
    app = SimpleNamespace(auth_mode='session', allow_registration=False,
                          auth=SimpleNamespace(user_count=lambda: 1), close=lambda: events.append('app-close'))
    class Server:
        draining = False
        def __init__(self, *args, **kwargs):
            pass
        def begin_draining(self):
            self.draining = True
        def request_shutdown(self):
            self.begin_draining()
        def serve_forever(self):
            events.append('serve')
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        def server_close(self):
            assert self.draining
            events.append('drain')
            # A second stop during cleanup must not unwind the drain.
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            if close_fails:
                raise TimeoutError('synthetic active request')
            app.close()
    monkeypatch.setattr(web_server.AppContext, 'from_env', classmethod(lambda cls, *a, **kw: app))
    monkeypatch.setattr(web_server, 'ThreadedHTTPServer', Server)
    if close_fails:
        with pytest.raises(TimeoutError):
            web_server.main()
        assert events == ['serve', 'drain']
    else:
        web_server.main()
        assert events == ['serve', 'drain', 'app-close']
    output = capsys.readouterr().out
    assert ('Stopped.' in output) is (not close_fails)
    assert {number: signal.getsignal(number) for number in previous} == previous


def test_stop_signal_during_thread_start_cannot_lose_or_double_count_work(monkeypatch):
    from socketserver import ThreadingMixIn
    server, serving, entered, release, closed = blocked_server()
    original = ThreadingMixIn.process_request
    requested = threading.Event()
    handler = _shutdown_handler(server, requested)
    def stop_before_thread_start(self, *args):
        handler(signal.SIGTERM, None)
        return original(self, *args)
    monkeypatch.setattr(ThreadingMixIn, 'process_request', stop_before_thread_start)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(fetch, server)
        try:
            assert entered.wait(2)
            with pytest.raises(TimeoutError):
                server.drain(timeout=0.01)
            release.set()
            assert response.result(timeout=2) == b'completed-before-storage-close'
            serving.join(timeout=2)
            assert not serving.is_alive()
            server.server_close()
            assert closed.is_set()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


def test_failed_worker_start_does_not_leave_a_phantom_request(monkeypatch):
    from socketserver import ThreadingMixIn
    server, serving, entered, release, closed = blocked_server()
    first, second = socket.socketpair()
    def failed_start(self, *args):
        raise RuntimeError('synthetic thread start failure')
    monkeypatch.setattr(ThreadingMixIn, 'process_request', failed_start)
    try:
        with pytest.raises(RuntimeError, match='thread start failure'):
            server.process_request(first, ('127.0.0.1', 0))
        assert server.drain(timeout=0.01)['active_http_requests'] == 0
        assert not entered.is_set() and not closed.is_set()
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_http_waiting_for_workspace_cannot_start_a_new_paid_job_after_drain(
        accounts, ledger, provider_factory, monkeypatch):
    app = web_server.AppContext.from_env('127.0.0.1', owner_pilot=pilot(accounts, ledger))
    server = ThreadedHTTPServer(('127.0.0.1', 0), web_server.Handler, app=app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client, csrf = login(f'http://127.0.0.1:{server.server_address[1]}')
    waiting = threading.Event()
    original = app.runtimes.acquire
    @contextmanager
    def observed_acquire(**kwargs):
        waiting.set()
        with original(**kwargs) as runtime:
            yield runtime
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with original(tenant_id=accounts.tenant, workspace_id=accounts.workspace):
                monkeypatch.setattr(app.runtimes, 'acquire', observed_acquire)
                response = pool.submit(client.post, '/api/run', {'goal': 'must never start'}, csrf=csrf)
                assert waiting.wait(2)
                server.begin_draining()
            status, _, body = response.result(timeout=8)
            assert status == 503 and body['error_code'] == 'server_draining'
            assert calls(provider_factory) == ledger.snapshot()['jobs'] == 0
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
