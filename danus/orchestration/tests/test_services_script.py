"""Isolated process-level regressions for ``scripts/services.sh``.

The harness copies the script beside a stub environment and service, so these
tests never read or mutate the checkout's live ``config/`` or ``runtime/``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan can briefly remain as a zombie after SIGTERM.  It is no longer
    # running even though kill(2)'s existence probe still succeeds.
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(probe.stdout.strip()) and not probe.stdout.lstrip().startswith("Z")


def _make_harness(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    services = scripts / "services.sh"
    shutil.copy2(ROOT / "scripts" / "services.sh", services)

    runtime = tmp_path / "runtime"
    marker = tmp_path / "service.identity"
    service = tmp_path / "service.py"
    service.write_text(
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(\n"
        "    f'{os.getpid()} {os.getpgrp()} {os.getsid(0)}\\n', encoding='ascii'\n"
        ")\n"
        "while True:\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    (scripts / "start-verify.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'exec "$DANUS_PY" "$DANUS_TEST_SERVICE" "$DANUS_TEST_MARKER"\n',
        encoding="utf-8",
    )
    (scripts / "env.sh").write_text(
        'DANUS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
        'DANUS_RUNTIME="${DANUS_TEST_RUNTIME:?}"\n'
        'DANUS_PY="${DANUS_TEST_PY:?}"\n'
        "VERIFY_PORT=65534\n"
        "export DANUS_ROOT DANUS_RUNTIME DANUS_PY VERIFY_PORT\n"
        "danus_verify_health(){ echo down; return 5; }\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        DANUS_TEST_RUNTIME=str(runtime),
        DANUS_TEST_PY=sys.executable,
        DANUS_TEST_SERVICE=str(service),
        DANUS_TEST_MARKER=str(marker),
    )
    return services, env, runtime, marker


@pytest.mark.parametrize("launcher", ["python_setsid", "forking_setsid"])
def test_service_detaches_and_pidfile_tracks_real_service(
    tmp_path: Path, launcher: str
) -> None:
    services, env, runtime, marker = _make_harness(tmp_path)
    if launcher == "python_setsid":
        # An explicitly empty override exercises the macOS path even on Linux.
        env["DANUS_SETSID_BIN"] = ""
    else:
        # Emulate a setsid implementation that forks.  The parent shell's $!
        # is then deliberately not the service pid; the in-session pid write
        # must still identify the process that ultimately execs the service.
        fake_setsid = tmp_path / "forking-setsid"
        fake_setsid.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "if os.fork():\n"
            "    os._exit(0)\n"
            "os.setsid()\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n",
            encoding="utf-8",
        )
        fake_setsid.chmod(0o755)
        env["DANUS_SETSID_BIN"] = str(fake_setsid)

    pid: int | None = None
    try:
        up = subprocess.run(
            ["bash", str(services), "up", "verify"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert up.returncode == 0, up.stdout + up.stderr
        assert _wait_until(marker.exists), up.stdout + up.stderr

        pidfile = runtime / "run" / "verify.pid"
        pid = int(pidfile.read_text(encoding="ascii").strip())
        service_pid, process_group, session = map(
            int, marker.read_text(encoding="ascii").split()
        )
        assert pid == service_pid
        assert process_group == pid
        assert session == pid
        assert _running(pid), "service must survive the shell that ran `up`"

        down = subprocess.run(
            ["bash", str(services), "down", "verify"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert down.returncode == 0, down.stdout + down.stderr
        assert _wait_until(lambda: not _running(pid))
        assert not pidfile.exists()
        pid = None
    finally:
        if pid is not None and _running(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
