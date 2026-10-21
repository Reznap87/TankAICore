#!/usr/bin/env python3
"""Verify the real private router image without provider keys or external I/O.

The config-only check needs Compose, but no running engine. The full check pulls
the pinned image, starts one disposable network-isolated container, checks its
effective user and /api/ping from inside, and removes only that container.
Never prints router logs: first-boot logs may contain an administrative setup code.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/tashfeenahmed/freellmapi@sha256:c3b92211cc0ada65e6044e8a17d3878ca27f20af8ca74cfdc4a4d51aa3f6ac63"


def docker(*args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=timeout, check=check)


def configuration() -> dict:
    env = dict(os.environ)
    # Disposable fixture, never a real installation's encryption or API key.
    env["FREELLMAPI_ENCRYPTION_KEY"] = "0" * 64
    env["FREELLMAPI_UNIFIED_KEY"] = ""
    result = subprocess.run([
        "docker", "compose", "-p", "tankai-freellm-config-check",
        "-f", "docker-compose.yml", "-f", "docker-compose.freellmapi.yml",
        "config", "--no-env-resolution", "--format", "json",
    ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=True)
    service = json.loads(result.stdout)["services"]["freellmapi"]
    assert service["image"] == IMAGE, "Router image must stay pinned"
    assert service.get("user") == "node", "Router must start directly as unprivileged node"
    assert service.get("cap_drop") == ["ALL"], "Keep all capabilities dropped"
    assert not service.get("cap_add"), "Do not add startup capabilities"
    assert "no-new-privileges:true" in service.get("security_opt", []), "Keep privilege protection"
    assert all(port.get("host_ip") == "127.0.0.1" for port in service["ports"]), "Keep router private"
    return service


def smoke(service: dict) -> None:
    name = "tankai-freellm-startup-" + uuid4().hex
    created = False
    try:
        docker("pull", IMAGE, timeout=180)
        docker("create", "--name", name, "--network", "none",
               "--user", service["user"], "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--pids-limit", "128",
               "--env", "NODE_ENV=production", "--env", "PORT=3001",
               "--env", "ENCRYPTION_KEY=" + "0" * 64,
               "--env", "FREELLMAPI_UPDATE_CHECK=off",
               "--env", "HEALTH_CHECK_INTERVAL_MS=86400000", IMAGE)
        created = True
        docker("start", name)
        # No host port, credential, provider, persistent volume or network access.
        identity = docker("exec", name, "node", "-e",
            "const fs=require('fs');const uid=process.getuid();"
            "const caps=fs.readFileSync('/proc/self/status','utf8').match(/^CapEff:\\s*(.*)$/m)[1];"
            "console.log(JSON.stringify({uid,caps}));if(uid===0||BigInt('0x'+caps)!==0n)process.exit(1)")
        uid = json.loads(identity.stdout)["uid"]
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            state = json.loads(docker("inspect", "--format", "{{json .State}}", name).stdout)
            if not state.get("Running"):
                raise RuntimeError("Router exited before healthcheck; exit=" + str(state.get("ExitCode")))
            health = docker("exec", name, "node", "-e",
                "fetch('http://127.0.0.1:3001/api/ping',{signal:AbortSignal.timeout(3000)})"
                ".then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                timeout=8, check=False)
            if health.returncode == 0:
                print(json.dumps({"router_startup": "passed", "uid": uid,
                    "effective_capabilities": "none", "network": "none",
                    "provider_credentials": False, "inference_requested": False}))
                return
            time.sleep(2)
        raise RuntimeError("Router healthcheck timed out")
    finally:
        # The exact generated name is local to this run; no existing service or volume.
        if created:
            docker("rm", "--force", name, timeout=30, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args()
    try:
        service = configuration()
        print("Private router Compose configuration passed.")
        if not args.config_only:
            smoke(service)
        return 0
    except (AssertionError, RuntimeError) as error:
        print(str(error))
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Docker configuration or startup check failed; router logs and credentials omitted.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
