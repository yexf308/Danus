"""Offline, fully-mocked coverage of every ``danus`` CLI verb + helper.

Unlike ``test_orchestration.py`` (which spawns the real loop subprocess with a
stub codex), this module never launches *any* process: ``spawn_loop`` is
monkeypatched to a recording fake, so ``do_start`` / ``main start`` exercise the
flock + pid bookkeeping without a fork. Everything runs under a tempdir agents
root. Targets the read helpers, the error/edge paths of each verb, the two text
formatters, the ``_task_from_args`` source selection, ``build_parser``, and the
full ``main`` dispatch table — plus ``python -m danus.orchestration`` via runpy.

Runs standalone (``python -m danus.orchestration.tests.test_cli_verbs``) and
under pytest.
"""

from __future__ import annotations

import gc
import hashlib
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

from danus.execution import layout as L
from danus.hotjoin import HotJoinStore
from danus.orchestration import cli


# --------------------------------------------------------------------------- #
# env / project helpers (mirrors test_orchestration.py so styles match)        #
# --------------------------------------------------------------------------- #

@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _project_env(tmp: Path, **extra):
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    env = {"DANUS_AGENTS_ROOT": str(tmp / "agents"),
           "DANUS_WORKER_CONTRACT": str(contract),
           "DANUS_WORKER_SKILLS": str(skills)}
    env.update(extra)
    with _env(**env):
        yield


class _FakeSpawn:
    """Records (wdir) calls; returns our *own* pid so ``_alive`` reports True and
    ``_start_one`` treats the worker as live — without launching anything."""

    def __init__(self):
        self.calls = []

    def __call__(self, wdir):
        self.calls.append(Path(wdir))
        return _FakeProcess(os.getpid())


class _FakeProcess:
    def __init__(self, pid: int, *, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self):
        self.returncode = -9
        return self.returncode


@contextmanager
def _patch_spawn():
    fake = _FakeSpawn()
    orig = cli.spawn_loop
    orig_identity = cli._process_identity
    cli.spawn_loop = fake
    cli._process_identity = lambda pid: {
        "pid": pid,
        "pgid": pid,
        "start_token": "fake-spawn-birth",
        "state": "R",
    }
    try:
        yield fake
    finally:
        cli.spawn_loop = orig
        cli._process_identity = orig_identity


def _wl(project: str, worker: str) -> L.WorkerLayout:
    return L.WorkerLayout(L.worker_dir(project, worker))


def _pid_record(wl: L.WorkerLayout, pid: int, *, token: str = "test-birth") -> dict:
    return {
        "schema_version": 1,
        "pid": pid,
        "pgid": pid,
        "start_token": token,
        "worker_dir": os.path.abspath(str(wl.dir)),
    }


def _write_pid_record(
    wl: L.WorkerLayout, pid: int, *, token: str = "test-birth"
) -> dict:
    record = _pid_record(wl, pid, token=token)
    cli._write_pid_record(wl, record)
    return record


@contextmanager
def _fake_process_identity(pid: int, *, token: str = "test-birth", state: str = "R"):
    original = cli._process_identity

    def fake(observed_pid: int):
        if observed_pid == pid:
            return {
                "pid": pid,
                "pgid": pid,
                "start_token": token,
                "state": state,
            }
        return original(observed_pid)

    cli._process_identity = fake
    try:
        yield
    finally:
        cli._process_identity = original


def test_darwin_libproc_birth_token_uses_microseconds_and_fails_closed():
    pgid = os.getpgid(os.getpid())

    class FakeProcPidInfo:
        argtypes = None
        restype = None

        def __init__(self, *, result: str = "ok"):
            self.result = result
            self.calls = []

        def __call__(self, pid, flavor, arg, buffer, size):
            self.calls.append((pid, flavor, arg, size))
            if self.result != "ok":
                return 0
            info = cli.ctypes.cast(
                buffer, cli.ctypes.POINTER(cli._DarwinProcBSDInfo)
            ).contents
            info.pbi_pid = pid
            info.pbi_pgid = pgid
            info.pbi_start_tvsec = 1_786_252_097
            info.pbi_start_tvusec = 12_345
            return size

    class FakeLibProc:
        def __init__(self, call):
            self.proc_pidinfo = call

    call = FakeProcPidInfo()
    identity = cli._darwin_process_birth(
        os.getpid(), libproc=FakeLibProc(call)
    )
    assert identity == {
        "pgid": pgid,
        "start_token": "darwin-libproc:1786252097:012345",
    }
    assert call.calls == [
        (
            os.getpid(),
            cli._DARWIN_PROC_PIDTBSDINFO,
            0,
            cli.ctypes.sizeof(cli._DarwinProcBSDInfo),
        )
    ]

    failed = FakeProcPidInfo(result="short")
    try:
        cli._darwin_process_birth(os.getpid(), libproc=FakeLibProc(failed))
        raise AssertionError("short libproc record must fail closed")
    except cli.ProcessIdentityError as exc:
        assert "incompatible record" in str(exc)


