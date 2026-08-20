"""Container runtime security preflight for TankAI worker hosts."""

from __future__ import annotations

import argparse
import json

from .container_runtime import ContainerRuntimeError, DockerCommandExecutor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tankai-runtime-doctor")
    parser.add_argument("--container-runtime", default="docker")
    parser.add_argument(
        "--allow-rootful",
        action="store_true",
        help="Nur lokale Entwicklung: hebt die Rootless-Pflicht des Checks auf",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    executor = DockerCommandExecutor(
        args.container_runtime,
        require_rootless=not args.allow_rootful,
    )
    try:
        version = executor.ensure_available()
        profile = executor.inspect_security_profile(server_version=version)
    except ContainerRuntimeError as exc:
        print(json.dumps({
            "ok": False,
            "runtime": args.container_runtime,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({
        "ok": True,
        "runtime": profile.runtime,
        "server_version": profile.server_version,
        "rootless": profile.rootless,
        "os_type": profile.os_type,
        "cgroup_version": profile.cgroup_version,
        "security_options": list(profile.security_options),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
