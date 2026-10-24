"""Independent expected CSV outputs, persistence, boundedness and concurrent writes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import csv
import io
import random
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tankai.web.file_jobs import DEFAULT_OPTIONS, FileJobError, FileJobStore, parse_csv, preview_csv


def opts(**changes):
    return {**DEFAULT_OPTIONS, **changes}


# These expected strings are deliberately not produced by the implementation's writer.
CASES = [
    ("leading-zeros", "id;value\n0001;01.20\n0002;2026-01-02\n", opts(),
     "id;value\r\n0001;01.20\r\n0002;2026-01-02\r\n", (2, 0, 0, 0)),
    ("quoted-separator", 'id,text\n1,"a,b"\n', opts(), 'id,text\r\n1,"a,b"\r\n', (1, 0, 0, 0)),
    ("escaped-quote", 'id,text\n1,"say ""yes"""\n', opts(), 'id,text\r\n1,"say ""yes"""\r\n', (1, 0, 0, 0)),
    ("embedded-newline", 'id,text\r\n1,"a\r\nb"\r\n', opts(), 'id,text\r\n1,"a\r\nb"\r\n', (1, 0, 0, 0)),
    ("unicode-bom", '\ufeffid\tOrt\n007\tKöln 🌿\n', opts(), 'id\tOrt\r\n007\tKöln 🌿\r\n', (1, 0, 0, 0)),
    ("physical-and-empty-rows", 'id,name\n\n,\n1,A\n', opts(removeBlank=True), 'id,name\r\n1,A\r\n', (1, 2, 0, 0)),
    ("dedup-after-trimming", 'id,name\n001, A \n001,A\n002,B\n', opts(trim=True, removeDuplicates=True),
     'id,name\r\n001,A\r\n002,B\r\n', (2, 0, 1, 1)),
    ("formula-header-and-values", '=id,value\n001,=1+1\n002,-25\n003,@x\n', opts(),
     "'=id,value\r\n001,'=1+1\r\n002,'-25\r\n003,'@x\r\n", (3, 0, 0, 0)),
    ("explicit-protection-off", 'id,value\n001,-25\n002,+12\n', opts(protectFormulas=False),
     'id,value\r\n001,-25\r\n002,+12\r\n', (2, 0, 0, 0)),
    ("unchanged-spaces-and-duplicates", 'id,name\n001, A \n001, A \n', opts(),
     'id,name\r\n001, A \r\n001, A \r\n', (2, 0, 0, 0)),
]


@pytest.mark.parametrize("name,source,options,expected,counts", CASES, ids=[c[0] for c in CASES])
def test_ten_independent_delivery_cases(name, source, options, expected, counts):
    detail, content = preview_csv(source.encode('utf-8'), 'auto', options)
    assert detail['valid']
    assert content == ('\ufeff' + expected).encode('utf-8')
    report = detail['report']
    assert tuple(report[key] for key in ('outputRows', 'blankRemoved', 'duplicatesRemoved', 'changedCells')) == counts
    if name == 'formula-header-and-values':
        assert report['formulaCellsChangedOnCsvExport'] == 4


@pytest.mark.parametrize('source', [b'', b'\xff', b'a\x00\n1', b'a,b\n"open,x', b'a,b\nx"y,z',
    b'a,b\n"x"z,y', b'a\n' + b'x' * 100_001, b'a\n' + b'x\n' * 5001,
    ','.join('x' for _ in range(41)).encode(), b'x' * 2_000_001],
    ids=['empty', 'encoding', 'nul', 'unclosed', 'bare-quote', 'after-quote',
         'field-limit', 'row-limit', 'column-limit', 'byte-limit'])
def test_invalid_or_oversized_sources_are_rejected(source):
    with pytest.raises(FileJobError):
        parse_csv(source)


def test_ragged_rows_are_never_silently_dropped_or_repaired():
    detail, content = preview_csv(b'a,b\n1,2,3\n,\n', 'auto', opts(removeBlank=True))
    assert not detail['valid'] and content is None
    assert detail['findings']['unevenRows'] == 1
    assert detail['errors'] == ['Datensatz 2 hat nicht 2 Felder']
    # The wrong-width empty record must not disappear just because blank removal is on.
    detail, content = preview_csv(b'a,b\n,,\n', 'auto', opts(removeBlank=True))
    assert not detail['valid'] and content is None


def test_headers_and_exact_boolean_options_are_enforced():
    for source in (b'a,a\n1,2\n', b'a, \n1,2\n'):
        assert not preview_csv(source, 'auto', opts())[0]['valid']
    for options in ({}, opts(trim=1), opts(extra=True)):
        with pytest.raises(FileJobError):
            preview_csv(b'a\n1', 'auto', options)


def test_generated_quoted_records_roundtrip_without_value_coercion():
    rng = random.Random(6291)
    alphabet = ['a', '0', ' ', '"', ',', ';', '\t', '\r', '\n', 'ä', '🌿', '=', '\ufeff']
    for _ in range(100):
        delimiter = rng.choice([',', ';', '\t'])
        expected = [[str(i) + ''.join(rng.choices(alphabet, k=rng.randint(0, 20)))
                     for i in range(3)] for _ in range(4)]
        source = io.StringIO(newline='')
        csv.writer(source, delimiter=delimiter, lineterminator='\r\n').writerows([['a', 'b', 'c'], *expected])
        detail, content = preview_csv(source.getvalue().encode(), delimiter, opts(protectFormulas=False))
        assert detail['valid']
        assert list(csv.reader(io.StringIO(content.decode('utf-8-sig'), newline=''), delimiter=delimiter)) == [['a', 'b', 'c'], *expected]


