#!/usr/bin/env python3
"""Offline Docker rehearsal. Does not call or simulate Cloudflare snapshot APIs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time

LABEL = "com.tankai.ci.pilot-snapshot"


def docker(*args, timeout=30, check=True):
    result = subprocess.run(["docker", *args], text=True, encoding="utf-8",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError("pilot Docker operation failed: " + args[0])
    return result


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def committed_image_id(output):
    # Docker 29 can emit CLI notices on stdout before the immutable image ID.
    matches = [line.strip() for line in output.splitlines()
               if re.fullmatch(r"sha256:[0-9a-f]{64}", line.strip())]
    require(len(matches) == 1, "Docker commit did not return one immutable image ID")
    return matches[0]


def cleanup(scope):
    # Discover only this workflow's labelled resources; recheck before removing.
    containers = docker("ps", "-aq", "--filter", f"label={LABEL}={scope}").stdout.split()
    for container in containers:
        info = json.loads(docker("inspect", container).stdout)[0]
        require(info["Config"]["Labels"].get(LABEL) == scope, "foreign container cleanup blocked")
        docker("rm", "-f", container)
    images = docker("image", "ls", "-q", "--filter", f"label={LABEL}={scope}").stdout.split()
    for image in set(images):
        info = json.loads(docker("image", "inspect", image).stdout)[0]
        require(info["Config"]["Labels"].get(LABEL) == scope, "foreign image cleanup blocked")
        docker("image", "rm", info["Id"])


def launch(image, scope, *command):
    return docker("run", "-d", "--network", "none", "--cap-drop", "ALL",
                  "--security-opt", "no-new-privileges", "--label", f"{LABEL}={scope}",
                  image, *command).stdout.strip()


def state(container):
    return json.loads(docker("inspect", container).stdout)[0]["State"]


def status(container):
    result = docker("exec", container, "python", "-m", "tankai.web.pilot_supervisor", "status", check=False)
    if result.returncode:
        return None
    require(len(result.stdout) <= 2048, "oversized supervisor response")
    return json.loads(result.stdout)


def ready(container, phase):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        require(state(container)["Running"], "pilot container stopped before readiness")
        result = status(container)
        if result is not None:
            require(result["phase"] == phase, "unexpected pilot phase")
            return result
        time.sleep(.2)
    raise RuntimeError("pilot container readiness timed out")


def no_http(container):
    probe = "import socket; s=socket.socket(); s.settimeout(2); assert s.connect_ex(('127.0.0.1',8765)) != 0; s.close()"
    docker("exec", container, "python", "-c", probe)


def stopped(container):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        info = state(container)
        if not info["Running"]:
            require(info["ExitCode"] == 1, "unsafe fresh boot did not fail closed")
            return
        time.sleep(.1)
    raise RuntimeError("unsafe fresh boot did not stop")


def smoke(image, scope):
    image_id = json.loads(docker("image", "inspect", image).stdout)[0]["Id"]
    source = launch(image_id, scope, "fresh")
    before = ready(source, "running")
    require(not before["snapshot_ready"] and before["child_running"], "running pilot claims snapshot readiness")
    original_pid = state(source)["Pid"]
    quiesced = json.loads(docker("exec", source, "python", "-m", "tankai.web.pilot_supervisor",
                                "quiesce", timeout=170).stdout)
    require(quiesced["phase"] == "quiesced" and quiesced["snapshot_ready"], "pilot failed to quiesce")
    require(not quiesced["child_running"] and state(source)["Pid"] == original_pid, "supervisor did not stay alive")
    no_http(source)
    # Exercise a filesystem-only copy, including a stale Unix socket. This is
    # deliberately Docker commit, NOT evidence that the native API is available.
    # Commit pauses by default; the deprecated --pause flag itself emits a notice.
    snapshot = committed_image_id(docker("commit", "--change", 'CMD ["inspect"]', source, timeout=120).stdout)
    restored = launch(snapshot, scope)
    inspected = ready(restored, "inspect")
    require(not inspected["child_running"] and not inspected["snapshot_ready"], "inspection activated work")
    require(inspected["root_id"] == quiesced["root_id"], "root identity changed in copy")
    require(inspected["root_sha256"] == quiesced["root_sha256"], "root bytes changed in copy")
    no_http(restored)
    # A restored copy may not use the fresh-start path, even if its caller tries.
    accidental = launch(snapshot, scope, "fresh")
    stopped(accidental)
    # Ordinary restart also must not silently recreate or resume a working root.
    docker("restart", "--time", "5", source, timeout=30)
    stopped(source)
    return {"result": "PASS", "scope": "local-offline-docker-only", "image_id": image_id,
            "web_child_drained": True, "supervisor_stayed_alive": True,
            "copied_root_sha256": quiesced["root_sha256"], "inspection_only": True,
            "fresh_copy_and_restart_blocked": True, "native_cloudflare_snapshot_tested": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    scope = hashlib.sha256(args.scope.encode()).hexdigest()[:24]
    if args.cleanup:
        cleanup(scope)
        return
    if not args.image:
        parser.error("--image required")
    try:
        print(json.dumps(smoke(args.image, scope), sort_keys=True))
    finally:
        cleanup(scope)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Fixed messages from this script only; never dump Docker's environment/logs.
        detail = str(error) if type(error) is RuntimeError else type(error).__name__
        print("pilot container smoke: FAIL: " + detail, file=sys.stderr)
        raise SystemExit(1)
