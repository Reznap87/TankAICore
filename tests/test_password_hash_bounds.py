"""Stored password hashes cannot select unbounded work or decoded buffers."""
from __future__ import annotations

import pytest

from tankai.web import auth


PASSWORD = "A-valid-password-123"
SALT = "12" * 16
DIGEST = "ab" * 32
CANONICAL = f"scrypt$16384$8$1${SALT}${DIGEST}"


@pytest.mark.parametrize(
    "encoded",
    [
        None, b"not-text", "", "scrypt$16384$8$1",
        f"argon2$16384$8$1${SALT}${DIGEST}",
        *[
            f"scrypt${n}${r}${p}${SALT}${DIGEST}"
            for n, r, p in [
                ("32768", "8", "1"), ("16385", "8", "1"),
                ("16384", "0", "1"), ("16384", "9", "1"),
                ("16384", "8", "2"), ("16384", "8", "0"),
                ("16384", "8", "999999999999999999999999999999"),
                ("016384", "8", "1"), ("+16384", "8", "1"),
                (" 16384", "8", "1"), ("16384", "08", "1"),
                ("16384", "8", "1.0"), ("16384", "8", "True"),
                ("16384", "8", "\u0661"),
            ]
        ],
        f"scrypt$16384$8$1${SALT[:-1]}${DIGEST}",
        f"scrypt$16384$8$1${SALT * 1000}${DIGEST}",
        f"scrypt$16384$8$1${SALT}${DIGEST * 1000}",
        f"scrypt$16384$8$1${SALT}$",
        f"scrypt$16384$8$1${SALT}${DIGEST[:-1]}",
        f"scrypt$16384$8$1${SALT}${DIGEST[:-1]}z",
        f"scrypt$16384$8$1${SALT}${DIGEST.upper()}",
        CANONICAL + "$extra", CANONICAL + "\n",
    ],
    ids=lambda encoded: f"{type(encoded).__name__}-length-{len(encoded) if isinstance(encoded, (str, bytes)) else 'none'}",
)
def test_invalid_hash_is_rejected_without_scrypt(monkeypatch, encoded):
    def forbidden(*args, **kwargs):
        pytest.fail("Malformed stored hash reached scrypt")

    monkeypatch.setattr(auth.hashlib, "scrypt", forbidden)
    assert auth.verify_password(PASSWORD, encoded) is False


@pytest.mark.parametrize("password", [None, 123, b"text", "a" * 257])
def test_invalid_or_oversized_password_is_rejected_before_scrypt(monkeypatch, password):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid password reached scrypt")

    monkeypatch.setattr(auth.hashlib, "scrypt", forbidden)
    assert auth.verify_password(password, CANONICAL) is False


def test_canonical_hash_uses_only_fixed_bounded_scrypt_parameters(monkeypatch):
    calls = []

    def fake_scrypt(password, **kwargs):
        calls.append((password, kwargs))
        return bytes.fromhex(DIGEST)

    monkeypatch.setattr(auth.hashlib, "scrypt", fake_scrypt)
    assert auth.verify_password(PASSWORD, CANONICAL)
    assert calls == [(PASSWORD.encode("utf-8"), {
        "salt": bytes.fromhex(SALT), "n": 16384, "r": 8, "p": 1, "dklen": 32,
    })]


def test_existing_generated_hash_still_authenticates_and_wrong_password_fails():
    encoded = auth.hash_password(PASSWORD)
    assert auth.verify_password(PASSWORD, encoded)
    assert not auth.verify_password("A-wrong-password-123", encoded)


def test_scrypt_resource_failure_is_rejected(monkeypatch):
    def exhausted(*args, **kwargs):
        raise MemoryError("bounded allocation unavailable")

    monkeypatch.setattr(auth.hashlib, "scrypt", exhausted)
    assert auth.verify_password(PASSWORD, CANONICAL) is False