@pytest.mark.parametrize('changes', [{'name': '../x.csv'}, {'name': 'x\ud800.csv'},
                                    {'goal': '\ud800'}, {'source': '\ud800'}],
                         ids=['path', 'filename-unicode', 'note-unicode', 'source-unicode'])
def test_invalid_text_does_not_create_a_job(tmp_path, changes):
    store = FileJobStore(tmp_path / 'jobs.db')
    identity = actor()
    with pytest.raises(FileJobError):
        store.create(identity, upload(**changes))
    assert store.list(identity) == []


def actor():
    return SimpleNamespace(tenant_id=str(uuid4()), workspace_id=str(uuid4()), user_id=str(uuid4()))


def upload(**changes):
    return {'requestId': str(uuid4()), 'name': 'kunden.csv', 'goal': 'Kennungen erhalten.',
            'source': 'id,name\n001, A \n001,A\n', 'delimiter': 'auto', **changes}


def test_persistence_exact_export_idempotency_and_restore(tmp_path):
    identity = actor()
    path = tmp_path / 'jobs.db'
    store = FileJobStore(path)
    data = upload()
    job = store.create(identity, data)
    assert store.create(identity, data)['id'] == job['id']
    with pytest.raises(FileJobError) as error:
        store.create(identity, {**data, 'goal': 'Changed'})
    assert error.value.status == 409
    job = store.update(identity, job['id'], {'revision': 1, 'options': opts(trim=True, removeDuplicates=True)})
    assert job['revision'] == 2
    # Reopening the store restores the selected draft and its original input.
    store = FileJobStore(path)
    assert store.get(identity, job['id'])['options']['removeDuplicates']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT source FROM csv_jobs').fetchone()[0] == data['source'].encode()
    assert store.update(identity, job['id'], {'revision': 2}, complete=True)['completed']
    assert store.update(identity, job['id'], {'revision': 2}, complete=True)['completed']
    content, kind, _ = store.download(identity, job['id'], 'csv')
    assert kind == 'text/csv; charset=utf-8'
    assert content == b'\xef\xbb\xbfid,name\r\n001,A\r\n'
    report = json.loads(store.download(identity, job['id'], 'report')[0])
    assert report['csvSha256'] == hashlib.sha256(content).hexdigest()
    assert report['sourceSha256'] == hashlib.sha256(data['source'].encode()).hexdigest()
    assert report['csvByteLength'] == len(content)
    assert report['externalModelCalls'] == 0 and not report['semanticCorrectnessVerified']
    assert 'rows' not in report
    with pytest.raises(FileJobError):
        store.update(identity, job['id'], {'revision': 2, 'options': opts()})
    # Exercise SQLite's snapshot API and load a separate restored database.
    backup = tmp_path / 'restored.db'
    with sqlite3.connect(path) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    restored = FileJobStore(backup)
    assert restored.download(identity, job['id'], 'csv')[0] == content
    assert json.loads(restored.download(identity, job['id'], 'report')[0]) == report


def test_all_operations_check_tenant_workspace_and_owner(tmp_path):
    store = FileJobStore(tmp_path / 'jobs.db')
    identity = actor()
    job_id = store.create(identity, upload())['id']
    for key in ('tenant_id', 'workspace_id', 'user_id'):
        other = SimpleNamespace(**{**vars(identity), key: str(uuid4())})
        assert store.list(other) == []
        for operation in (
            lambda: store.get(other, job_id),
            lambda: store.update(other, job_id, {'revision': 1, 'options': opts()}),
            lambda: store.update(other, job_id, {'revision': 1}, complete=True),
            lambda: store.download(other, job_id, 'csv'),
            lambda: store.delete(other, job_id),
        ):
            with pytest.raises(FileJobError) as error:
                operation()
            assert error.value.status == 404


def test_stale_preview_cannot_complete_newer_selection(tmp_path):
    store = FileJobStore(tmp_path / 'jobs.db')
    identity = actor()
    job_id = store.create(identity, upload())['id']
    store.update(identity, job_id, {'revision': 1, 'options': opts(trim=True)})
    with pytest.raises(FileJobError) as error:
        store.update(identity, job_id, {'revision': 1}, complete=True)
    assert error.value.status == 409
    with pytest.raises(FileJobError):
        store.download(identity, job_id, 'csv')
    for bad_revision in (True, '2', 0):
        with pytest.raises(FileJobError):
            store.update(identity, job_id, {'revision': bad_revision}, complete=True)


def test_concurrent_upload_quota_and_retry_are_atomic(tmp_path):
    path = tmp_path / 'jobs.db'
    store = FileJobStore(path)
    identity = actor()

    def attempt(_):
        try:
            return FileJobStore(path).create(identity, upload())['id']
        except FileJobError as error:
            assert error.status == 409
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = [value for value in pool.map(attempt, range(16)) if value]
    assert len(created) == 10 == len(store.list(identity))
    store.delete(identity, created[0])
    data = upload()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: FileJobStore(path).create(identity, data)['id'], range(4)))
    assert len(set(ids)) == 1
    assert len(store.list(identity)) == 10


def test_global_storage_quota_applies_before_completion(tmp_path, monkeypatch):
    from tankai.web import file_jobs
    store = FileJobStore(tmp_path / 'jobs.db')
    identity = actor()
    data = upload()
    job_id = store.create(identity, data)['id']
    monkeypatch.setattr(file_jobs, 'MAX_STORED_BYTES', len(data['source'].encode()))
    with pytest.raises(FileJobError):
        store.create(actor(), upload())
    with pytest.raises(FileJobError):
        store.update(identity, job_id, {'revision': 1}, complete=True)
    assert not store.get(identity, job_id)['completed']
