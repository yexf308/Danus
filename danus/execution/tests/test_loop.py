"""Offline tests for danus.execution.loop + __main__ (no real codex, no network).

Covers the round driver end-to-end without ever launching a real codex:

  - ``run_round`` against a FIXED fake-codex stub script: a chosen exit code, a
    hard-timeout (terminate → 124), and a missing binary (→ 127). These drive the
    real ``subprocess.Popen`` path in loop.py.
  - the ``main`` outer loop: stop-flag / deadline / max-rounds / consecutive-
    failure caps, the codex-missing (127) short-circuit, and the ``ok``/``error``
    status writes. ``run_round`` is monkeypatched so no subprocess spawns.
  - the SIGTERM handler (_on_term): terminates the in-flight child, writes
    ``terminated`` status, and exits 0.
  - __main__: ``runpy.run_module("danus.execution", run_name="__main__")`` with the
    loop entry patched, covering the argv guard + dispatch without spawning.
  - the remaining small error/edge branches in loop / layout / scaffold helpers.

Runs standalone (``python -m danus.execution.tests.test_loop``) and pytest.
"""

from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import pytest
from contextlib import contextmanager
from pathlib import Path

from danus.execution import layout as L
from danus.execution import loop, scaffold
from danus.hotjoin import HotJoinError


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    old_preflight = loop.require_gateway_runtime
    loop.require_gateway_runtime = lambda: None
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        loop.require_gateway_runtime = old_preflight
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _restore_sigterm():
    """main() installs a SIGTERM handler; save/restore so tests don't leak it."""
    old = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _mk_worker(tmp: Path, name: str = "high") -> L.WorkerLayout:
    """A minimal worker home under tmp: <tmp>/proj/workers/<name>."""
    wl = L.WorkerLayout(tmp / "proj" / "workers" / name)
    wl.dir.mkdir(parents=True)
    return wl


def _write_fake_codex(tmp: Path, body: str) -> Path:
    """Write an executable python fake-codex stub and return its path. The stub
    ignores all the exec args and just does what ``body`` says."""
    p = tmp / "fake_codex"
    p.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


# --- run_round: chosen exit code ------------------------------------------- #

def test_run_round_returns_codex_rc(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import sys\nsys.stdout.write('hello from codex\\n')\nsys.exit(3)\n")
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=30)
    assert rc == 3
    assert "hello from codex" in log.read_text()
    assert loop._Child.proc is None            # cleared in finally


def test_run_round_success_rc0(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import sys\nsys.exit(0)\n")
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=0)   # 0 => no timeout (wait forever)
    assert rc == 0


def test_run_round_normal_exit_sweeps_unreaped_owned_process_group(tmp: Path):
    wl = _mk_worker(tmp)
    marker = tmp / "normal-exit-owned-group"
    fake = _write_fake_codex(
        tmp,
        "import os, pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n",
    )
    log = wl.dir / "round-normal-group.log"
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(
            wl,
            {"MODEL": "m", "REASONING_EFFORT": "high"},
            "prompt",
            log,
            hard_timeout=30,
        )
    assert rc == 0
    leader, leader_group, grandchild, grandchild_group = map(
        int, marker.read_text(encoding="utf-8").split()
    )
    # The actual Codex and its descendants share the retained host's group;
    # the Codex leader is deliberately not allowed to detach from that host.
    assert leader_group != leader
    assert grandchild_group == leader_group
    assert leader_group != os.getpgrp()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (
        _running(leader) or _running(grandchild)
    ):
        time.sleep(0.02)
    assert not _running(leader)
    assert not _running(grandchild)


