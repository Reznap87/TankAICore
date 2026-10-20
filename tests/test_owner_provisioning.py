import base64
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tankai.web.auth import verify_password


SCRIPT = Path(__file__).parents[1] / ".github/scripts/provision_owner.py"
spec = importlib.util.spec_from_file_location("provision_owner", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.skipif(not shutil.which("openssl"), reason="OpenSSL is required for encrypted delivery")
def test_provisioning_encrypts_delivery_and_never_prints_secrets(monkeypatch, tmp_path, capsys):
    real_run = subprocess.run
    private = real_run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"], check=True, capture_output=True).stdout
    public = real_run(["openssl", "pkey", "-pubout", "-outform", "DER"], input=private, check=True, capture_output=True).stdout
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.test")
    monkeypatch.setenv("OWNER_PUBLIC_KEY", base64.b64encode(public).decode())
    installed = {}

    def run(command, **kwargs):
        if command[:4] == ["npx", "--no-install", "wrangler", "secret"]:
            if command[4] == "list":
                return subprocess.CompletedProcess(command, 0, "[]", "")
            installed.update(json.loads(kwargs["input"]))
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    output = capsys.readouterr().out
    encrypted = base64.b64decode(output.split("TANKAI_OWNER_CREDENTIALS_RSA_OAEP_SHA256=", 1)[1].splitlines()[0])
    key = tmp_path / "fixture-private.pem"
    key.write_bytes(private)
    delivery = real_run(["openssl", "pkeyutl", "-decrypt", "-inkey", str(key), "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256", "-pkeyopt", "rsa_mgf1_md:sha256"], input=encrypted, check=True, capture_output=True).stdout
    credentials = json.loads(delivery)
    owner = json.loads(installed["TANKAI_BOOTSTRAP_OWNER"])
    assert credentials["email"] == owner["email"] == "owner@example.test"
    assert verify_password(credentials["password"], owner["password_hash"])
    for secret in [credentials["password"], owner["password_hash"], installed["TANKAI_AUTH_STATE_TOKEN"]]:
        assert secret not in output


def test_provisioning_refuses_to_replace_existing_owner_secrets(monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.test")
    monkeypatch.setenv("OWNER_PUBLIC_KEY", base64.b64encode(b"x" * 300).decode())
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '[{"name":"TANKAI_AUTH_STATE_TOKEN"}]', "")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        module.main()
    assert len(calls) == 1
