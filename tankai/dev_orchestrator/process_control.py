"""Bounded process execution with cooperative cancellation.

The worker runner invokes explicit argv arrays only. This module keeps process
termination behavior consistent for host-side checks and OCI runtime clients.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int | None
    output: str
    timed_out: bool


def _terminate_process_group(process: subprocess.Popen[bytes], *, grace_seconds: float = 1.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=max(0.05, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float,
    output_limit: int = 10_000,
    cancellation_check: Callable[[], None] | None = None,
    poll_interval_seconds: float = 0.1,
) -> BoundedProcessResult:
    """Run one argv command and actively terminate it on timeout/cancellation.

    ``cancellation_check`` may raise any exception. The process group is
    terminated before the original exception is re-raised.
    """

    if not argv:
        raise ValueError("Leerer Prozessbefehl")
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name == "posix"),
    )
    buffer = bytearray()

    def drain() -> None:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > output_limit:
                del buffer[:-output_limit]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        while process.poll() is None:
            if cancellation_check is not None:
                cancellation_check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_group(process)
                break
            try:
                process.wait(timeout=min(max(0.01, poll_interval_seconds), remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        reader.join(timeout=10)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
    output = bytes(buffer[-output_limit:]).decode("utf-8", errors="replace")
    return BoundedProcessResult(
        returncode=process.returncode,
        output=output,
        timed_out=timed_out,
    )
