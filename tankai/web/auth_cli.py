"""Administrative CLI für TankAI-Benutzerkonten."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .auth import AuthStore


def _store(args: argparse.Namespace) -> AuthStore:
    return AuthStore(Path(args.db), session_hours=int(os.environ.get("TANKAI_SESSION_HOURS", "12")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TankAI Auth-Verwaltung")
    parser.add_argument("--db", default=os.environ.get("TANKAI_AUTH_DB", ".tankai/data/auth.db"))
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="Erstellt Nutzer, Mandant und Standard-Workspace")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--tenant", required=True)
    create.add_argument("--workspace", default="Standard")
    create.add_argument("--password-stdin", action="store_true")

    listing = sub.add_parser("list-users", help="Listet vorhandene Nutzer")
    listing.set_defaults(command="list-users")

    reset = sub.add_parser("set-password", help="Setzt Passwort und widerruft alle Sessions")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password-stdin", action="store_true")
    return parser


def _read_password(stdin: bool) -> str:
    if stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Passwort: ")
        confirmation = getpass.getpass("Passwort wiederholen: ")
        if password != confirmation:
            raise ValueError("Passwörter stimmen nicht überein")
    return password


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store(args)
    try:
        if args.command == "create-user":
            user_id, tenant_id, workspace_id = store.create_user_with_tenant(
                email=args.email,
                password=_read_password(args.password_stdin),
                display_name=args.name,
                tenant_name=args.tenant,
                workspace_name=args.workspace,
            )
            print(json.dumps({"user_id": user_id, "tenant_id": tenant_id, "workspace_id": workspace_id}, indent=2))
            return 0
        if args.command == "list-users":
            print(json.dumps(store.list_users(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "set-password":
            user_id = store.get_user_id_by_email(args.email)
            if not user_id:
                raise ValueError("Nutzer nicht gefunden")
            store.set_password(user_id, _read_password(args.password_stdin))
            print("Passwort geändert; alle Sessions wurden widerrufen.")
            return 0
    except ValueError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
