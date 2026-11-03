"""Password stdin works across Windows-to-Linux pipes without trimming spaces."""
import io
import json

import pytest

from tankai.web import auth_cli
from tankai.web.auth import AuthStore

PASSWORD = "Synthetic-stdin-password-123"


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_password_line_endings(ending, monkeypatch):
    monkeypatch.setattr(auth_cli.sys, "stdin", io.StringIO(PASSWORD+ending))
    assert auth_cli._read_password(True) == PASSWORD


@pytest.mark.parametrize("password", [" "+PASSWORD, PASSWORD+" ", PASSWORD+"\t"])
def test_password_spaces_are_not_silently_trimmed(password, monkeypatch):
    monkeypatch.setattr(auth_cli.sys, "stdin", io.StringIO(password+"\r\n"))
    assert auth_cli._read_password(True) == password


def test_create_and_reset_password_with_crlf_revoke_old_session(tmp_path, monkeypatch, capsys):
    database = tmp_path/"auth.db"
    monkeypatch.setattr(auth_cli.sys, "stdin", io.StringIO(PASSWORD+"\r\n"))
    assert auth_cli.main(["--db", str(database), "create-user", "--email", "stdin@example.test",
        "--name", "Synthetic", "--tenant", "Synthetic", "--password-stdin"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"user_id", "tenant_id", "workspace_id"}
    store = AuthStore(database)
    session = store.authenticate(email="stdin@example.test", password=PASSWORD)
    assert session is not None
    changed = "Different-synthetic-password-456"
    monkeypatch.setattr(auth_cli.sys, "stdin", io.StringIO(changed+"\r\n"))
    assert auth_cli.main(["--db", str(database), "set-password", "--email", "stdin@example.test", "--password-stdin"]) == 0
    assert store.resolve_session(session.token) is None
    assert store.authenticate(email="stdin@example.test", password=PASSWORD) is None
    assert store.authenticate(email="stdin@example.test", password=changed) is not None
    output = capsys.readouterr()
    assert PASSWORD not in output.out+output.err and changed not in output.out+output.err
