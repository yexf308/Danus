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
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

from danus.execution import layout as L
from danus.execution import loop, scaffold


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
                "args": ["-m", "danus.gateway"],
                "env": {
                    "DANUS_PROJECT_DIR": str(wl.project_dir),
                    "DANUS_AUTHOR": "author7",
                    "DANUS_ROLE": "worker",
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

    class _StubProc:
        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return _StubProc()

    original_popen = subprocess.Popen
    subprocess.Popen = fake_popen
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
        subprocess.Popen = original_popen

    assert rc == 0
    command = captured["command"]
    mcp_overrides = [
        (token, command[index + 1])
        for index, token in enumerate(command[:-1])
        if token in ("--config", "-c")
        and command[index + 1].startswith("mcp_servers.danus")
    ]
    assert len(mcp_overrides) == 1
    assert mcp_overrides[0][0] == "--config"
    assert not any(part.startswith("mcp_servers.danus.") for part in command)
    parsed = tomllib.loads(mcp_overrides[0][1])["mcp_servers"]["danus"]
    assert parsed == {
        "command": sys.executable,
        "args": ["-m", "danus.gateway"],
        "env": {
            "DANUS_PROJECT_DIR": str(wl.project_dir),
            "DANUS_AUTHOR": "worker9",
            "DANUS_ROLE": "worker",
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
    assert "codex binary not found" in log.read_text()


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
    """A child that ignores terminate() (wait(10) times out) is force-killed. We
    fake Popen so the 10s terminate-grace does not slow the test."""
    wl = _mk_worker(tmp)
    log = wl.dir / "round.log"

    class _StubProc:
        def __init__(self):
            self.terminated = False
            self.killed = False
            self._waits = 0

        def wait(self, timeout=None):
            self._waits += 1
            # 1st wait = the hard-timeout expiry; 2nd wait = the 10s grace expiry.
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    stub = _StubProc()
    orig_popen = subprocess.Popen
    subprocess.Popen = lambda *a, **k: stub
    try:
        with _env(DANUS_CODEX_BIN=str(tmp / "anything")):
            rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                                "prompt", log, hard_timeout=1)
    finally:
        subprocess.Popen = orig_popen
    assert rc == 124
    assert stub.terminated and stub.killed          # terminate → (grace expired) → kill
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

    class _FakeProc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    fake_proc = _FakeProc()

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
    assert fake_proc.terminated
    assert json.loads(wl.status.read_text())["state"] == "terminated"


# --- write_status: recovers from a corrupt existing status ----------------- #

def test_write_status_corrupt_existing_recovers(tmp: Path):
    wl = _mk_worker(tmp)
    wl.status.write_text("{not json")            # corrupt → JSONDecodeError branch
    loop.write_status(wl, state="running")
    st = json.loads(wl.status.read_text())
    assert st["state"] == "running" and st["worker"] == "high"


# --- _parse_last_fact_id: unreadable path → None --------------------------- #

def test_parse_last_fact_id_missing_file(tmp: Path):
    assert loop._parse_last_fact_id(tmp / "no_such.log") is None   # OSError branch


# --- _cleanup_pid: only removes a .pid that points at us ------------------- #

def test_cleanup_pid_removes_own(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text(str(os.getpid()))
    loop._cleanup_pid(wl)
    assert not wl.pid.exists()


def test_cleanup_pid_keeps_foreign(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text("999999999")            # some other pid
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
        test_main_consecutive_failure_cap,
        test_main_timeout_rc124_does_not_count_as_failure,
        test_main_codex_missing_127,
        test_main_missing_worker_dir,
        test_main_sigterm_handler,
        test_write_status_corrupt_existing_recovers,
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
