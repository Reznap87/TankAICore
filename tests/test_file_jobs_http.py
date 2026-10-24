import json
import threading
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from tankai.web import server as web_server
from test_file_jobs import opts, upload
from test_web_server import Client, _configure


@pytest.fixture
def web(tmp_path, monkeypatch):
    store = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv('TANKAI_FILE_JOBS_ENABLED', '1')
    monkeypatch.setenv('TANKAI_EMBEDDER', 'hashing')
    for name in ('first', 'other'):
        store.create_user_with_tenant(email=name + '@example.com', password='File-job-password-123',
                                    display_name=name, tenant_name=name)
    app = web_server.AppContext.from_env('127.0.0.1')
    # CSV routes must not even acquire a model runtime.
    def forbid(**kwargs):
        raise AssertionError('A CSV operation attempted to acquire a model runtime')
    monkeypatch.setattr(app.runtimes, 'acquire', forbid)
    server = web_server.ThreadedHTTPServer(('127.0.0.1', 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        yield app, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def login(base, name='first'):
    client = Client(base)
    status, _, me = client.post('/api/auth/login', {'email': name + '@example.com', 'password': 'File-job-password-123'})
    assert status == 200 and me['file_jobs_enabled']
    client.opener.addheaders.append(('X-TankAI-Workspace', me['workspace']['id']))
    return client, me['csrf_token']


def test_http_auth_csrf_scoping_and_confirmed_download(web):
    app, base = web
    anonymous = Client(base)
    assert anonymous.get('/api/file-jobs')[0] == 401
    client, csrf = login(base)
    assert client.post('/api/file-jobs', upload())[0] == 403
    status, _, job = client.post('/api/file-jobs', upload(), csrf=csrf)
    assert status == 201
    path = '/api/file-jobs/' + job['id']
    assert client.get(path + '/csv')[0] == 409
    other, other_csrf = login(base, 'other')
    assert other.get(path)[0] == 404
    assert other.post(path + '/delete', {}, csrf=other_csrf)[0] == 404
    status, _, job = client.post(path + '/preview', {'revision': 1, 'options': opts(trim=True, removeDuplicates=True)}, csrf=csrf)
    assert status == 200
    assert client.post(path + '/complete', {'revision': 1}, csrf=csrf)[0] == 409
    assert client.post(path + '/complete', {'revision': job['revision']}, csrf=csrf)[0] == 200
    with client.opener.open(Request(base + path + '/csv'), timeout=5) as response:
        assert response.headers['Cache-Control'] == 'no-store'
        assert "script-src 'none'" in response.headers['Content-Security-Policy']
        assert 'attachment;' in response.headers['Content-Disposition']
        assert response.read() == b'\xef\xbb\xbfid,name\r\n001,A\r\n'
    assert client.get(path + '/report')[2]['externalModelCalls'] == 0
    assert client.post(path + '/delete', {}, csrf=csrf)[0] == 200
    assert client.get(path)[0] == 404


def test_workspace_switch_or_missing_binding_blocks_upload(web):
    _, base = web
    client, csrf = login(base)
    client.opener.addheaders = [('X-TankAI-Workspace', 'different-workspace')]
    assert client.post('/api/file-jobs', upload(), csrf=csrf)[0] == 409
    assert client.get('/api/file-jobs')[0] == 409
    client.opener.addheaders = []
    assert client.post('/api/file-jobs', upload(), csrf=csrf)[0] == 409
    assert client.get('/api/file-jobs')[2]['jobs'] == []


def test_disabled_feature_preserves_existing_login_contract(web):
    app, base = web
    app.file_jobs = None
    client = Client(base)
    status, _, me = client.post('/api/auth/login', {
        'email': 'first@example.com', 'password': 'File-job-password-123',
    })
    assert status == 200
    assert 'file_jobs_enabled' not in me
    assert client.get('/api/file-jobs')[0] == 404
    assert client.get('/files')[0] == 404


def test_private_ui_nonce_and_untrusted_preview_text(web):
    _, base = web
    client, csrf = login(base)
    with client.opener.open(base + '/files', timeout=5) as response:
        html = response.read().decode()
        csp = response.headers['Content-Security-Policy']
        assert 'nonce-' in csp and 'unsafe-inline' not in csp
        assert 'innerHTML' not in html
        assert 'Deine Dateiaufträge' in html and '__FILE_JOB_SCRIPT__' not in html
    malicious = '<img src=x onerror=alert(1)>'
    status, _, job = client.post('/api/file-jobs', upload(source='name\n' + malicious + '\n'), csrf=csrf)
    assert status == 201
    detail = client.get('/api/file-jobs/' + job['id'])[2]
    assert detail['preview']['rows'] == [[malicious]]
    assert '<img' not in web_server.FILE_JOBS_TEMPLATE.split('<script')[1]


def test_feature_is_off_by_default_and_cannot_be_public(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)
    monkeypatch.delenv('TANKAI_FILE_JOBS_ENABLED', raising=False)
    app = web_server.AppContext.from_env('127.0.0.1')
    assert app.file_jobs is None
    app.close()
    monkeypatch.setenv('TANKAI_FILE_JOBS_ENABLED', '1')
    for host in ('0.0.0.0', '192.0.2.1'):
        with pytest.raises(RuntimeError, match='privaten Loopback'):
            web_server.AppContext.from_env(host)
    monkeypatch.setenv('TANKAI_AUTH_MODE', 'disabled')
    with pytest.raises(RuntimeError, match='Anmeldung'):
        web_server.AppContext.from_env('127.0.0.1')