def _wait_processes_gone(*pids: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(_running(pid) for pid in pids):
        time.sleep(0.02)
    assert all(not _running(pid) for pid in pids)


def test_exec_owner_sigkill_revokes_liveness_and_kills_paid_group(tmp: Path):
    wl = _mk_worker(tmp)
    marker = tmp / "exec-owner-death"
    fake = _write_fake_codex(
        tmp,
        "import os, pathlib, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "time.sleep(120)\n",
    )
    code = (
        "from pathlib import Path\n"
        "from danus.execution import layout, loop\n"
        "loop.require_gateway_runtime=lambda: None\n"
        "wl=layout.WorkerLayout(Path(__import__('sys').argv[1]))\n"
        "raise SystemExit(loop.run_round(wl, {'MODEL':'m','REASONING_EFFORT':'high'},"
        " 'prompt', wl.dir/'owner-death.log', 120))\n"
    )
    env = os.environ.copy()
    env["DANUS_CODEX_BIN"] = str(fake)
    owner = subprocess.Popen([sys.executable, "-c", code, str(wl.dir)], env=env)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.02)
        assert marker.exists()
        leader, group, grandchild, grandchild_group = map(
            int, marker.read_text(encoding="utf-8").split()
        )
        assert group != leader
        assert grandchild_group == group
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=5)
        second_marker = tmp / "overlapping-paid-launch"
        second = tmp / "fake_codex_second"
        second.write_text(
            "#!/usr/bin/env python3\n"
            f"from pathlib import Path; Path({str(second_marker)!r}).write_text('started')\n",
            encoding="utf-8",
        )
        second.chmod(0o755)
        # The old host retains the same flock OFD during its TERM/KILL cleanup,
        # so an immediate replacement worker cannot begin a second paid job.
        with _env(DANUS_CODEX_BIN=str(second)):
            assert loop.run_round(
                wl,
                {"MODEL": "m", "REASONING_EFFORT": "high"},
                "prompt",
                wl.dir / "overlap-refused.log",
                hard_timeout=10,
            ) == 126
        assert not second_marker.exists()
        refused_status = json.loads(wl.status.read_text(encoding="utf-8"))
        assert refused_status["attempt_failure_code"] == (
            "prior_paid_cleanup_in_progress"
        )
        assert "cleanup is still in progress" in refused_status["attempt_failure"]
        _wait_processes_gone(leader, grandchild, group)
        # Once the old group is terminal the same fence is released.
        with _env(DANUS_CODEX_BIN=str(second)):
            assert loop.run_round(
                wl,
                {"MODEL": "m", "REASONING_EFFORT": "high"},
                "prompt",
                wl.dir / "overlap-after-cleanup.log",
                hard_timeout=10,
            ) == 0
        assert second_marker.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()


def test_host_sigkill_is_swept_by_live_worker_under_unreaped_fence(tmp: Path):
    wl = _mk_worker(tmp)
    marker = tmp / "host-crash"
    release = tmp / "release-host-sweep"
    fake = _write_fake_codex(
        tmp,
        "import os, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "time.sleep(120)\n",
    )
    code = (
        "from pathlib import Path\n"
        "import time\n"
        "from danus.execution import layout, loop\n"
        "loop.require_gateway_runtime=lambda: None\n"
        "wl=layout.WorkerLayout(Path(__import__('sys').argv[1]))\n"
        "marker=Path(__import__('sys').argv[2])\n"
        "release=Path(__import__('sys').argv[3])\n"
        "real_poll=loop._owned_child_exited_no_reap\n"
        "def gated_poll(proc):\n"
        "    if marker.exists() and not release.exists():\n"
        "        while not release.exists(): time.sleep(0.01)\n"
        "    return real_poll(proc)\n"
        "loop._owned_child_exited_no_reap=gated_poll\n"
        "loop.run_round(wl, {'MODEL':'m','REASONING_EFFORT':'high'},"
        " 'prompt', wl.dir/'host-crash.log', 120)\n"
    )
    env = os.environ.copy()
    env["DANUS_CODEX_BIN"] = str(fake)
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(wl.dir),
            str(marker),
            str(release),
        ],
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.02)
        assert marker.exists()
        leader, host_group, grandchild, grandchild_group = map(
            int, marker.read_text(encoding="utf-8").split()
        )
        assert host_group == grandchild_group
        os.kill(host_group, signal.SIGKILL)
        # The worker-side copy of the paid authority stays locked while host
        # death has been observed but group sweep is deliberately barriered.
        with pytest.raises(HotJoinError, match="cleanup is still in progress"):
            loop._acquire_paid_authority(wl)
        release.touch()
        owner.wait(timeout=8)
        _wait_processes_gone(leader, grandchild, host_group)
        paid_fd = loop._acquire_paid_authority(wl)
        os.close(paid_fd)
    finally:
        release.touch(exist_ok=True)
        if owner.poll() is None:
            owner.kill()
            owner.wait()