def _expect_exit(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except SystemExit as e:
        return e
    raise AssertionError(f"expected SystemExit from {getattr(fn, '__name__', fn)}")


# --------------------------------------------------------------------------- #
# read helpers: _read_pid / _alive / _read_status                              #
# --------------------------------------------------------------------------- #

def test_read_pid_missing_and_garbage(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        assert cli._read_pid(wl) is None
        wl.pid.write_text("not-an-int\n")
        try:
            cli._read_pid(wl)
            raise AssertionError("legacy PID text must fail closed")
        except cli.ProcessIdentityError:
            pass
        wl.pid.write_text("4321\n")
        try:
            cli._read_pid(wl)
            raise AssertionError("legacy integer PID must fail closed")
        except cli.ProcessIdentityError:
            pass
        _write_pid_record(wl, 4321)
        assert cli._read_pid(wl) == 4321


def test_alive_variants(tmp: Path):
    # falsy pid / None -> dead
    assert cli._alive(None) is False
    assert cli._alive(0) is False
    # our own process is alive (and not a zombie)
    assert cli._alive(os.getpid()) is True
    # a pid that (almost certainly) does not exist -> ProcessLookupError -> dead
    assert cli._alive(2_000_000_000) is False


def test_stop_one_force_sigkill_fallback(tmp: Path):
    """Force publishes a cooperative request and never signals an external PID."""
    ready = tmp / "handler_ready"
    prog = (
        "import signal, time, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(120)\n"
    )
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        proc = subprocess.Popen([sys.executable, "-c", prog], start_new_session=True)
        cli._write_pid_record(wl, cli._capture_pid_record(wl, proc.pid))
        try:
            # wait until the child has installed its SIGTERM-ignoring handler
            end = time.time() + 10
            while time.time() < end and not ready.exists():
                time.sleep(0.02)
            assert ready.exists(), "child never signalled readiness"
            assert cli._alive(proc.pid) is True
            res = cli._stop_one(wl, force=True)
            assert res == "stopping (cooperative force)"
            assert wl.stop.read_text(encoding="utf-8") == "force\n"
            assert cli._alive(proc.pid) is True
            assert wl.pid.exists()
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


def test_stop_one_force_sigkill_killpg_raises(tmp: Path):
    """Force uses no numeric signal even if a signal function would explode."""
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        _write_pid_record(wl, 2_000_000_000)

        real_record_live = cli._pid_record_is_live
        real_killpg = cli.os.killpg
        signals = []

        cli._pid_record_is_live = lambda record: True
        def boom_killpg(pgid, sig):
            signals.append((pgid, sig))
            raise AssertionError("cooperative stop must not signal")
        cli.os.killpg = boom_killpg
        try:
            assert cli._stop_one(wl, force=True) == "stopping (cooperative force)"
            assert signals == []
            assert wl.pid.exists()
            assert wl.stop.read_text(encoding="utf-8") == "force\n"
        finally:
            cli._pid_record_is_live = real_record_live
            cli.os.killpg = real_killpg


def test_alive_proc_read_failure_defaults_alive(tmp: Path):
    """Race branch (cli.py 70-71): ``os.kill(pid,0)`` succeeds (pid exists) but the
    ``/proc/<pid>/stat`` read fails (the process vanished between the two calls, or
    /proc is unavailable). ``_alive`` conservatively returns True. We simulate the
    race by patching ``cli.Path`` so the /proc read raises OSError, using our own
    (definitely-live, non-zombie) pid so os.kill succeeds."""
    real_Path = cli.Path

    class _BoomPath:
        def __init__(self, *a, **k):
            pass
        def read_text(self, *a, **k):
            raise OSError("simulated /proc read failure")

    cli.Path = _BoomPath
    try:
        assert cli._alive(os.getpid()) is True        # kill ok, /proc read boom -> True
    finally:
        cli.Path = real_Path


def test_stop_one_pid_reuse_mismatch_never_signals(tmp: Path):
    """A live process at a stale PID is not the recorded worker birth."""
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        pid = 23456
        _write_pid_record(wl, pid, token="old-birth")
        signals = []
        original_killpg = cli.os.killpg
        cli.os.killpg = lambda pgid, sig: signals.append((pgid, sig))
        try:
            with _fake_process_identity(pid, token="new-birth"):
                try:
                    cli._stop_one(wl, force=True)
                    raise AssertionError("PID reuse must fail closed")
                except cli.ProcessIdentityError:
                    pass
        finally:
            cli.os.killpg = original_killpg
        assert signals == []
        assert wl.pid.exists()


def test_read_status_missing_and_bad_json(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        # do_new wrote a valid status; corrupt it -> {}
        wl.status.write_text("{ not json", encoding="utf-8")
        assert cli._read_status(wl) == {}
        for value in ([], "valid but not an object", 4, True, None):
            wl.status.write_text(json.dumps(value), encoding="utf-8")
            assert cli._read_status(wl) == {}
        # remove it -> {}
        wl.status.unlink()
        assert cli._read_status(wl) == {}


def test_worker_status_exposes_canonical_unfinished_paid_intent_and_recovery_argv(
    tmp: Path,
):
    with _project_env(tmp):
        for index, state in enumerate(
            ("prepared", "dispatching", "started", "delivery_unknown"), start=1
        ):
            project = f"P{index}"
            cli.do_new(project, roles="high:1")
            wl = _wl(project, "high")
            store = HotJoinStore(wl.project_dir)
            store.set_thread_id("high", f"thread-{index}")
            intent = store.round_intent(
                "high",
                f"thread-{index}",
                prompt_sha256=hashlib.sha256(f"prompt-{index}".encode()).hexdigest(),
                requested_model="offline-model",
                requested_effort="low",
            )
            if state != "prepared":
                store.record_round_intent(
                    intent["client_id"], "dispatching", expected_states={"prepared"}
                )
            if state == "started":
                store.record_round_intent(
                    intent["client_id"],
                    "started",
                    turn_id=f"turn-{index}",
                    expected_states={"dispatching"},
                )
            if state == "delivery_unknown":
                store.record_round_intent(
                    intent["client_id"],
                    "delivery_unknown",
                    expected_states={"dispatching"},
                )

            status = cli.worker_status(wl)
            canonical = status["unfinished_paid_intent"]
            assert canonical == {
                "client_id": intent["client_id"],
                "thread_id": f"thread-{index}",
                "state": state,
                "turn_id": f"turn-{index}" if state == "started" else None,
                "requested_model": "offline-model",
                "requested_effort": "low",
                "prompt_sha256": hashlib.sha256(
                    f"prompt-{index}".encode()
                ).hexdigest(),
            }
            assert status["intent_ledger_error"] is None
            recovery = status["recovery_required"]
            if state == "prepared":
                assert recovery["action"] == "resume_or_cancel_prepared_intent"
                assert recovery["paid_dispatch_state"] == "not_dispatched"
                assert recovery["paid_outcome_unknown"] is False
                assert recovery["argv"] == [
                    "bin/danus", "start", f"{project}/high"
                ]
                assert recovery["cancel_argv"] == [
                    "bin/danus",
                    "cancel-prepared-intent",
                    f"{project}/high",
                    "--thread-id",
                    f"thread-{index}",
                    "--client-id",
                    intent["client_id"],
                    "--reason",
                    "<OWNER_REASON>",
                ]
                cancel_parsed = cli.build_parser().parse_args(
                    recovery["cancel_argv"][1:]
                )
                assert cancel_parsed.target == f"{project}/high"
            else:
                assert recovery["action"] == "abandon_intent"
                assert recovery["argv"] == [
                    "bin/danus",
                    "abandon-intent",
                    f"{project}/high",
                    "--thread-id",
                    f"thread-{index}",
                    "--client-id",
                    intent["client_id"],
                    "--expected-state",
                    state,
                    "--reason",
                    "<OWNER_REASON>",
                    "--acknowledge-paid-outcome-unknown",
                ]
            parsed = cli.build_parser().parse_args(recovery["argv"][1:])
            assert parsed.target == f"{project}/high"

        cli.do_new("Pbad", roles="high:1")
        bad = _wl("Pbad", "high")
        control = bad.project_dir / ".human-intervention"
        control.mkdir()
        (control / "events.sqlite3").mkdir()
        status = cli.worker_status(bad)
        assert status["unfinished_paid_intent"] is None
        assert "canonical paid-intent ledger" in status["intent_ledger_error"]


def test_alive_permission_error_means_alive():
    """A pid we can't signal (owned by another user, e.g. init pid 1) raises
    PermissionError from ``os.kill(pid, 0)`` -> treated as alive ('exists but not
    ours'). pid 1 always exists and is root-owned when we're not root."""
    if os.geteuid() == 0:
        return  # as root os.kill(1,0) succeeds; the PermissionError branch is unreachable
    assert cli._alive(1) is True


def test_alive_zombie_is_dead():
    """A child that exited but hasn't been reaped is a zombie; /proc reports state
    'Z' and ``_alive`` must call it dead. We fork a child that exits immediately
    and do NOT wait() it, so it lingers as a zombie we own."""
    import subprocess
    import time
    if not Path("/proc").is_dir():
        return  # the implementation's /proc zombie-state check is Linux-specific
    # 'true' exits at once; without wait() it becomes a zombie child of us.
    proc = subprocess.Popen(["true"])
    try:
        # wait for the kernel to mark it Z (exited, unreaped)
        pid = proc.pid
        def is_zombie():
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
                return stat.rsplit(")", 1)[1].split()[0] == "Z"
            except (OSError, IndexError):
                return False
        end = time.time() + 5
        while time.time() < end and not is_zombie():
            time.sleep(0.02)
        assert is_zombie(), "child did not become a zombie"
        assert cli._alive(pid) is False              # /proc state 'Z' => dead
    finally:
        proc.wait()                                   # reap it


# --------------------------------------------------------------------------- #
# worker_status labels                                                          #
# --------------------------------------------------------------------------- #

def test_worker_status_stuck_label(tmp: Path):
    """alive + state=running + round_started_at far in the past -> 'stuck?'."""
    with _project_env(tmp, DANUS_ROUND_HARD_TIMEOUT="10"):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        _write_pid_record(wl, os.getpid())
        old = 1.0                                     # epoch ~1970 => hugely stale
        wl.status.write_text(json.dumps(
            {"state": "running", "round": 5, "round_started_at": old,
             "last_round_at": old, "last_fact_id": "F9"}))
        with _fake_process_identity(os.getpid()):
            s = cli.worker_status(wl)
        assert s["alive"] is True and s["label"] == "stuck?"
        assert s["age_s"] is not None and s["last_fact_id"] == "F9"


def test_worker_status_working_and_dead_labels(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        # alive + running but fresh => 'working'
        import time
        _write_pid_record(wl, os.getpid())
        wl.status.write_text(json.dumps(
            {"state": "running", "round": 2, "round_started_at": time.time()}))
        with _fake_process_identity(os.getpid()):
            assert cli.worker_status(wl)["label"] == "working"
        # not alive + unknown terminal state => 'dead'
        _write_pid_record(wl, 2_000_000_000)
        wl.status.write_text(json.dumps({"state": "weird", "round": 3}))
        d = cli.worker_status(wl)
        assert d["alive"] is False and d["label"] == "dead" and d["age_s"] is None
        # not alive + recognized terminal state => that state as label
        wl.status.write_text(json.dumps({"state": "deadline", "round": 3}))
        assert cli.worker_status(wl)["label"] == "deadline"


def test_worker_status_json_exposes_layered_paid_and_recovery_outcomes(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        paid = {"round": 2, "rc": 124, "terminal_status": "interrupted"}
        attempt = {
            "round": 3,
            "rc": 123,
            "dispatch_state": "none",
            "failure_code": "thread_history_exceeds_transport_limit",
        }
        recovery = {
            "action": "rotate_thread",
            "argv": ["bin/danus", "rotate-thread", "P/high"],
        }
        wl.status.write_text(
            json.dumps(
                {
                    "state": "error",
                    "round": 3,
                    "error": "bounded resume failed",
                    "last_paid_turn": paid,
                    "last_attempt": attempt,
                    "recovery_required": recovery,
                }
            ),
            encoding="utf-8",
        )
        status = cli.worker_status(wl)
    assert status["error"] == "bounded resume failed"
    assert status["last_paid_turn"] == paid
    assert status["last_attempt"] == attempt
    assert status["recovery_required"] == recovery


# --------------------------------------------------------------------------- #
# do_start: mocked spawn, locked path, no-workers, project-wide + stagger       #
# --------------------------------------------------------------------------- #

def test_do_start_calls_spawn_with_worker_dir(tmp: Path):
    with _project_env(tmp), _patch_spawn() as fake:
        cli.do_new("P", roles="high:1")
        res = cli.do_start("P/high")
        assert res == [{"worker": "high", "result": "started"}]
        assert fake.calls == [_wl("P", "high").dir]
        wl = _wl("P", "high")
        assert cli._read_pid(wl) == os.getpid()      # pid file written from fake pid
        # second start sees our-own-pid as alive => idempotent already-running
        res2 = cli.do_start("P/high")
        assert res2 == [{"worker": "high", "result": "already-running"}]
        assert len(fake.calls) == 1                   # spawn NOT called again


def test_do_start_locked_returns_locked(tmp: Path):
    import fcntl
    with _project_env(tmp), _patch_spawn():
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        wl.dir.mkdir(parents=True, exist_ok=True)
        held = open(wl.lock, "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            assert cli._start_one(wl) == "locked"
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()


def test_do_start_clears_stale_stop(tmp: Path):
    with _project_env(tmp), _patch_spawn():
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        wl.dir.mkdir(parents=True, exist_ok=True)
        wl.stop.touch()
        assert cli._start_one(wl) == "started"
        assert not wl.stop.exists()                   # stale stop cleared


def test_start_and_stop_fail_closed_on_legacy_pid_without_side_effects(tmp: Path):
    with _project_env(tmp), _patch_spawn() as fake:
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        wl.pid.write_text("12345\n", encoding="ascii")
        signals = []
        original_killpg = cli.os.killpg
        cli.os.killpg = lambda pgid, sig: signals.append((pgid, sig))
        try:
            for operation in (
                lambda: cli._start_one(wl),
                lambda: cli._stop_one(wl, force=True),
            ):
                try:
                    operation()
                    raise AssertionError("legacy PID record must fail closed")
                except cli.ProcessIdentityError:
                    pass
        finally:
            cli.os.killpg = original_killpg
        assert fake.calls == []
        assert signals == []
        assert wl.pid.read_text(encoding="ascii") == "12345\n"


def test_legacy_pid_status_is_explicit_and_public_lifecycle_is_actionable(tmp: Path):
    with _project_env(tmp), _patch_spawn() as fake:
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        wl.pid.write_text("12345\n", encoding="ascii")

        status = cli.worker_status(wl)
        assert status["alive"] is False
        assert status["label"] == "pid-unsafe"
        assert "PID record" in status["pid_record_error"]
        assert cli.do_list()[0]["live"] == 0

        for operation in (
            lambda: cli.do_start("P/high"),
            lambda: cli.do_stop("P/high", force=True),
        ):
            error = _expect_exit(operation)
            assert "archive the old PID record" in str(error)
            assert str(wl.pid) in str(error)

        assert fake.calls == []
        assert wl.pid.read_text(encoding="ascii") == "12345\n"


def test_pid_record_write_failure_cleans_unregistered_child(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        pid = 424242
        cleaned = []
        originals = (
            cli.spawn_loop,
            cli._capture_pid_record,
            cli._write_pid_record,
            cli._cleanup_unregistered_child,
        )
        fake_proc = _FakeProcess(pid)
        cli.spawn_loop = lambda _wdir: fake_proc
        cli._capture_pid_record = lambda _wl, _pid: _pid_record(_wl, _pid)
        cli._write_pid_record = lambda _wl, _record: (_ for _ in ()).throw(
            OSError("simulated disk fault")
        )
        cli._cleanup_unregistered_child = cleaned.append
        try:
            try:
                cli._start_one(wl)
                raise AssertionError("PID write fault must fail start")
            except OSError as exc:
                assert "disk fault" in str(exc)
        finally:
            (
                cli.spawn_loop,
                cli._capture_pid_record,
                cli._write_pid_record,
                cli._cleanup_unregistered_child,
            ) = originals
        assert cleaned == [fake_proc]
        assert not wl.pid.exists()


def test_fast_child_exit_after_pid_write_removes_matching_record_and_reaps(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        pid = 434343
        cleaned = []
        originals = (
            cli.spawn_loop,
            cli._capture_pid_record,
            cli._pid_record_is_live,
            cli._cleanup_unregistered_child,
        )
        fake_proc = _FakeProcess(pid)
        cli.spawn_loop = lambda _wdir: fake_proc
        cli._capture_pid_record = lambda _wl, _pid: _pid_record(_wl, _pid)
        cli._pid_record_is_live = lambda _record: False
        cli._cleanup_unregistered_child = cleaned.append
        try:
            try:
                cli._start_one(wl)
                raise AssertionError("fast child exit must fail start")
            except cli.ProcessIdentityError as exc:
                assert "exited during" in str(exc)
        finally:
            (
                cli.spawn_loop,
                cli._capture_pid_record,
                cli._pid_record_is_live,
                cli._cleanup_unregistered_child,
            ) = originals
        assert cleaned == [fake_proc]
        assert not wl.pid.exists()


def test_cleanup_unregistered_child_kills_and_waitpid_reaps():
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    cli._cleanup_unregistered_child(proc)
    assert cli._alive(proc.pid) is False
    try:
        os.waitpid(proc.pid, os.WNOHANG)
        raise AssertionError("unregistered child was not reaped")
    except ChildProcessError:
        pass


def test_cleanup_of_already_reaped_handle_never_signals_reused_numeric_pid():
    unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
    fake = _FakeProcess(unrelated.pid, returncode=70)
    signals = []
    original_kill = cli.os.kill
    original_killpg = cli.os.killpg
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    try:
        cli._cleanup_unregistered_child(fake)
        assert signals == []
        assert cli._alive(unrelated.pid)
    finally:
        cli.os.kill = original_kill
        cli.os.killpg = original_killpg
        if cli._alive(unrelated.pid):
            os.killpg(unrelated.pid, signal.SIGKILL)
        unrelated.wait(timeout=5)


def test_start_retains_spawn_handle_through_durable_birth_registration(tmp: Path):
    finalized = []

    class TrackingProcess(_FakeProcess):
        def __del__(self):
            finalized.append(True)

    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        holder = [TrackingProcess(os.getpid())]
        originals = (cli.spawn_loop, cli._capture_pid_record, cli._process_identity)

        def spawn(_wdir):
            return holder.pop()

        def capture(worker, pid):
            gc.collect()
            assert finalized == []
            return _pid_record(worker, pid, token="retained-handle")

        cli.spawn_loop = spawn
        cli._capture_pid_record = capture
        cli._process_identity = lambda pid: {
            "pid": pid,
            "pgid": pid,
            "start_token": "retained-handle",
            "state": "R",
        }
        try:
            assert cli._start_one(wl) == "started"
        finally:
            cli.spawn_loop, cli._capture_pid_record, cli._process_identity = originals


def test_start_clears_inherited_sigchld_ignore_and_reaps_fast_exit(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        original_spawn = cli.spawn_loop
        original_sigchld = signal.getsignal(signal.SIGCHLD)

        def fast_spawn(_wdir):
            proc = subprocess.Popen(
                [sys.executable, "-c", "pass"], start_new_session=True
            )
            # Do not poll/wait: with SIGCHLD reset by _start_one this leaves a
            # zombie whose PID cannot be reused until cleanup owns the reap.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = subprocess.run(
                    ["ps", "-o", "state=", "-p", str(proc.pid)],
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if state.startswith("Z"):
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("child did not reach zombie state")
            return proc

        cli.spawn_loop = fast_spawn
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        try:
            try:
                cli._start_one(wl)
                raise AssertionError("fast-exit child must not register")
            except cli.ProcessIdentityError:
                pass
            assert signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN
            assert not wl.pid.exists()
        finally:
            cli.spawn_loop = original_spawn
            signal.signal(signal.SIGCHLD, original_sigchld)


def test_failed_registration_sweeps_fast_worker_stubborn_grandchild(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        marker = tmp / "fast-worker-grandchild"
        original_spawn = cli.spawn_loop

        def fast_spawn(_wdir):
            code = (
                "import os,pathlib,signal,subprocess,sys\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text("
                "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", code], start_new_session=True
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = subprocess.run(
                    ["ps", "-o", "state=", "-p", str(proc.pid)],
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if marker.exists() and state.startswith("Z"):
                    return proc
                time.sleep(0.02)
            raise AssertionError("fast worker did not leave a retained zombie")

        cli.spawn_loop = fast_spawn
        try:
            try:
                cli.do_start("P/high", stagger=0)
                raise AssertionError("fast worker must fail registration")
            except SystemExit as exc:
                assert "exited before supervisor registration" in str(exc)
        finally:
            cli.spawn_loop = original_spawn
        leader, group, grandchild, grandchild_group = map(
            int, marker.read_text(encoding="utf-8").split()
        )
        assert group == leader == grandchild_group
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and cli._alive(grandchild):
            time.sleep(0.02)
        assert not cli._alive(leader)
        assert not cli._alive(grandchild)
        assert not wl.pid.exists()


def test_do_start_no_workers_raises(tmp: Path):
    with _project_env(tmp), _patch_spawn():
        e = _expect_exit(cli.do_start, "ghost")
        assert "no workers for target" in str(e)


def test_do_start_project_wide_stagger(tmp: Path):
    with _project_env(tmp), _patch_spawn() as fake:
        cli.do_new("P", roles="high:2")
        res = cli.do_start("P", stagger=0)            # stagger 0 => no sleep
        assert {r["worker"] for r in res} == {"high", "high2"}
        assert {r["result"] for r in res} == {"started"}
        assert {c.name for c in fake.calls} == {"high", "high2"}


# --------------------------------------------------------------------------- #
# do_status / do_stop no-workers                                                #
# --------------------------------------------------------------------------- #

def test_do_status_no_workers_raises(tmp: Path):
    with _project_env(tmp):
        assert "no workers" in str(_expect_exit(cli.do_status, "ghost"))


def test_do_stop_no_workers_raises(tmp: Path):
    with _project_env(tmp):
        assert "no workers" in str(_expect_exit(cli.do_stop, "ghost"))


# --------------------------------------------------------------------------- #
# _stop_one: not-running (graceful + force), graceful touch, force kill path     #
# --------------------------------------------------------------------------- #

def test_stop_one_not_running_graceful(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        assert cli._stop_one(wl, force=False) == "not-running"


def test_stop_one_not_running_force_cleans_pid(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        _write_pid_record(wl, 2_000_000_000)
        assert cli._stop_one(wl, force=True) == "not-running"
        assert not wl.pid.exists()                     # stale pid removed


def test_stop_one_graceful_touches_stop(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        _write_pid_record(wl, os.getpid())
        with _fake_process_identity(os.getpid()):
            assert cli._stop_one(wl, force=False) == "stopping (graceful)"
        assert wl.stop.exists()
        wl.stop.unlink()                               # don't leave a stop flag on us


def test_stop_one_force_kills_a_real_child(tmp: Path):
    """The CLI never treats an external recorded process as its signal child."""
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        cli._write_pid_record(wl, cli._capture_pid_record(wl, proc.pid))
        try:
            assert cli._stop_one(wl, force=True) == "stopping (cooperative force)"
            assert cli._alive(proc.pid) is True
            assert wl.pid.exists()
            assert wl.stop.read_text(encoding="utf-8") == "force\n"
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# do_finalize: validate + write / reject unknown / suggestion mode             #
# --------------------------------------------------------------------------- #

def _add_fact(project: str, statement: str = "S", proof: str = "P", preds=None) -> str:
    """Add a verified fact to a project's fact graph (test helper — writes via the
    core FactGraph, the same path fact_submit uses on accept)."""
    from danus.core import FactGraph
    fg = FactGraph(L.project_dir(project))
    return fg.add(problem_id="p", author="a", statement=statement, proof=proof,
                  predecessors=preds or [])


def test_finalize_validates_and_writes(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        fid = _add_fact("P")
        r = cli.do_finalize("P", [fid])
        assert r["target_fact_ids"] == [fid]
        target = L.project_dir("P") / "TARGET.md"
        assert target.exists() and fid in target.read_text(encoding="utf-8")
        # write-paper's reader sees the same id
        from danus.write_paper import assemble
        assert assemble.target_fact_ids(L.project_dir("P")) == [fid]


def test_finalize_dedups_preserving_order(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        f1 = _add_fact("P", statement="one")
        f2 = _add_fact("P", statement="two")
        r = cli.do_finalize("P", [f1, f2, f1])
        assert r["target_fact_ids"] == [f1, f2]


def test_finalize_rejects_unknown_fact_id(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        e = _expect_exit(cli.do_finalize, "P", ["fact_does_not_exist"])
        assert "unknown fact id" in str(e)
        # nothing written
        assert not (L.project_dir("P") / "TARGET.md").exists()


def test_finalize_rejects_unknown_project(tmp: Path):
    with _project_env(tmp):
        assert "no such project" in str(_expect_exit(cli.do_finalize, "ghost", ["x"]))


def test_finalize_suggestion_mode_writes_nothing(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        leaf = _add_fact("P", statement="leaf")
        top = _add_fact("P", statement="top", preds=[leaf])   # leaf is a predecessor
        r = cli.do_finalize("P", [])                          # suggestion mode
        assert "suggested" in r
        assert r["suggested"] == [top], "only the terminal fact is suggested"
        assert leaf not in r["suggested"]
        assert not (L.project_dir("P") / "TARGET.md").exists(), "suggestion writes nothing"


def test_main_finalize_write_and_suggest(tmp: Path):
    with _project_env(tmp), _patch_spawn():
        _run_main(["new", "P", "--roles", "high:1"])
        fid = _add_fact("P")
        rc, out = _run_main(["finalize", "P", fid])
        assert rc == 0 and "finalized target for P" in out and fid in out
        # suggestion mode via main (blank fact_ids)
        rc, out = _run_main(["finalize", "P"])
        assert rc == 0 and ("candidate target facts" in out or "no candidate" in out)


# --------------------------------------------------------------------------- #
# do_list: bad project.json, formatting                                         #
# --------------------------------------------------------------------------- #

def test_do_list_bad_project_json(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1", model="gpt-5.5")
        (L.project_dir("P") / "project.json").write_text("{ broken", encoding="utf-8")
        rows = {r["project"]: r for r in cli.do_list()}
        assert rows["P"]["model"] == "—"               # unparseable meta => dash


def test_do_list_missing_project_json(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        (L.project_dir("P") / "project.json").unlink()
        rows = {r["project"]: r for r in cli.do_list()}
        assert rows["P"]["model"] == "—"


# --------------------------------------------------------------------------- #
# text formatters _fmt_list / _fmt_status                                       #
# --------------------------------------------------------------------------- #

def test_fmt_list_empty_and_rows():
    assert cli._fmt_list([]) == "(no projects under the agents root)"
    rows = [{"project": "Proj", "workers": 3, "live": 1, "model": "gpt-5.5"}]
    out = cli._fmt_list(rows)
    assert "PROJECT" in out and "Proj" in out and "gpt-5.5" in out


def test_fmt_status_rows():
    rows = [
        {"worker": "high", "label": "working", "state": "running", "round": 4,
         "age_s": 12.4, "last_fact_id": "F7"},
        {"worker": "xhigh", "label": "dead", "state": "created", "round": 0,
         "age_s": None, "last_fact_id": None},
    ]
    out = cli._fmt_status(rows)
    assert "WORKER" in out and "high" in out and "xhigh" in out
    assert "12s" in out                                # age rendered from float
    assert "—" in out                                  # None age / fact => dash


# --------------------------------------------------------------------------- #
# _task_from_args: --task / --file / --stdin / none                             #
# --------------------------------------------------------------------------- #

class _Args:
    def __init__(self, task=None, file=None, stdin=False):
        self.task = task
        self.file = file
        self.stdin = stdin


def test_task_from_args_task():
    assert cli._task_from_args(_Args(task="direct task")) == "direct task"


def test_task_from_args_file(tmp: Path):
    p = tmp / "task.txt"
    p.write_text("from a file\n", encoding="utf-8")
    assert cli._task_from_args(_Args(file=str(p))) == "from a file\n"


def test_task_from_args_stdin(monkeypatch=None):
    import sys
    old = sys.stdin
    sys.stdin = io.StringIO("piped task\n")
    try:
        assert cli._task_from_args(_Args(stdin=True)) == "piped task\n"
    finally:
        sys.stdin = old


def test_task_from_args_none_raises():
    assert "one of --task" in str(_expect_exit(cli._task_from_args, _Args()))


# --------------------------------------------------------------------------- #
# build_parser                                                                  #
# --------------------------------------------------------------------------- #

def test_build_parser_all_verbs():
    p = cli.build_parser()
    assert p.parse_args(["list", "--json"]).cmd == "list"
    a = p.parse_args(["new", "P", "--roles", "high:2", "--model", "m"])
    assert a.cmd == "new" and a.project == "P" and a.roles == "high:2" and a.model == "m"
    a = p.parse_args(["assign", "P/high", "--task", "t"])
    assert a.cmd == "assign" and a.target == "P/high" and a.task == "t"
    a = p.parse_args(["finalize", "P", "fact_a", "fact_b"])
    assert a.cmd == "finalize" and a.project == "P" and a.fact_ids == ["fact_a", "fact_b"]
    assert p.parse_args(["finalize", "P"]).fact_ids == []      # suggestion mode
    assert p.parse_args(["start", "P"]).cmd == "start"
    assert p.parse_args(["status", "P", "--json"]).json is True
    assert p.parse_args(["stop", "P", "--force"]).force is True
    a = p.parse_args(
        ["reset-thread", "P/high", "--expected-thread-id", "thread-lost"]
    )
    assert a.cmd == "reset-thread" and a.expected_thread_id == "thread-lost"
    a = p.parse_args(
        [
            "rotate-thread",
            "P/high",
            "--expected-thread-id",
            "thread-large",
            "--reason",
            "history over limit",
        ]
    )
    assert a.cmd == "rotate-thread"
    assert a.expected_thread_id == "thread-large"
    assert a.reason == "history over limit"
    a = p.parse_args(
        [
            "abandon-intent",
            "P/high",
            "--thread-id",
            "thread-large",
            "--client-id",
            "danus-round:exact",
            "--expected-state",
            "delivery_unknown",
            "--reason",
            "owner reconciled available history",
            "--acknowledge-paid-outcome-unknown",
        ]
    )
    assert a.cmd == "abandon-intent"
    assert a.thread_id == "thread-large"
    assert a.client_id == "danus-round:exact"
    assert a.expected_state == "delivery_unknown"
    assert a.acknowledge_paid_outcome_unknown is True
    a = p.parse_args(
        [
            "cancel-prepared-intent",
            "P/high",
            "--thread-id",
            "thread-prepared",
            "--client-id",
            "danus-round:prepared",
            "--reason",
            "immutable configuration drifted",
        ]
    )
    assert a.cmd == "cancel-prepared-intent"
    assert a.thread_id == "thread-prepared"
    assert a.client_id == "danus-round:prepared"
    assert a.reason == "immutable configuration drifted"
    # subcommand is required
    try:
        p.parse_args([])
        raise AssertionError("expected argparse to require a subcommand")
    except SystemExit:
        pass


# --------------------------------------------------------------------------- #
# main dispatch — every verb, text + json branches                             #
# --------------------------------------------------------------------------- #

def _run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def test_main_new_then_list_text_and_json(tmp: Path):
    with _project_env(tmp), _patch_spawn():
        rc, out = _run_main(["new", "P", "--roles", "high:2", "--model", "gpt-5.5"])
        assert rc == 0 and "created P with 2 workers" in out and "high" in out
        rc, out = _run_main(["list"])
        assert rc == 0 and "PROJECT" in out and "P" in out
        rc, out = _run_main(["list", "--json"])
        rows = json.loads(out)
        assert rc == 0 and rows[0]["project"] == "P" and rows[0]["workers"] == 2


def test_main_assign(tmp: Path):
    with _project_env(tmp), _patch_spawn():
        _run_main(["new", "P", "--roles", "high:1"])
        rc, out = _run_main(["assign", "P/high", "--task", "prove lemma 4"])
        assert rc == 0 and "assigned P/high" in out
        assert _wl("P", "high").task.read_text() == "prove lemma 4\n"


def test_main_start_status_stop(tmp: Path):
    with _project_env(tmp), _patch_spawn() as fake:
        _run_main(["new", "P", "--roles", "high:1"])
        rc, out = _run_main(["start", "P/high"])
        assert rc == 0 and "high: started" in out and len(fake.calls) == 1
        # status text branch (worker is "alive" = our pid)
        rc, out = _run_main(["status", "P/high"])
        assert rc == 0 and "WORKER" in out
        # status json branch
        rc, out = _run_main(["status", "P/high", "--json"])
        assert rc == 0 and json.loads(out)[0]["worker"] == "high"
        # stop graceful (worker "alive" via our pid => touches .stop)
        rc, out = _run_main(["stop", "P/high"])
        assert rc == 0 and "graceful" in out
        _wl("P", "high").stop.unlink(missing_ok=True)


def test_main_stop_force_not_running(tmp: Path):
    with _project_env(tmp), _patch_spawn():
        _run_main(["new", "P", "--roles", "high:1"])
        rc, out = _run_main(["stop", "P/high", "--force"])
        assert rc == 0 and "not-running" in out


# --------------------------------------------------------------------------- #
# python -m danus.orchestration  (the __main__ entry point)                     #
# --------------------------------------------------------------------------- #

def test_dunder_main_entrypoint(tmp: Path):
    """Exercise ``__main__.py`` via runpy with a mocked ``main`` so no real verb
    runs. Asserts it calls ``sys.exit`` with main()'s return code."""
    import danus.orchestration.cli as climod

    orig = climod.main
    calls = {}

    def fake_main(argv=None):
        calls["argv"] = argv
        return 0

    climod.main = fake_main
    old_argv = None
    try:
        import sys
        old_argv = sys.argv[:]
        sys.argv = ["danus", "list"]
        try:
            runpy.run_module("danus.orchestration", run_name="__main__")
            raise AssertionError("expected SystemExit from __main__")
        except SystemExit as e:
            assert e.code == 0
    finally:
        climod.main = orig
        if old_argv is not None:
            import sys
            sys.argv = old_argv


# --------------------------------------------------------------------------- #
# runner (standalone parity with test_orchestration.py)                         #
# --------------------------------------------------------------------------- #

def main() -> None:
    no_arg = [
        test_fmt_list_empty_and_rows, test_fmt_status_rows,
        test_task_from_args_task, test_task_from_args_stdin,
        test_task_from_args_none_raises, test_build_parser_all_verbs,
        test_alive_permission_error_means_alive, test_alive_zombie_is_dead,
        test_darwin_libproc_birth_token_uses_microseconds_and_fails_closed,
        test_cleanup_unregistered_child_kills_and_waitpid_reaps,
    ]
    tmp_tests = [
        test_alive_variants,
        test_worker_status_exposes_canonical_unfinished_paid_intent_and_recovery_argv,
        test_stop_one_force_sigkill_fallback,
        test_stop_one_force_sigkill_killpg_raises,
        test_alive_proc_read_failure_defaults_alive,
        test_stop_one_pid_reuse_mismatch_never_signals,
        test_read_pid_missing_and_garbage, test_read_status_missing_and_bad_json,
        test_worker_status_stuck_label, test_worker_status_working_and_dead_labels,
        test_do_start_calls_spawn_with_worker_dir, test_do_start_locked_returns_locked,
        test_do_start_clears_stale_stop, test_do_start_no_workers_raises,
        test_start_and_stop_fail_closed_on_legacy_pid_without_side_effects,
        test_legacy_pid_status_is_explicit_and_public_lifecycle_is_actionable,
        test_pid_record_write_failure_cleans_unregistered_child,
        test_fast_child_exit_after_pid_write_removes_matching_record_and_reaps,
        test_do_start_project_wide_stagger, test_do_status_no_workers_raises,
        test_do_stop_no_workers_raises, test_stop_one_not_running_graceful,
        test_stop_one_not_running_force_cleans_pid, test_stop_one_graceful_touches_stop,
        test_stop_one_force_kills_a_real_child, test_do_list_bad_project_json,
        test_do_list_missing_project_json, test_task_from_args_file,
        test_finalize_validates_and_writes, test_finalize_dedups_preserving_order,
        test_finalize_rejects_unknown_fact_id, test_finalize_rejects_unknown_project,
        test_finalize_suggestion_mode_writes_nothing, test_main_finalize_write_and_suggest,
        test_main_new_then_list_text_and_json, test_main_assign,
        test_main_start_status_stop, test_main_stop_force_not_running,
        test_dunder_main_entrypoint,
    ]
    for t in no_arg:
        t()
        print(f"  [ok] {t.__name__}")
    for t in tmp_tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL CLI VERB TESTS PASSED")


if __name__ == "__main__":
    main()
