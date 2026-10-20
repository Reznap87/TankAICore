#!/usr/bin/env python3
"""Generate a first owner; publish only RSA-OAEP encrypted delivery material.

Run by the protected, exact-SHA production workflow. No passwords, password
hashes, storage tokens, or Cloudflare credentials are written to repository files
or emitted to Actions logs. Repeated provisioning refuses to replace secrets.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from uuid import uuid4


def main() -> None:
    email = os.environ.get("OWNER_EMAIL", "").strip().casefold()
    public_key = os.environ.get("OWNER_PUBLIC_KEY", "").strip()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise SystemExit("A valid owner email is required")
    try:
        der = base64.b64decode(public_key, validate=True)
    except ValueError:
        raise SystemExit("Invalid RSA public key") from None
    if not 250 <= len(der) <= 2000:
        raise SystemExit("Invalid RSA public key length")
    wrangler = ["npx", "--no-install", "wrangler", "secret"]
    listing = subprocess.run(wrangler + ["list", "--format", "json"], check=True, capture_output=True, text=True)
    existing = {item["name"] for item in json.loads(listing.stdout)}
    if existing & {"TANKAI_BOOTSTRAP_OWNER", "TANKAI_AUTH_STATE_TOKEN"}:
        raise SystemExit("Owner configuration already exists; refusing to overwrite it")
    password = secrets.token_urlsafe(24)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    config = {
        "email": email,
        "password_hash": f"scrypt$16384$8$1${salt.hex()}${digest.hex()}",
        "user_id": str(uuid4()), "tenant_id": str(uuid4()), "workspace_id": str(uuid4()),
    }
    delivery = json.dumps({"email": email, "password": password}, separators=(",", ":")).encode()
    with tempfile.NamedTemporaryFile(suffix=".der") as key:
        key.write(der)
        key.flush()
        encrypted = subprocess.run([
            "openssl", "pkeyutl", "-encrypt", "-pubin", "-keyform", "DER", "-inkey", key.name,
            "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
            "-pkeyopt", "rsa_mgf1_md:sha256",
        ], input=delivery, capture_output=True, check=True).stdout
    # The ciphertext can safely be in public CI logs. Retain the matching private
    # key outside GitHub; no unencrypted recovery artifact is ever uploaded.
    print("TANKAI_OWNER_CREDENTIALS_RSA_OAEP_SHA256=" + base64.b64encode(encrypted).decode(), flush=True)
    payload = json.dumps({"TANKAI_BOOTSTRAP_OWNER": json.dumps(config), "TANKAI_AUTH_STATE_TOKEN": secrets.token_urlsafe(32)})
    result = subprocess.run(wrangler + ["bulk"], input=payload, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit("Owner secret installation failed; inspect Cloudflare access, not secret values")
    print("Owner configuration installed; initial password delivered only as RSA ciphertext.")


if __name__ == "__main__":
    main()