def test_run_round_post_spawn_exception_revokes_host_and_paid_group(
    tmp: Path, monkeypatch: pytest.MonkeyPatch
):
    wl = _mk_worker(tmp)
    marker = tmp / "post-spawn-exception"
    fake = _write_fake_codex(
        tmp,
        "import os, pathlib, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "time.sleep(120)\n",
    )

    def injected_failure(_worker: L.WorkerLayout) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.02)
        assert marker.exists()
        raise RuntimeError("injected post-spawn failure")

    monkeypatch.setattr(loop, "_force_stop_requested", injected_failure)
    with _env(DANUS_CODEX_BIN=str(fake)), pytest.raises(
        RuntimeError, match="injected post-spawn failure"
    ):
        loop.run_round(
            wl,
            {"MODEL": "m", "REASONING_EFFORT": "high"},
            "prompt",
            wl.dir / "post-spawn.log",
            hard_timeout=30,
        )
    leader, group, grandchild, grandchild_group = map(
        int, marker.read_text(encoding="utf-8").split()
    )
    assert group == grandchild_group
    _wait_processes_gone(leader, grandchild, group)
    assert loop._Child.proc is None


def test_run_round_cooperative_stop_terminates_only_owned_child(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import time\ntime.sleep(120)\n")
    log = wl.dir / "round.log"
    wl.stop.write_text("force\n", encoding="utf-8")
    started = time.monotonic()
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(
            wl,
            {"MODEL": "m", "REASONING_EFFORT": "high"},
            "prompt",
            log,
            hard_timeout=30,
        )
    assert rc == loop.WORKER_STOP_REQUESTED_RC
    assert time.monotonic() - started < 5
    assert "cooperative owner stop requested" in log.read_text(encoding="utf-8")
    assert loop._Child.proc is None


def test_run_round_force_stop_removes_owned_child_process_group(tmp: Path):
    wl = _mk_worker(tmp)
    marker = tmp / "owned-group"
    fake = _write_fake_codex(
        tmp,
        "import os, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "time.sleep(120)\n",
    )
    log = wl.dir / "round-group.log"

    def request_stop() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.02)
        assert marker.exists()
        wl.stop.write_text("force\n", encoding="utf-8")

    stopper = threading.Thread(target=request_stop)
    stopper.start()
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(
            wl,
            {"MODEL": "m", "REASONING_EFFORT": "high"},
            "prompt",
            log,
            hard_timeout=30,
        )
    stopper.join(timeout=5)
    assert not stopper.is_alive()
    assert rc == loop.WORKER_STOP_REQUESTED_RC
    leader, leader_group, grandchild, grandchild_group = map(
        int, marker.read_text(encoding="utf-8").split()
    )
    assert leader_group != leader
    assert grandchild_group == leader_group
    assert leader_group != os.getpgrp()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (
        _running(leader) or _running(grandchild)
    ):
        time.sleep(0.02)
    assert not _running(leader)
    assert not _running(grandchild)


def test_worker_mcp_config_arg_is_one_complete_toml_object(tmp: Path):
    wl = _mk_worker(tmp, "author7")
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:18091/verify"):
        config_arg = loop._worker_mcp_config_arg(wl)

    # Codex CLI --config accepts a TOML assignment. Parsing it with the stdlib
    # catches quoting/inline-table mistakes without launching Codex.
    assert tomllib.loads(config_arg) == {
        "mcp_servers": {
            "danus": {
                "command": sys.executable,
                "args": ["-I", "-B", "-m", "danus.gateway"],
                "env": {
                    "DANUS_PROJECT_DIR": str(wl.project_dir),
                    "DANUS_AUTHOR": "author7",
                    "DANUS_ROLE": "worker",
                    "DANUS_HOTJOIN_ENABLED": "1",
                    "DANUS_HOTJOIN_TARGET": "author7",
                    "DANUS_VERIFY_URL": "http://127.0.0.1:18091/verify",
                },
                "tool_timeout_sec": 3600,
                "default_tools_approval_mode": "approve",
                "required": True,
            }
        }
    }


