"""Production fault tests for the resident-service guardian (no network spend)."""

from __future__ import annotations

import fcntl
import atexit
import http.client
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts" / "service-identity.py"
PYTHON = Path(sys.executable)
TEST_DIGEST = "a" * 64
_SHORT_ROOT = Path(tempfile.mkdtemp(prefix="danus-guardian-tests-", dir="/tmp"))
atexit.register(lambda: shutil.rmtree(_SHORT_ROOT, ignore_errors=True))


def _run(*args: str | Path, env: dict[str, str] | None = None, timeout: float = 15):
    return subprocess.run(
        [str(value) for value in args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _helper(*args: str | Path, env: dict[str, str] | None = None, timeout: float = 15):
    return _run(PYTHON, "-I", "-B", HELPER, *args, env=env, timeout=timeout)


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _fake_service(path: Path) -> Path:
    path.write_text(
        """import json, os, pathlib, signal, socket, sys, time
mode = os.environ.get('DANUS_TEST_MODE', 'healthy')
marker = os.environ.get('DANUS_TEST_MARKER')
grandchild_marker = os.environ.get('DANUS_TEST_GRANDCHILD')
if mode == 'immediate':
    raise SystemExit(17)
if mode == 'leader_exit':
    child = os.fork()
    if child == 0:
        if grandchild_marker:
            pathlib.Path(grandchild_marker).write_text(str(os.getpid()), encoding='ascii')
        while True:
            time.sleep(1)
    raise SystemExit(0)
if mode in {'ignore_term', 'ignore_term_grandchild'}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == 'ignore_term_grandchild':
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if grandchild_marker:
            pathlib.Path(grandchild_marker).write_text(str(os.getpid()), encoding='ascii')
        while True:
            time.sleep(1)
if marker:
    pathlib.Path(marker).write_text(str(os.getpid()), encoding='ascii')
port = int(os.environ['DANUS_TEST_PORT'])
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('127.0.0.1', port))
sock.listen(8)
nonce = os.environ.get('DANUS_SERVICE_INSTANCE_NONCE', '')
kind = os.environ.get('DANUS_TEST_HEALTH_KIND', 'dashboard')
if kind == 'verify':
    body = {
        'status': 'ok', 'pid': os.getpid(), 'instance_nonce': nonce,
        'output_protocol_version': int(os.environ.get('DANUS_TEST_PROTOCOL', '3')),
        'verifier_bundle_digest': os.environ.get('DANUS_TEST_DIGEST', 'a' * 64),
    }
else:
    body = {'status': 'ok', 'pid': os.getpid(), 'instance_nonce': nonce}
if mode == 'foreign_nonce':
    body['instance_nonce'] = '0' * 32
if mode == 'huge':
    body['padding'] = 'x' * 5000
status = 500 if mode == 'status500' else 200
raw = b'not-json' if mode == 'malformed' else json.dumps(body, separators=(',', ':')).encode()
response = (
    f'HTTP/1.1 {status} TEST\\r\\nContent-Type: application/json\\r\\n'
    f'Content-Length: {len(raw)}\\r\\nConnection: close\\r\\n\\r\\n'
).encode() + raw
while True:
    conn, _ = sock.accept()
    try:
        conn.settimeout(1)
        conn.recv(4096)
        if mode == 'drip':
            for value in response:
                try:
                    conn.sendall(bytes([value]))
                except OSError:
                    break
                time.sleep(0.15)
        else:
            conn.sendall(response)
    finally:
        conn.close()
""",
        encoding="utf-8",
    )
    return path


def _context(tmp_path: Path, *, kind: str = "dashboard", mode: str = "healthy"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    short = Path(tempfile.mkdtemp(prefix="c-", dir=_SHORT_ROOT))
    run = short / "run"
    logs = short / "logs"
    assert _helper("prepare", run, logs).returncode == 0
    service = "verify" if kind == "verify" else "dashboard-Test"
    entry = "verify" if kind == "verify" else "dashboard Test"
    port = _free_port()
    fake = _fake_service(tmp_path / "fake_service.py")
    env = os.environ.copy()
    env.update(
        DANUS_TEST_MODE=mode,
        DANUS_TEST_PORT=str(port),
        DANUS_TEST_HEALTH_KIND=kind,
        DANUS_TEST_PROTOCOL="3",
        DANUS_TEST_DIGEST=TEST_DIGEST,
        DANUS_TEST_MARKER=str(tmp_path / "child.pid"),
        DANUS_TEST_GRANDCHILD=str(tmp_path / "grandchild.pid"),
    )
    add = _helper("manifest", "add", run / "autostart", run / "autostart.lock", entry)
    assert add.returncode == 0, add.stderr
    generation = json.loads(add.stdout)["generation"]
    health_url = f"http://127.0.0.1:{port}/health"
    return {
        "run": run,
        "logs": logs,
        "record": run / f"{service}.pid",
        "lock": run / f"{service}.lock",
        "socket": run / f"{service}.sock",
        "log": logs / f"{service}.log",
        "manifest": run / "autostart",
        "manifest_lock": run / "autostart.lock",
        "service": service,
        "entry": entry,
        "generation": generation,
        "kind": kind,
        "url": health_url,
        "port": port,
        "protocol": "3" if kind == "verify" else "-",
        "digest": TEST_DIGEST if kind == "verify" else "-",
        "fake": fake,
        "env": env,
    }


def _start(ctx, *, env: dict[str, str] | None = None, timeout: float = 15):
    return _helper(
        "start",
        ctx["record"],
        ctx["lock"],
        ctx["socket"],
        ctx["service"],
        ctx["log"],
        str(ctx.get("start_timeout", "3")),
        ctx["kind"],
        ctx["url"],
        ctx["protocol"],
        ctx["digest"],
        ctx["manifest"],
        ctx["manifest_lock"],
        ctx["entry"],
        ctx["generation"],
        "--",
        PYTHON,
        ctx["fake"],
        env=env or ctx["env"],
        timeout=timeout,
    )


def _stop(ctx, timeout: float = 10):
    return _helper(
        "stop",
        ctx["record"],
        ctx["lock"],
        "6",
        ctx["generation"],
        timeout=timeout,
    )


def _cleanup(ctx) -> None:
    if ctx["record"].exists():
        _stop(ctx)
        _helper("reconcile", ctx["record"], ctx["lock"])


def _actual_verify_context(tmp_path: Path):
    ctx = _context(tmp_path, kind="verify")
    contract_result = _helper("verifier-contract", ROOT)
    assert contract_result.returncode == 0, contract_result.stderr
    contract = json.loads(contract_result.stdout)
    ctx["protocol"] = str(contract["protocol"])
    ctx["digest"] = contract["digest"]
    ctx["start_timeout"] = "10"
    wrapper = tmp_path / "run_verify.py"
    wrapper.write_text(
        "import runpy\nrunpy.run_module('danus.verify', run_name='__main__')\n",
        encoding="utf-8",
    )
    codex = tmp_path / "stubborn_codex.py"
    codex.write_text(
        f"#!{PYTHON}\n"
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(120)'])\n"
        "Path(os.environ['DANUS_TEST_GROUP_MARKER']).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    os.chmod(codex, 0o700)
    marker = tmp_path / "paid-group.txt"
    ctx["fake"] = wrapper
    ctx["paid_marker"] = marker
    ctx["env"].update(
        VERIFY_HOST="127.0.0.1",
        VERIFY_PORT=str(ctx["port"]),
        DANUS_CODEX_BIN=str(codex),
        DANUS_STATE_DIR=str(tmp_path / "state"),
        VERIFIER_RESULTS_DIR=str(tmp_path / "verify-runs"),
        VERIFY_AGENT_HOME=str(tmp_path / "verify-home"),
        CODEX_TIMEOUT_SECONDS="0",
        DANUS_VERIFY_MAX_CONCURRENT_REQUESTS="1",
        DANUS_TEST_GROUP_MARKER=str(marker),
    )
    return ctx


def _verify_payload(ctx) -> dict[str, object]:
    health = _helper("verify-health", ctx["record"], ctx["url"])
    assert health.returncode == 0, health.stdout + health.stderr
    contract = json.loads(health.stdout)
    instance_nonce = json.loads(ctx["record"].read_text(encoding="utf-8"))[
        "instance_nonce"
    ]
    return {
        "expected_verifier_instance_nonce": instance_nonce,
        "expected_output_protocol_version": contract["protocol"],
        "expected_verifier_bundle_digest": contract["digest"],
        "statement": "For every integer n, n + 0 equals n.",
        "proof": (
            "Zero is the additive identity of the integers, so adding zero to "
            "any integer n leaves the value unchanged. Hence n + 0 = n for "
            "every integer n, as required."
        ),
    }


def _scheduler_snapshot(port: int, timeout: float = 4.0) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/scheduler")
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert isinstance(body, dict)
        return body
    finally:
        connection.close()


def test_guardian_lifecycle_uses_0600_nonce_control_and_no_shell_numeric_kill(tmp_path):
    ctx = _context(tmp_path)
    try:
        started = _start(ctx)
        assert started.returncode == 0, started.stdout + started.stderr
        start_body = json.loads(started.stdout)
        record = json.loads(ctx["record"].read_text(encoding="utf-8"))
        assert record["schema_version"] == 2
        assert record["authority"] == "unix_guardian"
        assert len(record["control_nonce"]) == len(record["instance_nonce"]) == 32
        assert stat.S_IMODE(ctx["record"].stat().st_mode) == 0o600
        assert stat.S_IMODE(ctx["socket"].stat().st_mode) == 0o600
        status_result = _helper("status", ctx["record"])
        status_body = json.loads(status_result.stdout)
        assert status_body["state"] == "ready"
        assert status_body["child_pid"] == start_body["child_pid"]
        assert "control_nonce" not in status_result.stdout
        assert "instance_nonce" not in status_result.stdout
        assert record["control_nonce"] not in ctx["log"].read_text(
            encoding="utf-8", errors="replace"
        )
        stopped = _stop(ctx)
        assert stopped.returncode == 0, stopped.stderr
        assert not ctx["record"].exists() and not ctx["socket"].exists()
        for script in ("services.sh", "env.sh", "recover.sh"):
            text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
            assert "kill -" not in text and "kill \"$" not in text
    finally:
        _cleanup(ctx)


def test_verify_health_requires_child_pid_nonce_protocol_digest(tmp_path):
    ctx = _context(tmp_path, kind="verify")
    try:
        assert _start(ctx).returncode == 0
        health = _helper("verify-health", ctx["record"], ctx["url"])
        assert health.returncode == 0
        body = json.loads(health.stdout)
        assert body["state"] == "ours" and body["protocol"] == 3
        assert body["digest"] == TEST_DIGEST
    finally:
        _cleanup(ctx)


def test_control_socket_rejects_wrong_nonce_and_oversized_protocol(tmp_path):
    ctx = _context(tmp_path)
    try:
        assert _start(ctx).returncode == 0
        record = json.loads(ctx["record"].read_text(encoding="utf-8"))
        for payload in (
            json.dumps(
                {"version": 1, "nonce": "0" * 32, "command": "stop"}
            ).encode()
            + b"\n",
            (
                '{"version":NaN,"nonce":"'
                + record["control_nonce"]
                + '","command":"stop"}\n'
            ).encode("ascii"),
            b"x" * 5000 + b"\n",
        ):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(record["socket_path"])
            client.sendall(payload)
            response = client.recv(4096)
            client.close()
            assert b"rejected" in response
        # A same-UID peer without the nonce cannot keep the state machine from
        # generation reconciliation by dripping a partial request forever.
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(record["socket_path"])
        for _ in range(4):
            try:
                client.sendall(b" ")
            except OSError:
                break
            time.sleep(0.3)
        client.settimeout(0.5)
        response = client.recv(4096)
        client.close()
        assert b"rejected" in response
        assert json.loads(_helper("status", ctx["record"]).stdout)["state"] == "ready"
    finally:
        _cleanup(ctx)


@pytest.mark.parametrize("mode", ["foreign_nonce", "huge", "status500", "drip"])
def test_readiness_rejects_foreign_nonce_huge_body_and_http_500(tmp_path, mode):
    ctx = _context(tmp_path, mode=mode)
    result = _start(ctx)
    assert result.returncode != 0
    assert "readiness" in result.stdout or "health" in ctx["log"].read_text(
        encoding="utf-8", errors="replace"
    )
    assert _wait(lambda: not ctx["record"].exists())
    marker = tmp_path / "child.pid"
    if marker.exists():
        assert _wait(lambda: not _running(int(marker.read_text())))


def test_exec_failure_and_immediate_exit_leave_no_orphan_or_record(tmp_path):
    ctx = _context(tmp_path)
    missing = dict(ctx)
    missing["fake"] = tmp_path / "does-not-exist"
    result = _start(missing)
    assert result.returncode != 0
    assert _wait(lambda: not ctx["record"].exists())

    immediate = _context(tmp_path / "immediate", mode="immediate")
    env = immediate["env"].copy()
    start = time.monotonic()
    proc = subprocess.run(
        [
            str(PYTHON), "-I", "-B", str(HELPER), "start",
            str(immediate["record"]), str(immediate["lock"]),
            str(immediate["socket"]), immediate["service"], str(immediate["log"]),
            "2", immediate["kind"], immediate["url"], "-", "-",
            str(immediate["manifest"]), str(immediate["manifest_lock"]),
            immediate["entry"], immediate["generation"], "--",
            str(PYTHON), str(immediate["fake"]),
        ],
        env=env,
        preexec_fn=lambda: signal.signal(signal.SIGCHLD, signal.SIG_IGN),
        text=True,
        capture_output=True,
        check=False,
        timeout=8,
    )
    assert proc.returncode != 0 and time.monotonic() - start < 6
    assert _wait(lambda: not immediate["record"].exists())


def test_leader_dead_child_live_is_swept_before_reaping_group_leader(tmp_path):
    ctx = _context(tmp_path, mode="leader_exit")
    result = _start(ctx)
    assert result.returncode != 0
    grandchild = tmp_path / "grandchild.pid"
    assert _wait(grandchild.exists)
    pid = int(grandchild.read_text())
    assert _wait(lambda: not _running(pid)), "leader-dead descendant survived guardian"
    assert _wait(lambda: not ctx["record"].exists())


def test_guardian_sigkill_keeps_lock_until_host_cleans_service_group(tmp_path):
    ctx = _context(tmp_path, mode="ignore_term")
    assert _start(ctx).returncode == 0
    record = json.loads(ctx["record"].read_text())
    status_body = json.loads(_helper("status", ctx["record"]).stdout)
    child_pid = status_body["child_pid"]
    os.kill(record["guardian_pid"], signal.SIGKILL)  # deterministic fault injection

    fd = os.open(ctx["lock"], os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    assert _wait(lambda: not _running(child_pid), 5)

    def lock_free():
        fd2 = os.open(ctx["lock"], os.O_RDWR)
        try:
            try:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        finally:
            os.close(fd2)

    assert _wait(lock_free)
    reconciled = _helper("reconcile", ctx["record"], ctx["lock"])
    assert reconciled.returncode == 0
    assert json.loads(reconciled.stdout)["state"] == "stale_cleared"


def test_guardian_death_after_service_exec_before_status_cannot_orphan(tmp_path):
    ctx = _context(tmp_path, mode="ignore_term_grandchild")
    barrier = tmp_path / "exec-before-status"
    env = ctx["env"].copy()
    env.update(
        DANUS_SERVICE_TEST_BARRIER_POINT="host-after-service-exec-before-status",
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    command = [
        str(PYTHON), "-I", "-B", str(HELPER), "start",
        str(ctx["record"]), str(ctx["lock"]), str(ctx["socket"]),
        ctx["service"], str(ctx["log"]), "3", ctx["kind"], ctx["url"],
        ctx["protocol"], ctx["digest"], str(ctx["manifest"]),
        str(ctx["manifest_lock"]), ctx["entry"], ctx["generation"], "--",
        str(PYTHON), str(ctx["fake"]),
    ]
    launcher = subprocess.Popen(
        command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    reached = Path(str(barrier) + ".reached")
    release = Path(str(barrier) + ".release")
    assert _wait(reached.exists)
    assert _wait((tmp_path / "child.pid").exists)
    assert _wait((tmp_path / "grandchild.pid").exists)
    child_pid = int((tmp_path / "child.pid").read_text())
    grandchild_pid = int((tmp_path / "grandchild.pid").read_text())
    guardian_pid = json.loads(ctx["record"].read_text())["guardian_pid"]
    os.kill(guardian_pid, signal.SIGKILL)
    try:
        # The service-host and actual service share the flock OFD, so even in
        # this exact exec/status gap no replacement can overlap their cleanup.
        assert _start(ctx).returncode == 75
    finally:
        release.touch()
    launcher.communicate(timeout=8)
    assert _wait(lambda: not _running(child_pid), 6)
    assert _wait(lambda: not _running(grandchild_pid), 6)

    def lock_free():
        fd = os.open(ctx["lock"], os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        finally:
            os.close(fd)

    assert _wait(lock_free)
    assert _helper("reconcile", ctx["record"], ctx["lock"]).returncode == 0


@pytest.mark.parametrize("termination", ["down", "guardian_sigkill"])
def test_verify_paid_group_fences_restart_until_stubborn_cleanup(tmp_path, termination):
    ctx = _actual_verify_context(tmp_path)
    started = _start(ctx, timeout=20)
    assert started.returncode == 0, started.stdout + started.stderr
    status = json.loads(_helper("status", ctx["record"]).stdout)
    verify_pid = status["child_pid"]
    guardian_pid = json.loads(ctx["record"].read_text())["guardian_pid"]
    old_generation = ctx["generation"]
    payload = _verify_payload(ctx)
    request_code = (
        "import http.client,json,sys; "
        "c=http.client.HTTPConnection('127.0.0.1',int(sys.argv[1]),timeout=30); "
        "c.request('POST','/verify',body=sys.argv[2],"
        "headers={'Content-Type':'application/json'}); c.getresponse().read()"
    )
    requester = subprocess.Popen(
        [str(PYTHON), "-c", request_code, str(ctx["port"]), json.dumps(payload)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stop = None
    try:
        assert _wait(ctx["paid_marker"].exists, 10)
        paid_pid, paid_host_pgid, paid_child_pid, paid_child_pgid = map(
            int, ctx["paid_marker"].read_text().split()
        )
        assert paid_host_pgid == paid_child_pgid
        assert paid_pid != paid_host_pgid
        # This is the real service, not a TestClient seam: the one paid slot
        # stays occupied while the first Codex process is blocked. Exact
        # duplicates now coalesce, so inspect the scheduler instead of issuing
        # an obsolete busy-rejection probe.
        scheduler = _scheduler_snapshot(ctx["port"])
        assert scheduler["paid_concurrency_limit"] == 1
        assert scheduler["running"] == scheduler["active_keys"] == 1

        deleted = _helper(
            "manifest", "del", ctx["manifest"], ctx["manifest_lock"], ctx["entry"]
        )
        assert json.loads(deleted.stdout)["generation"] == old_generation
        if termination == "down":
            stop = subprocess.Popen(
                [
                    str(PYTHON), "-I", "-B", str(HELPER), "stop",
                    str(ctx["record"]), str(ctx["lock"]), "15", old_generation,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            os.kill(guardian_pid, signal.SIGKILL)

        added = _helper(
            "manifest", "add", ctx["manifest"], ctx["manifest_lock"], ctx["entry"]
        )
        new_generation = json.loads(added.stdout)["generation"]
        assert new_generation != old_generation
        replacement = dict(ctx)
        replacement["generation"] = new_generation

        assert _wait(lambda: not _running(verify_pid), 8)
        assert _wait(lambda: not _running(guardian_pid), 8)
        # Guardian and service copies are now gone.  Only the detached paid host
        # retains their flock OFD during its TERM grace, which must fence G2.
        assert _running(paid_pid) or _running(paid_child_pid)
        if termination == "down":
            transition = _helper("status", ctx["record"])
            assert transition.returncode == 0
            assert json.loads(transition.stdout)["state"] == "cleanup_in_progress"
            health_transition = _helper("verify-health", ctx["record"], ctx["url"])
            assert health_transition.returncode == 4
            assert json.loads(health_transition.stdout)["state"] == (
                "cleanup_in_progress"
            )
        competing = _start(replacement)
        assert competing.returncode == 75, competing.stdout + competing.stderr

        assert _wait(
            lambda: not any(
                _running(pid)
                for pid in (paid_host_pgid, paid_pid, paid_child_pid)
            ),
            10,
        )
        requester.wait(timeout=8)
        if stop is not None:
            out, err = stop.communicate(timeout=8)
            assert stop.returncode == 0, out + err

        def lock_free():
            fd = os.open(ctx["lock"], os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True
            finally:
                os.close(fd)

        assert _wait(lock_free, 5)
        if termination == "guardian_sigkill":
            reconciled = _helper("reconcile", ctx["record"], ctx["lock"])
            assert reconciled.returncode == 0, reconciled.stdout + reconciled.stderr
        restarted = _start(replacement, timeout=20)
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        assert json.loads(_helper("status", replacement["record"]).stdout)[
            "intent_generation"
        ] == new_generation
        assert _helper(
            "manifest", "del", replacement["manifest"],
            replacement["manifest_lock"], replacement["entry"]
        ).returncode == 0
        stopped = _stop(replacement, timeout=15)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    finally:
        if requester.poll() is None:
            requester.terminate()
            requester.wait(timeout=3)
        if stop is not None and stop.poll() is None:
            stop.terminate()
            stop.wait(timeout=3)


def test_nonterminal_after_kill_never_releases_authority_or_admits_restart(tmp_path):
    ctx = _context(tmp_path, mode="ignore_term")
    barrier = tmp_path / "terminal-wait"
    env = ctx["env"].copy()
    env.update(
        DANUS_SERVICE_TEST_FORCE_NONTERMINAL="1",
        DANUS_SERVICE_TEST_BARRIER_POINT="guardian-before-terminal-wait",
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    assert _start(ctx, env=env).returncode == 0
    stop = subprocess.Popen(
        [
            str(PYTHON), "-I", "-B", str(HELPER), "stop",
            str(ctx["record"]), str(ctx["lock"]), "6", ctx["generation"],
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    release = Path(str(barrier) + ".release")
    try:
        assert _wait(lambda: Path(str(barrier) + ".reached").exists())
        competing = _start(ctx)
        assert competing.returncode == 75
        assert ctx["record"].exists()
        fd = os.open(ctx["lock"], os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        release.touch()
    out, err = stop.communicate(timeout=8)
    assert stop.returncode == 0, out + err
    assert _wait(lambda: not ctx["record"].exists())


@pytest.mark.parametrize("fault", ["kill", "waitid"])
def test_cleanup_syscall_exception_retains_authority_until_exact_reap(tmp_path, fault):
    ctx = _context(tmp_path, mode="ignore_term")
    barrier = tmp_path / f"cleanup-{fault}"
    env = ctx["env"].copy()
    env.update(
        DANUS_SERVICE_TEST_CLEANUP_EXCEPTION=fault,
        DANUS_SERVICE_TEST_BARRIER_POINT="guardian-cleanup-retry",
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    assert _start(ctx, env=env).returncode == 0
    original = json.loads(ctx["record"].read_text(encoding="utf-8"))
    stop = subprocess.Popen(
        [
            str(PYTHON), "-I", "-B", str(HELPER), "stop",
            str(ctx["record"]), str(ctx["lock"]), "6", ctx["generation"],
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    reached = Path(str(barrier) + ".reached")
    release = Path(str(barrier) + ".release")
    try:
        assert _wait(reached.exists)
        # The failed cleanup attempt must retain the authenticated record and
        # lifecycle lock.  A replacement guardian cannot overlap it even if
        # the host happens to reach terminal state while this barrier is held.
        assert json.loads(ctx["record"].read_text())["control_nonce"] == original[
            "control_nonce"
        ]
        competing = _start(ctx)
        assert competing.returncode == 75
        fd = os.open(ctx["lock"], os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        release.touch()
    out, err = stop.communicate(timeout=8)
    assert stop.returncode == 0, out + err
    assert _wait(lambda: not ctx["record"].exists())


def test_open_then_unlinked_record_uses_lock_backed_absence_semantics(
    tmp_path, monkeypatch
):
    helper = runpy.run_path(str(HELPER), run_name="danus_test_service_identity")
    record = tmp_path / "dashboard-Test.pid"
    record.write_bytes(b"{}\n")
    record.chmod(0o600)
    real_fstat = os.fstat

    def unlink_before_fstat(fd):
        record.unlink()
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", unlink_before_fstat)
    with pytest.raises(FileNotFoundError):
        helper["read_record"](record)


@pytest.mark.parametrize(
    "fault",
    [
        "guardian-after-lock",
        "guardian-after-record",
        "guardian-after-exec",
        "host-before-setsid",
    ],
)
def test_guardian_and_pre_setsid_crash_cuts_are_bounded_and_orphan_free(tmp_path, fault):
    ctx = _context(tmp_path)
    env = ctx["env"].copy()
    env["DANUS_SERVICE_FAULT_POINT"] = fault
    start = time.monotonic()
    result = _start(ctx, env=env)
    assert result.returncode != 0 and time.monotonic() - start < 8
    marker = tmp_path / "child.pid"
    if marker.exists():
        assert _wait(lambda: not _running(int(marker.read_text())))
    if fault in {"guardian-after-record", "guardian-after-exec"}:
        # SIGKILL cannot run guardian cleanup; the safe stale record is honest
        # and is removed only by lock-authenticated reconciliation.
        assert ctx["record"].exists()
        assert _wait(
            lambda: _helper("reconcile", ctx["record"], ctx["lock"]).returncode == 0
        )
    else:
        assert _wait(lambda: not ctx["record"].exists())


def test_launcher_sigkill_after_fork_leaves_guardian_as_authority(tmp_path):
    ctx = _context(tmp_path)
    env = ctx["env"].copy()
    env["DANUS_SERVICE_FAULT_POINT"] = "launcher-after-fork"
    result = _start(ctx, env=env)
    assert result.returncode == 97
    assert _wait(ctx["record"].exists)
    assert _wait(lambda: _helper("status", ctx["record"]).returncode == 0)
    try:
        assert _stop(ctx).returncode == 0
    finally:
        _cleanup(ctx)


def test_actual_launcher_sigkill_after_fork_is_taken_over_by_guardian(tmp_path):
    ctx = _context(tmp_path)
    barrier = tmp_path / "launcher"
    env = ctx["env"].copy()
    env.update(
        DANUS_SERVICE_TEST_BARRIER_POINT="launcher-after-fork",
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    command = [
        str(PYTHON), "-I", "-B", str(HELPER), "start",
        str(ctx["record"]), str(ctx["lock"]), str(ctx["socket"]),
        ctx["service"], str(ctx["log"]), "3", ctx["kind"], ctx["url"],
        ctx["protocol"], ctx["digest"], str(ctx["manifest"]),
        str(ctx["manifest_lock"]), ctx["entry"], ctx["generation"], "--",
        str(PYTHON), str(ctx["fake"]),
    ]
    launcher = subprocess.Popen(command, env=env)
    assert _wait(lambda: Path(str(barrier) + ".reached").exists())
    launcher.kill()
    launcher.wait(timeout=3)
    assert launcher.returncode == -signal.SIGKILL
    assert _wait(ctx["record"].exists)
    assert _wait(lambda: _helper("status", ctx["record"]).returncode == 0)
    try:
        assert _stop(ctx).returncode == 0
    finally:
        _cleanup(ctx)


def test_forged_pid_pgid_record_never_signals_unrelated_process(tmp_path):
    run = tmp_path / "run"
    logs = tmp_path / "logs"
    assert _helper("prepare", run, logs).returncode == 0
    unrelated = subprocess.Popen(["sleep", "60"], start_new_session=True)
    record = run / "verify.pid"
    record.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "authority": "unix_guardian",
                "guardian_pid": unrelated.pid,
                "service": "verify",
                "argv_sha256": "b" * 64,
                "socket_path": str(run / "verify.sock"),
                "control_nonce": "c" * 32,
                "instance_nonce": "d" * 32,
                "health_kind": "verify",
                "health_url": "http://127.0.0.1:9/health",
                "expected_protocol": 3,
                "expected_digest": "e" * 64,
                "intent_entry": "verify",
                "intent_generation": "f" * 32,
                "created_ns": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(record, 0o600)
    try:
        stopped = _helper("stop", record, run / "verify.lock", "1", "f" * 32)
        assert stopped.returncode != 0
        assert _running(unrelated.pid)
        assert record.exists()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_hardlinked_record_is_unsafe_and_never_deleted(tmp_path):
    ctx = _context(tmp_path)
    ctx["record"].write_text("{}\n", encoding="utf-8")
    os.chmod(ctx["record"], 0o600)
    alias = tmp_path / "record-alias"
    os.link(ctx["record"], alias)
    status_result = _helper("status", ctx["record"])
    reconcile = _helper("reconcile", ctx["record"], ctx["lock"])
    assert status_result.returncode != 0 and reconcile.returncode != 0
    assert ctx["record"].exists() and alias.exists()


def test_record_registration_is_atomic_no_clobber_without_hardlink_state(tmp_path):
    ctx = _context(tmp_path)
    barrier = tmp_path / "record-noclobber"
    env = ctx["env"].copy()
    env.update(
        DANUS_SERVICE_TEST_BARRIER_POINT="atomic-before-no-clobber-rename",
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    command = [
        str(PYTHON), "-I", "-B", str(HELPER), "start",
        str(ctx["record"]), str(ctx["lock"]), str(ctx["socket"]),
        ctx["service"], str(ctx["log"]), "3", ctx["kind"], ctx["url"],
        ctx["protocol"], ctx["digest"], str(ctx["manifest"]),
        str(ctx["manifest_lock"]), ctx["entry"], ctx["generation"], "--",
        str(PYTHON), str(ctx["fake"]),
    ]
    launcher = subprocess.Popen(
        command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    reached = Path(str(barrier) + ".reached")
    release = Path(str(barrier) + ".release")
    try:
        assert _wait(reached.exists)
        attacker = b'{"attacker":true}\n'
        ctx["record"].write_bytes(attacker)
        os.chmod(ctx["record"], 0o600)
    finally:
        release.touch()
    out, err = launcher.communicate(timeout=8)
    assert launcher.returncode != 0, out + err
    assert ctx["record"].read_bytes() == attacker
    assert ctx["record"].stat().st_nlink == 1
    assert _wait(lambda: not ctx["socket"].exists())


@pytest.mark.parametrize(
    "action,cut,expected_present",
    [
        ("add", "after-file-fsync", False),
        ("add", "after-rename", True),
        ("add", "after-dir-fsync", True),
        ("del", "after-file-fsync", True),
        ("del", "after-rename", False),
        ("del", "after-dir-fsync", False),
    ],
)
def test_up_down_intent_each_fsync_crash_cut_is_monotone(
    tmp_path, action, cut, expected_present
):
    run = tmp_path / "run"
    logs = tmp_path / "logs"
    assert _helper("prepare", run, logs).returncode == 0
    manifest, lock = run / "autostart", run / "autostart.lock"
    if action == "del":
        assert _helper("manifest", "add", manifest, lock, "verify").returncode == 0
    env = os.environ.copy()
    env["DANUS_SERVICE_FAULT_POINT"] = cut
    result = _helper("manifest", action, manifest, lock, "verify", env=env)
    assert result.returncode == 97
    snapshot = _helper("manifest", "snapshot", manifest, lock)
    assert snapshot.returncode == 0
    entries = {item["entry"] for item in json.loads(snapshot.stdout)}
    assert ("verify" in entries) is expected_present


def test_manifest_has_is_typed_and_unsafe_or_timeout_is_nonzero(tmp_path):
    run = tmp_path / "run"
    logs = tmp_path / "logs"
    assert _helper("prepare", run, logs).returncode == 0
    manifest, lock = run / "autostart", run / "autostart.lock"
    assert json.loads(_helper("manifest", "has", manifest, lock, "verify").stdout) == {
        "generation": None,
        "state": "absent",
    }
    added = json.loads(_helper("manifest", "add", manifest, lock, "verify").stdout)
    present = json.loads(
        _helper("manifest", "has", manifest, lock, "verify", added["generation"]).stdout
    )
    assert present["state"] == "present"

    lock_fd = os.open(lock, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        env = os.environ.copy()
        env["DANUS_MANIFEST_LOCK_TIMEOUT_SECONDS"] = "0.05"
        timed = _helper("manifest", "has", manifest, lock, "verify", env=env)
        assert timed.returncode != 0 and "timed out" in timed.stderr
    finally:
        os.close(lock_fd)

    unsafe = tmp_path / "unsafe-manifest"
    os.symlink(manifest, unsafe)
    result = _helper("manifest", "has", unsafe, lock, "verify")
    assert result.returncode != 0


def _services_harness(tmp_path: Path):
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "services.sh", scripts / "services.sh")
    shutil.copy2(HELPER, scripts / "service-identity.py")
    fake = _fake_service(root / "fake_service.py")
    (scripts / "start-dashboard.sh").write_text(
        '#!/usr/bin/env bash\nexec "$DANUS_PY" "$DANUS_TEST_SERVICE"\n', encoding="utf-8"
    )
    (scripts / "start-verify.sh").write_text(
        '#!/usr/bin/env bash\nexec "$DANUS_PY" "$DANUS_TEST_SERVICE"\n', encoding="utf-8"
    )
    (root / "danus" / "verify").mkdir(parents=True)
    (root / "danus" / "__init__.py").write_text("", encoding="utf-8")
    (root / "danus" / "core.py").write_text(
        "VERIFICATION_OUTPUT_PROTOCOL_VERSION=3\n", encoding="utf-8"
    )
    (root / "danus" / "verify" / "__init__.py").write_text("", encoding="utf-8")
    (root / "danus" / "verify" / "launcher.py").write_text(
        f"VERIFIER_BUNDLE_DIGEST={TEST_DIGEST!r}\n", encoding="utf-8"
    )
    runtime = Path(tempfile.mkdtemp(prefix="s-", dir=_SHORT_ROOT))
    projects = tmp_path / "projects"
    (projects / "Project").mkdir(parents=True)
    port = _free_port()
    (scripts / "env.sh").write_text(
        f'export DANUS_ROOT={str(root)!r}\n'
        'export DANUS_RUNTIME="${DANUS_TEST_RUNTIME:?}"\n'
        'export DANUS_AGENTS_ROOT="${DANUS_TEST_PROJECTS:?}"\n'
        'export DANUS_PY="${DANUS_TEST_PY:?}"\n'
        'export VERIFY_PORT="${DANUS_TEST_PORT:?}"\n'
        'export DASHBOARD_PORT="${DANUS_TEST_PORT:?}"\n'
        'danus_verify_health(){ local out state; '
        'out="$("$DANUS_PY" -I -B "$DANUS_ROOT/scripts/service-identity.py" verify-health '
        '"$DANUS_RUNTIME/run/verify.pid" "http://127.0.0.1:$VERIFY_PORT/health" 2>/dev/null)"; '
        'state="$(printf "%s" "$out" | "$DANUS_PY" -I -c '
        "'import json,sys; print(json.load(sys.stdin).get(\"state\",\"unsafe\"))' "
        '2>/dev/null || echo unsafe)"; echo "$state"; [ "$state" = ours ]; }\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        DANUS_TEST_RUNTIME=str(runtime),
        DANUS_TEST_PROJECTS=str(projects),
        DANUS_TEST_PY=str(PYTHON),
        DANUS_TEST_PORT=str(port),
        DANUS_TEST_SERVICE=str(fake),
        DANUS_TEST_MODE="healthy",
        DANUS_TEST_HEALTH_KIND="dashboard",
        DANUS_SERVICE_START_TIMEOUT="3",
        DANUS_SERVICE_STOP_TIMEOUT="5",
    )
    prepared = _run(
        PYTHON,
        "-I",
        "-B",
        scripts / "service-identity.py",
        "prepare",
        runtime / "run",
        runtime / "logs",
        env=env,
    )
    assert prepared.returncode == 0, prepared.stderr
    return scripts / "services.sh", env, runtime, projects


def _manifest_state(services: Path, env: dict[str, str], entry: str) -> str:
    run = Path(env["DANUS_TEST_RUNTIME"])
    result = _run(
        PYTHON,
        "-I",
        "-B",
        services.parent / "service-identity.py",
        "manifest",
        "has",
        run / "run" / "autostart",
        run / "run" / "autostart.lock",
        entry,
        env=env,
    )
    return json.loads(result.stdout)["state"]


def test_explicit_up_add_down_start_interleave_cannot_resurrect(tmp_path):
    services, env, runtime, _ = _services_harness(tmp_path)
    barrier = tmp_path / "barrier"
    up_env = env.copy()
    up_env.update(
        DANUS_SERVICE_TEST_BARRIER_POINT="guardian-after-lock",
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    up = subprocess.Popen(
        ["bash", str(services), "up", "dashboard", "Project"],
        env=up_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait(lambda: Path(str(barrier) + ".reached").exists())
    down = subprocess.Popen(
        ["bash", str(services), "down", "dashboard"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait(lambda: _manifest_state(services, env, "dashboard Project") == "absent")
    Path(str(barrier) + ".release").touch()
    up_out, up_err = up.communicate(timeout=10)
    down_out, down_err = down.communicate(timeout=10)
    assert up.returncode == down.returncode == 0, up_out + up_err + down_out + down_err
    assert not (runtime / "run" / "dashboard-Project.pid").exists()


@pytest.mark.parametrize(
    "barrier_point", ["guardian-after-intent-check", "guardian-after-exec"]
)
def test_down_after_initial_intent_check_cannot_commit_startup(tmp_path, barrier_point):
    services, env, runtime, _ = _services_harness(tmp_path)
    barrier = tmp_path / barrier_point
    up_env = env.copy()
    up_env.update(
        DANUS_SERVICE_TEST_BARRIER_POINT=barrier_point,
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    up = subprocess.Popen(
        ["bash", str(services), "up", "dashboard", "Project"],
        env=up_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait(lambda: Path(str(barrier) + ".reached").exists())
    down = subprocess.Popen(
        ["bash", str(services), "down", "dashboard"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait(lambda: _manifest_state(services, env, "dashboard Project") == "absent")
    Path(str(barrier) + ".release").touch()
    up.communicate(timeout=10)
    down.communicate(timeout=10)
    assert _wait(lambda: not (runtime / "run" / "dashboard-Project.pid").exists())
    child_marker = Path(env.get("DANUS_TEST_MARKER", ""))
    if child_marker.is_file():
        assert _wait(lambda: not _running(int(child_marker.read_text())))
    assert _manifest_state(services, env, "dashboard Project") == "absent"


def test_recover_snapshot_then_down_wins_generation_recheck(tmp_path):
    services, env, runtime, _ = _services_harness(tmp_path)
    helper = services.parent / "service-identity.py"
    add = _run(
        PYTHON, "-I", "-B", helper, "manifest", "add",
        runtime / "run" / "autostart", runtime / "run" / "autostart.lock",
        "dashboard Project", env=env,
    )
    generation = json.loads(add.stdout)["generation"]
    barrier = tmp_path / "recover-barrier"
    recover_env = env.copy()
    recover_env.update(
        DANUS_SERVICE_TEST_BARRIER_POINT="guardian-after-lock",
        DANUS_SERVICE_TEST_BARRIER_PATH=str(barrier),
    )
    recover = subprocess.Popen(
        ["bash", str(services), "recover-up", "dashboard Project", generation],
        env=recover_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait(lambda: Path(str(barrier) + ".reached").exists())
    down = subprocess.Popen(
        ["bash", str(services), "down", "dashboard"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait(lambda: _manifest_state(services, env, "dashboard Project") == "absent")
    Path(str(barrier) + ".release").touch()
    recover.communicate(timeout=10)
    down.communicate(timeout=10)
    assert recover.returncode == down.returncode == 0
    assert not (runtime / "run" / "dashboard-Project.pid").exists()


def test_deleted_intent_self_stops_ready_guardian_even_if_down_caller_crashes(tmp_path):
    services, env, runtime, _ = _services_harness(tmp_path)
    up = _run("bash", services, "up", "dashboard", "Project", env=env)
    assert up.returncode == 0, up.stdout + up.stderr
    record = runtime / "run" / "dashboard-Project.pid"
    status_before = json.loads(
        _run(
            PYTHON, "-I", "-B", services.parent / "service-identity.py",
            "status", record, env=env,
        ).stdout
    )
    child_pid = status_before["child_pid"]
    deleted = _run(
        PYTHON, "-I", "-B", services.parent / "service-identity.py",
        "manifest", "del", runtime / "run" / "autostart",
        runtime / "run" / "autostart.lock", "dashboard Project", env=env,
    )
    assert json.loads(deleted.stdout)["state"] == "deleted"
    # This deliberately omits the control stop, modelling down's caller dying
    # immediately after the durable delete.
    assert _wait(lambda: not record.exists(), 5)
    assert _wait(lambda: not _running(child_pid), 5)


def test_replaced_generation_yields_old_guardian_and_busy_up_starts_new_one(tmp_path):
    services, env, runtime, _ = _services_harness(tmp_path)
    helper = services.parent / "service-identity.py"
    assert _run("bash", services, "up", "dashboard", "Project", env=env).returncode == 0
    record = runtime / "run" / "dashboard-Project.pid"
    old_status = json.loads(_run(PYTHON, "-I", "-B", helper, "status", record, env=env).stdout)
    old_pid = old_status["child_pid"]
    old_generation = old_status["intent_generation"]

    assert _run(
        PYTHON, "-I", "-B", helper, "manifest", "del",
        runtime / "run" / "autostart", runtime / "run" / "autostart.lock",
        "dashboard Project", env=env,
    ).returncode == 0
    added = _run(
        PYTHON, "-I", "-B", helper, "manifest", "add",
        runtime / "run" / "autostart", runtime / "run" / "autostart.lock",
        "dashboard Project", env=env,
    )
    new_generation = json.loads(added.stdout)["generation"]
    assert new_generation != old_generation

    up = _run("bash", services, "up", "dashboard", "Project", env=env, timeout=15)
    assert up.returncode == 0, up.stdout + up.stderr
    new_status = json.loads(_run(PYTHON, "-I", "-B", helper, "status", record, env=env).stdout)
    assert new_status["state"] == "ready"
    assert new_status["intent_generation"] == new_generation
    assert new_status["child_pid"] != old_pid
    assert _wait(lambda: not _running(old_pid))

    # A delayed stop belonging to gen1 may never stop the newer gen2 guardian.
    stale_stop = _run(
        PYTHON, "-I", "-B", helper, "stop", record,
        runtime / "run" / "dashboard-Project.lock", "2", old_generation, env=env,
    )
    assert stale_stop.returncode == 0
    assert json.loads(stale_stop.stdout)["state"] == "superseded"
    assert json.loads(_run(PYTHON, "-I", "-B", helper, "status", record, env=env).stdout)[
        "intent_generation"
    ] == new_generation
    assert _run("bash", services, "down", "dashboard", env=env).returncode == 0


def test_bulk_down_removes_manifest_only_entries(tmp_path):
    services, env, runtime, _ = _services_harness(tmp_path)
    helper = services.parent / "service-identity.py"
    for entry in ("verify", "dashboard Project"):
        result = _run(
            PYTHON, "-I", "-B", helper, "manifest", "add",
            runtime / "run" / "autostart", runtime / "run" / "autostart.lock",
            entry, env=env,
        )
        assert result.returncode == 0
    down = _run("bash", services, "down", "all", env=env)
    assert down.returncode == 0, down.stdout + down.stderr
    snapshot = _run("bash", services, "manifest-snapshot", env=env)
    assert snapshot.returncode == 0 and not snapshot.stdout.strip()


def test_bulk_down_linearizes_record_and_manifest_only_services_once(tmp_path):
    services, env, runtime, _ = _services_harness(tmp_path)
    helper = services.parent / "service-identity.py"
    real_helper = services.parent / "service-identity-real.py"
    helper.replace(real_helper)
    trace = tmp_path / "bulk-calls.jsonl"
    helper.write_text(
        "import json, os, sys\n"
        "args=sys.argv[1:]\n"
        "if (args[:2] == ['manifest','del']) or args[:1] == ['stop']:\n"
        "  with open(os.environ['DANUS_TEST_BULK_TRACE'],'a',encoding='utf-8') as f:\n"
        "    f.write(json.dumps(args) + '\\n')\n"
        f"os.execv({str(PYTHON)!r}, [{str(PYTHON)!r}, '-I', '-B', "
        f"{str(real_helper)!r}, *args])\n",
        encoding="utf-8",
    )
    env = {**env, "DANUS_TEST_BULK_TRACE": str(trace)}
    assert _run("bash", services, "up", "dashboard", "Project", env=env).returncode == 0
    assert _run(
        PYTHON, "-I", "-B", real_helper, "manifest", "add",
        runtime / "run" / "autostart", runtime / "run" / "autostart.lock",
        "verify", env=env,
    ).returncode == 0
    down = _run("bash", services, "down", "all", env=env, timeout=15)
    assert down.returncode == 0, down.stdout + down.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    deletes = [call[4] for call in calls if call[:2] == ["manifest", "del"]]
    stops = [Path(call[1]).stem for call in calls if call[:1] == ["stop"]]
    assert deletes.count("dashboard Project") == 1
    assert deletes.count("verify") == 1
    assert stops.count("dashboard-Project") == 1
    assert stops.count("verify") == 1


@pytest.mark.parametrize("foreign_mode", ["healthy", "huge", "status500", "malformed"])
def test_services_test_down_and_foreign_are_nonzero(tmp_path, foreign_mode):
    services, env, _runtime, _ = _services_harness(tmp_path)
    down = _run("bash", services, "test", env=env)
    assert down.returncode != 0 and "down" in (down.stdout + down.stderr)

    foreign = subprocess.Popen(
        [str(PYTHON), env["DANUS_TEST_SERVICE"]],
        env={
            **env,
            "DANUS_SERVICE_INSTANCE_NONCE": "f" * 32,
            "DANUS_TEST_MODE": foreign_mode,
        },
    )
    try:
        assert _wait(lambda: _run("bash", services, "test", env=env).returncode == 3)
    finally:
        foreign.terminate()
        foreign.wait(timeout=3)


def test_log_path_symlink_fifo_hardlink_and_follow_are_rejected(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("secret\n", encoding="utf-8")
    os.chmod(log, 0o600)
    assert _helper("read-log", log).returncode == 0
    assert _helper("read-log", log, "--follow").returncode != 0
    hard = tmp_path / "hard.log"
    os.link(log, hard)
    assert _helper("read-log", log).returncode != 0
    link = tmp_path / "link.log"
    os.symlink(hard, link)
    assert _helper("read-log", link).returncode != 0
    fifo = tmp_path / "fifo.log"
    os.mkfifo(fifo)
    started = time.monotonic()
    assert _helper("read-log", fifo, timeout=2).returncode != 0
    assert time.monotonic() - started < 1


def test_service_paths_and_dashboard_project_symlink_are_rejected(tmp_path):
    services, env, _runtime, projects = _services_harness(tmp_path)
    for name in ("../escape", "bad/name", "x\nverify"):
        result = _run("bash", services, "up", "dashboard", name, env=env)
        assert result.returncode != 0
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    os.symlink(sentinel, projects / "Alias")
    start_dashboard = services.parent / "start-dashboard-real.sh"
    shutil.copy2(ROOT / "scripts" / "start-dashboard.sh", start_dashboard)
    # It locates service-identity.py beside itself and rejects the final symlink
    # before importing or binding the dashboard.
    result = _run("bash", start_dashboard, "Alias", env=env)
    assert result.returncode != 0 and "symlink" in result.stderr


@pytest.mark.parametrize("timeout_value", ["inf", "nan", "61", "-1"])
def test_stop_timeout_is_finite_and_bounded(tmp_path, timeout_value):
    ctx = _context(tmp_path)
    result = _helper(
        "stop", ctx["record"], ctx["lock"], timeout_value, ctx["generation"]
    )
    assert result.returncode != 0 and "timeout" in result.stderr


def test_service_entrypoint_is_executable_and_broken_record_symlink_is_visible(tmp_path):
    assert os.access(ROOT / "scripts" / "services.sh", os.X_OK)
    services, env, runtime, _ = _services_harness(tmp_path)
    broken = runtime / "run" / "verify.pid"
    os.symlink(runtime / "run" / "does-not-exist", broken)
    status_result = _run("bash", services, "status", env=env)
    assert status_result.returncode != 0
    assert "unsafe" in status_result.stdout


def test_recover_aggregates_failures_and_runs_final_health_gate(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts" / "recover.sh", scripts / "recover.sh")
    calls = tmp_path / "calls"
    (scripts / "env.sh").write_text(
        f'DANUS_ROOT={str(tmp_path)!r}\nDANUS_RUNTIME={str(tmp_path / "runtime")!r}\n'
        'CODEX_BACKEND=api\n',
        encoding="utf-8",
    )
    (scripts / "bootstrap.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (scripts / "check-codex.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (scripts / "services.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {str(calls)!r}\n"
        "case \"$1\" in\n"
        " recover-stale) exit 1;;\n"
        " manifest-snapshot) printf 'verify|11111111111111111111111111111111\\n"
        "dashboard Project|22222222222222222222222222222222\\n';;\n"
        " recover-up) [ \"$2\" != verify ];;\n"
        " status) exit 0;;\n"
        " test) exit 5;;\n"
        "esac\n",
        encoding="utf-8",
    )
    result = _run("bash", scripts / "recover.sh")
    assert result.returncode != 0
    observed = calls.read_text(encoding="utf-8")
    assert "recover-up verify" in observed
    assert "recover-up dashboard Project" in observed
    assert "status" in observed and "test" in observed
