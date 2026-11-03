"""Restored CSV storage serves real authenticated requests in a fresh app context."""
import secrets
import threading
from urllib.request import Request

from tankai.web import file_backup as backup
from tankai.web import server as web_server
from test_file_jobs import opts, upload
from test_file_jobs_http import login, web


def test_fresh_app_resumes_saved_choice_and_serves_unchanged_deliveries(web, tmp_path, monkeypatch):
    app, base = web
    client, csrf = login(base)
    _, _, draft = client.post('/api/file-jobs', upload(name='draft.csv'), csrf=csrf)
    draft_path = '/api/file-jobs/' + draft['id']
    _, _, draft = client.post(draft_path + '/preview', {'revision': 1, 'options': opts(trim=True)}, csrf=csrf)
    _, _, done = client.post('/api/file-jobs', upload(name='complete.csv'), csrf=csrf)
    done_path = '/api/file-jobs/' + done['id']
    assert client.post(done_path + '/complete', {'revision': 1}, csrf=csrf)[0] == 200
    original_report = client.get(done_path + '/report')[2]
    with client.opener.open(Request(base + done_path + '/csv'), timeout=5) as response:
        original_csv = response.read()

    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'jobs.enc'
    backup.create_backup(app.file_jobs.path, encrypted, key)
    restored_root = tmp_path / 'new-data-root'
    backup.restore_backup(encrypted, restored_root, key)
    # Authentication has its own storage/recovery contract. Keep its original identities;
    # only CSV/workspace storage is replaced by the new restored data root.
    monkeypatch.setenv('TANKAI_DATA_ROOT', str(restored_root))
    fresh_app = web_server.AppContext.from_env('127.0.0.1')
    def no_model(**kwargs):
        raise AssertionError('Restore verification attempted model inference')
    monkeypatch.setattr(fresh_app.runtimes, 'acquire', no_model)
    server = web_server.ThreadedHTTPServer(('127.0.0.1', 0), web_server.Handler, app=fresh_app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        new_base = f'http://127.0.0.1:{server.server_address[1]}'
        fresh_client, fresh_csrf = login(new_base)
        status, _, restored_draft = fresh_client.get(draft_path)
        assert status == 200 and restored_draft['revision'] == draft['revision']
        assert restored_draft['options']['trim'] and not restored_draft['completed']
        assert fresh_client.post(draft_path + '/complete', {'revision': draft['revision']}, csrf=fresh_csrf)[0] == 200
        assert fresh_client.get(done_path + '/report')[2] == original_report
        with fresh_client.opener.open(Request(new_base + done_path + '/csv'), timeout=5) as response:
            assert response.read() == original_csv
        other, other_csrf = login(new_base, 'other')
        assert other.get(done_path)[0] == 404
        assert other.get(done_path + '/report')[0] == 404
        assert other.post(done_path + '/delete', {}, csrf=other_csrf)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