def test_run_round_injects_complete_mcp_without_project_config(tmp: Path):
    wl = _mk_worker(tmp, "worker9")
    assert not wl.codex_config.exists()  # regression: production cannot rely on it
    log = wl.dir / "round.log"
    captured = {}

    original_spawn = loop.spawn_owned_child

    def fake_spawn(command, **kwargs):
        captured["command"] = command
        # Preserve the retained-child/PID-fence contract while replacing only
        # the external Codex command under test.
        return original_spawn([sys.executable, "-c", "pass"], **kwargs)

    loop.spawn_owned_child = fake_spawn
    try:
        with _env(
            DANUS_CODEX_BIN=str(tmp / "fake-codex"),
            DANUS_VERIFY_URL="http://127.0.0.1:18091/verify",
        ):
            rc = loop.run_round(
                wl,
                {"MODEL": "gpt-test", "REASONING_EFFORT": "xhigh"},
                "prompt",
                log,
                hard_timeout=30,
            )
    finally:
        loop.spawn_owned_child = original_spawn

    assert rc == 0
    command = captured["command"]
    mcp_overrides = [
        (token, command[index + 1])
        for index, token in enumerate(command[:-1])
        if token in ("--config", "-c")
        and command[index + 1].startswith("mcp_servers=")
    ]
    assert len(mcp_overrides) == 1
    assert mcp_overrides[0][0] == "--config"
    assert "--json" in command
    assert not any(part.startswith("mcp_servers.danus.") for part in command)
    parsed = tomllib.loads(mcp_overrides[0][1])["mcp_servers"]["danus"]
    assert parsed == {
        "command": sys.executable,
        "args": ["-I", "-B", "-m", "danus.gateway"],
        "env": {
            "DANUS_PROJECT_DIR": str(wl.project_dir),
            "DANUS_AUTHOR": "worker9",
            "DANUS_ROLE": "worker",
            "DANUS_HOTJOIN_ENABLED": "1",
            "DANUS_HOTJOIN_TARGET": "worker9",
            "DANUS_VERIFY_URL": "http://127.0.0.1:18091/verify",
        },
        "tool_timeout_sec": 3600,
        "default_tools_approval_mode": "approve",
        "required": True,
    }
    assert not wl.codex_config.exists()


# --- run_round: hard timeout → terminate → 124 ----------------------------- #

def test_run_round_hard_timeout_terminates(tmp: Path):
    wl = _mk_worker(tmp)
    # sleeps far past the tiny hard_timeout; a plain terminate() ends it.
    fake = _write_fake_codex(tmp, "import time\ntime.sleep(60)\n")
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=1)
    assert rc == 124
    assert "hard-timeout after 1s" in log.read_text()
    assert loop._Child.proc is None


# --- run_round: missing binary → 127 --------------------------------------- #

def test_run_round_missing_binary_returns_127(tmp: Path):
    wl = _mk_worker(tmp)
    missing = tmp / "does_not_exist_codex"
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(missing)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=30)
    assert rc == 127
    assert "owned paid child failed to exec" in log.read_text()


def test_run_round_gateway_preflight_failure_starts_no_codex(tmp: Path, monkeypatch):
    wl = _mk_worker(tmp)
    log = wl.dir / "preflight.log"
    codex_calls = []

    def fail_preflight():
        raise loop.GatewayRuntimeUnavailable("broken mcp import")

    monkeypatch.setattr(loop, "require_gateway_runtime", fail_preflight)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: codex_calls.append(a))
    rc = loop.run_round(
        wl,
        {"MODEL": "m", "REASONING_EFFORT": "high"},
        "prompt",
        log,
        hard_timeout=1,
    )

    assert rc == 126
    assert codex_calls == []
    assert "gateway runtime unavailable" in log.read_text()


# --- run_round: unresponsive child → terminate times out → kill → 124 ------ #

def test_run_round_timeout_then_kill(tmp: Path):
    """A child that ignores TERM is group-KILLed while still unreaped."""
    wl = _mk_worker(tmp)
    log = wl.dir / "round.log"
    fake = _write_fake_codex(
        tmp,
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n",
    )
    original_terminate = loop._terminate_owned_child
    loop._terminate_owned_child = lambda proc: original_terminate(proc, grace=0.1)
    try:
        with _env(DANUS_CODEX_BIN=str(fake)):
            rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                                "prompt", log, hard_timeout=1)
    finally:
        loop._terminate_owned_child = original_terminate
    assert rc == 124
    assert loop._Child.proc is None


# --- main loop: stop flag → graceful stop ---------------------------------- #

def test_main_stops_on_stop_flag(tmp: Path):
    wl = _mk_worker(tmp)
    wl.stop.touch()          # stop before the first round
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0"):
        _patch_run_round(lambda *a, **k: 0)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert not wl.stop.exists()                       # consumed
    assert json.loads(wl.status.read_text())["state"] == "stopped"


def test_main_consumes_stop_reported_during_active_round(tmp: Path):
    wl = _mk_worker(tmp)
    calls = []

    def stopped_round(worker, *_args, **_kwargs):
        calls.append(1)
        worker.stop.write_text("force\n", encoding="utf-8")
        return loop.WORKER_STOP_REQUESTED_RC

    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0"):
        _patch_run_round(stopped_round)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert calls == [1]
    assert not wl.stop.exists()
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"
    assert status["last_rc"] == loop.WORKER_STOP_REQUESTED_RC


# --- main loop: deadline → stop -------------------------------------------- #

def test_main_stops_on_deadline(tmp: Path):
    wl = _mk_worker(tmp)
    (wl.project_dir / L.DEADLINE_FILE).write_text("1")   # epoch 1 = long past
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0"):
        _patch_run_round(lambda *a, **k: 0)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert json.loads(wl.status.read_text())["state"] == "deadline"


# --- main loop: max-rounds cap --------------------------------------------- #

def test_main_max_rounds_cap(tmp: Path):
    wl = _mk_worker(tmp)
    calls = []
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0", DANUS_MAX_ROUNDS="2",
                                  DANUS_MAX_CONSEC_FAILURES="0"):
        _patch_run_round(lambda *a, **k: (calls.append(1) or 0))
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert len(calls) == 2                              # exactly max_rounds rounds ran
    st = json.loads(wl.status.read_text())
    assert st["state"] == "max_rounds"
    assert st["round"] == 2 and st["last_rc"] == 0


def test_main_restart_preserves_old_logs_and_advances_round_sequence(tmp: Path):
    wl = _mk_worker(tmp)
    wl.logs.mkdir()
    old_bytes = b"immutable prior round\n"
    (wl.logs / "round_3.log").write_bytes(old_bytes)
    wl.status.write_text(json.dumps({"state": "stopped", "round": 3}))
    observed = []

    def one_round(_wl, _role, _prompt, log_path, _hard_timeout):
        observed.append(log_path.name)
        log_path.write_text(f"new {log_path.name}\n", encoding="utf-8")
        return 0

    with _restore_sigterm(), _env(
        DANUS_ROUND_BEAT="0",
        DANUS_MAX_ROUNDS="1",
        DANUS_MAX_CONSEC_FAILURES="0",
    ):
        _patch_run_round(one_round)
        try:
            assert loop.main(str(wl.dir)) == 0
            assert loop.main(str(wl.dir)) == 0
        finally:
            _unpatch_run_round()

    assert observed == ["round_4.log", "round_5.log"]
    assert (wl.logs / "round_3.log").read_bytes() == old_bytes
    assert (wl.logs / "round_4.log").read_text() == "new round_4.log\n"
    assert (wl.logs / "round_5.log").read_text() == "new round_5.log\n"
    status = json.loads(wl.status.read_text())
    assert status["state"] == "max_rounds"
    assert status["round"] == 5


# --- main loop: consecutive-failure cap → error / rc 1 --------------------- #

def test_main_consecutive_failure_cap(tmp: Path):
    wl = _mk_worker(tmp)
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0", DANUS_MAX_CONSEC_FAILURES="2",
                                  DANUS_MAX_ROUNDS="0"):
        def _fail(w, role, prompt, log_path, ht):
            log_path.write_text('"fact_id": "0123456789abcdef"\n')
            return 5                                    # a failing rc (not 0/124)
        _patch_run_round(_fail)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 1
    st = json.loads(wl.status.read_text())
    assert st["state"] == "error" and "consecutive failed rounds" in st["error"]
    # last idle status carried the parsed fact id
    assert st.get("last_fact_id") == "0123456789abcdef" or st["last_rc"] == 5


def test_main_timeout_rc124_does_not_count_as_failure(tmp: Path):
    """rc 124 (hard-timeout) resets the consecutive-failure counter, so a run of
    124s never trips the failure cap — it must stop via max_rounds instead."""
    wl = _mk_worker(tmp)
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0", DANUS_MAX_CONSEC_FAILURES="2",
                                  DANUS_MAX_ROUNDS="3"):
        _patch_run_round(lambda *a, **k: 124)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert json.loads(wl.status.read_text())["state"] == "max_rounds"


# --- main loop: codex missing (127) short-circuits ------------------------- #

def test_main_codex_missing_127(tmp: Path):
    wl = _mk_worker(tmp)
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0"):
        _patch_run_round(lambda *a, **k: 127)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 127
    st = json.loads(wl.status.read_text())
    assert st["state"] == "error" and st["error"] == "codex binary not found"


# --- main: bad worker dir → rc 2 ------------------------------------------- #

def test_main_missing_worker_dir(tmp: Path):
    rc = loop.main(str(tmp / "nope"))
    assert rc == 2


def test_main_gateway_preflight_fails_before_any_state_or_launch(tmp: Path, monkeypatch):
    wl = _mk_worker(tmp)
    calls = {"config": 0, "status": 0, "popen": 0}

    def fail_preflight():
        raise loop.GatewayRuntimeUnavailable("missing danus.gateway.server")

    monkeypatch.setattr(loop, "require_gateway_runtime", fail_preflight)
    monkeypatch.setattr(
        scaffold,
        "write_codex_config",
        lambda *_args, **_kwargs: calls.__setitem__("config", calls["config"] + 1),
    )
    monkeypatch.setattr(
        loop,
        "write_status",
        lambda *_args, **_kwargs: calls.__setitem__("status", calls["status"] + 1),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: calls.__setitem__("popen", calls["popen"] + 1),
    )

    assert loop.main(str(wl.dir)) == 126
    assert calls == {"config": 0, "status": 0, "popen": 0}
    assert not wl.codex_config.exists()
    assert not wl.logs.exists()
    assert not wl.status.exists()


# --- SIGTERM handler: terminate child, write terminated, exit 0 ------------ #

def test_main_sigterm_handler(tmp: Path):
    wl = _mk_worker(tmp)
    fake_proc = loop.spawn_owned_child(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp,
        popen=subprocess.Popen,
    )

    # run_round: install a live child then deliver SIGTERM to ourselves so the
    # loop's own handler fires (covers _on_term end to end).
    def _round(w, role, prompt, log_path, ht):
        loop._Child.proc = fake_proc
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(2)                     # give the signal time to be delivered
        return 0

    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0"):
        _patch_run_round(_round)
        try:
            try:
                loop.main(str(wl.dir))
                assert False, "handler should sys.exit(0)"
            except SystemExit as e:
                assert e.code == 0
        finally:
            _unpatch_run_round()
            loop._Child.proc = None
    assert fake_proc.returncode is not None
    assert json.loads(wl.status.read_text())["state"] == "terminated"


# --- write_status: recovers from a corrupt existing status ----------------- #

def test_write_status_corrupt_existing_recovers(tmp: Path):
    wl = _mk_worker(tmp)
    wl.status.write_text("{not json")            # corrupt → JSONDecodeError branch
    loop.write_status(wl, state="running")
    st = json.loads(wl.status.read_text())
    assert st["state"] == "running" and st["worker"] == "high"


def test_write_status_legal_nonobject_json_recovers(tmp: Path):
    wl = _mk_worker(tmp)
    for value in ([], ["old"], "old", 7, True, None):
        wl.status.write_text(json.dumps(value), encoding="utf-8")
        loop.write_status(wl, state="running")
        status = json.loads(wl.status.read_text(encoding="utf-8"))
        assert isinstance(status, dict)
        assert status["state"] == "running"
        assert status["worker"] == "high"


# --- _parse_last_fact_id: unreadable path → None --------------------------- #

def test_parse_last_fact_id_missing_file(tmp: Path):
    assert loop._parse_last_fact_id(tmp / "no_such.log") is None   # OSError branch


# --- _cleanup_pid: only removes a .pid that points at us ------------------- #

def test_cleanup_pid_removes_own(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text(json.dumps({"schema_version": 1, "pid": os.getpid()}))
    loop._cleanup_pid(wl)
    assert not wl.pid.exists()


def test_cleanup_pid_keeps_foreign(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text(json.dumps({"schema_version": 1, "pid": 999_999_999}))
    loop._cleanup_pid(wl)
    assert wl.pid.exists()                     # left intact


def test_cleanup_pid_swallows_oserror(tmp: Path):
    """A .pid that cannot be read (here: it is a directory) → OSError swallowed."""
    wl = _mk_worker(tmp)
    wl.pid.mkdir()                             # read_text on a dir raises OSError
    loop._cleanup_pid(wl)                      # must not raise
    assert wl.pid.exists()


# --- main loop: positive beat sleeps between rounds ------------------------ #

def test_main_beat_sleep_between_rounds(tmp: Path):
    """A positive DANUS_ROUND_BEAT makes the loop sleep between rounds; we stub
    time.sleep so no real wall-clock time passes and record it fired."""
    wl = _mk_worker(tmp)
    slept = []
    orig_sleep = time.sleep

    def _one_then_stop(*a, **k):
        wl.stop.touch()          # stop after the first round completes
        return 0

    time.sleep = lambda s: slept.append(s)
    try:
        with _restore_sigterm(), _env(DANUS_ROUND_BEAT="7", DANUS_MAX_ROUNDS="0",
                                      DANUS_MAX_CONSEC_FAILURES="0"):
            _patch_run_round(_one_then_stop)
            try:
                rc = loop.main(str(wl.dir))
            finally:
                _unpatch_run_round()
    finally:
        time.sleep = orig_sleep
    assert rc == 0
    assert 7 in slept                          # the beat sleep fired once


# --- kickoff prompt -------------------------------------------------------- #

def test_kickoff_mentions_worker_and_project():
    p = loop.kickoff("ProjX", "wkrY")
    assert "wkrY" in p and "ProjX" in p and "TASK.md" in p


# --- __main__ entry -------------------------------------------------------- #

def test_dunder_main_dispatches(tmp: Path):
    """runpy the package as __main__ with the loop entry patched: the guard runs
    and dispatches to main() without spawning anything."""
    seen = {}

    def _fake_main(arg):
        seen["arg"] = arg
        return 0

    orig = loop.main
    loop.main = _fake_main
    argv = sys.argv
    sys.argv = ["prog", "/some/worker/dir"]
    try:
        try:
            runpy.run_module("danus.execution", run_name="__main__")
            assert False, "should sys.exit"
        except SystemExit as e:
            assert e.code == 0
    finally:
        loop.main = orig
        sys.argv = argv
    assert seen["arg"] == "/some/worker/dir"


def test_dunder_main_usage_guard():
    """Wrong argc → usage message + exit 2 (no dispatch)."""
    argv = sys.argv
    sys.argv = ["prog"]                        # missing worker_dir
    try:
        try:
            runpy.run_module("danus.execution", run_name="__main__")
            assert False, "should sys.exit(2)"
        except SystemExit as e:
            assert e.code == 2
    finally:
        sys.argv = argv


# --- layout defaults (no env overrides) ------------------------------------ #

def test_layout_defaults_and_empties(tmp: Path):
    with _env(DANUS_WORKER_CONTRACT=None, DANUS_WORKER_SKILLS=None,
              DANUS_AGENTS_ROOT=None):
        # repo_root / worker_md / worker_skills_dir defaults
        rr = L.repo_root()
        assert L.worker_md() == rr / "agents" / "contracts" / "worker.md"
        assert L.worker_skills_dir() == rr / "agents" / "skills" / "worker"
        # agents_root default = <cwd>/runtime/projects
        assert L.agents_root() == (Path.cwd() / "runtime" / "projects").resolve()
    # list_workers / list_projects on a nonexistent root → []
    with _env(DANUS_AGENTS_ROOT=str(tmp / "no_such_root")):
        assert L.list_workers("ghost") == []
        assert L.list_projects() == []


# --- scaffold.symlink branches --------------------------------------------- #

def test_symlink_skips_existing(tmp: Path):
    target = tmp / "target"
    target.write_text("x")
    link = tmp / "link"
    link.write_text("already here")            # link path exists → early return
    scaffold.symlink(target, link)
    assert link.read_text() == "already here"  # untouched


def test_symlink_swallows_oserror(tmp: Path):
    target = tmp / "target"
    target.write_text("x")
    # a link path whose parent does not exist → os.symlink raises OSError, swallowed
    link = tmp / "no_parent_dir" / "link"
    scaffold.symlink(target, link)             # must not raise
    assert not link.exists()


# --- runner ---------------------------------------------------------------- #

# run_round monkeypatch helpers (so the standalone runner works without pytest's
# monkeypatch fixture): swap loop.run_round for the duration of a test.
_ORIG_RUN_ROUND = loop.run_round


def _patch_run_round(fn):
    loop.run_round = fn


def _unpatch_run_round():
    loop.run_round = _ORIG_RUN_ROUND


_NO_TMP = {test_kickoff_mentions_worker_and_project, test_dunder_main_usage_guard}


def main() -> None:
    tests = [
        test_run_round_returns_codex_rc,
        test_run_round_success_rc0,
        test_worker_mcp_config_arg_is_one_complete_toml_object,
        test_run_round_injects_complete_mcp_without_project_config,
        test_run_round_hard_timeout_terminates,
        test_run_round_missing_binary_returns_127,
        test_run_round_timeout_then_kill,
        test_main_stops_on_stop_flag,
        test_main_stops_on_deadline,
        test_main_max_rounds_cap,
        test_main_restart_preserves_old_logs_and_advances_round_sequence,
        test_main_consecutive_failure_cap,
        test_main_timeout_rc124_does_not_count_as_failure,
        test_main_codex_missing_127,
        test_main_missing_worker_dir,
        test_main_sigterm_handler,
        test_write_status_corrupt_existing_recovers,
        test_write_status_legal_nonobject_json_recovers,
        test_parse_last_fact_id_missing_file,
        test_cleanup_pid_removes_own,
        test_cleanup_pid_keeps_foreign,
        test_cleanup_pid_swallows_oserror,
        test_main_beat_sleep_between_rounds,
        test_kickoff_mentions_worker_and_project,
        test_dunder_main_dispatches,
        test_dunder_main_usage_guard,
        test_layout_defaults_and_empties,
        test_symlink_skips_existing,
        test_symlink_swallows_oserror,
    ]
    for t in tests:
        if t in _NO_TMP:
            t()
        else:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL LOOP TESTS PASSED")


if __name__ == "__main__":
    main()
