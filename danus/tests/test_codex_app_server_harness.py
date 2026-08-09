"""Offline contract tests for :mod:`danus.hotjoin`'s app-server JSONL pump.

Every subprocess in this module is ``fake_codex_app_server.py``.  The tests do
not resolve or execute Codex and cannot make a model/API request.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import danus.hotjoin as hotjoin_module

from danus.hotjoin import (
    AppServerClient,
    AppServerClosed,
    HotJoinError,
    HotJoinStore,
    ProtocolError,
    RpcError,
)

from .fake_codex_app_server import THREAD_ID, TURN_ID


FAKE = Path(__file__).resolve().parent / "fake_codex_app_server.py"


def _notifications(method: str) -> Callable[[dict[str, Any]], bool]:
    return lambda message: message.get("method") == method


def _turn_notification(method: str, turn_id: str) -> Callable[[dict[str, Any]], bool]:
    def matches(message: dict[str, Any]) -> bool:
        if message.get("method") != method:
            return False
        params = message.get("params") or {}
        turn = params.get("turn") or {}
        return turn.get("id") == turn_id

    return matches


def _trace_messages(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def _pid_running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


@pytest.fixture
def make_client(tmp_path: Path):
    clients: list[AppServerClient] = []
    traces: list[Path] = []

    def make(scenario: str) -> tuple[AppServerClient, Path]:
        trace = tmp_path / f"trace-{len(traces)}-{scenario}.jsonl"
        env = os.environ.copy()
        env.update(
            {
                "DANUS_FAKE_APP_SERVER_SCENARIO": scenario,
                "DANUS_FAKE_APP_SERVER_TRACE": str(trace),
                "PYTHONUNBUFFERED": "1",
            }
        )
        client = AppServerClient(
            argv=[sys.executable, str(FAKE), "--scenario", scenario],
            cwd=tmp_path,
            env=env,
        )
        client.start()
        client.initialize(timeout=1)
        clients.append(client)
        traces.append(trace)
        return client, trace

    yield make

    for client in reversed(clients):
        client.close()


def _start_thread(client: AppServerClient) -> dict[str, Any]:
    result = client.rpc("thread/start", {"ephemeral": True}, timeout=2)
    assert result["thread"]["id"] == THREAD_ID
    return result


def test_app_server_close_removes_owned_child_process_group(tmp_path: Path):
    marker = tmp_path / "app-group"
    script = tmp_path / "app-with-grandchild.py"
    script.write_text(
        "import os, pathlib, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    client = AppServerClient([sys.executable, str(script)], cwd=tmp_path)
    client.start()
    _wait_until(marker.exists)
    leader, leader_group, grandchild, grandchild_group = map(
        int, marker.read_text(encoding="utf-8").split()
    )
    assert leader_group != leader
    assert grandchild_group == leader_group
    assert leader_group != os.getpgrp()
    client.close(grace=0.1)
    _wait_until(lambda: not _pid_running(leader) and not _pid_running(grandchild))


def test_app_server_owner_sigkill_kills_server_and_grandchild(tmp_path: Path):
    marker = tmp_path / "app-owner-death"
    script = tmp_path / "app-owner-death.py"
    script.write_text(
        "import os, pathlib, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    owner_code = (
        "from pathlib import Path\n"
        "import sys, time\n"
        "from danus.hotjoin import AppServerClient\n"
        "client=AppServerClient([sys.executable, sys.argv[1]], cwd=Path(sys.argv[2]))\n"
        "client.start()\n"
        "time.sleep(120)\n"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(script), str(tmp_path)]
    )
    try:
        _wait_until(marker.exists)
        leader, group, grandchild, grandchild_group = map(
            int, marker.read_text(encoding="utf-8").split()
        )
        assert group != leader
        assert grandchild_group == group
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=5)
        _wait_until(
            lambda: not _pid_running(leader)
            and not _pid_running(grandchild)
            and not _pid_running(group),
            timeout=8,
        )
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()


def test_app_server_post_spawn_reader_failure_cleans_paid_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    marker = tmp_path / "app-reader-failure"
    script = tmp_path / "app-reader-failure.py"
    script.write_text(
        "import os, pathlib, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}')\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    client = AppServerClient([sys.executable, str(script)], cwd=tmp_path)

    def fail_reader_setup(*_args: object, **_kwargs: object) -> object:
        _wait_until(marker.exists)
        raise RuntimeError("injected reader setup failure")

    monkeypatch.setattr(hotjoin_module.threading, "Thread", fail_reader_setup)
    with pytest.raises(RuntimeError, match="injected reader setup failure"):
        client.start()
    leader, group, grandchild, grandchild_group = map(
        int, marker.read_text(encoding="utf-8").split()
    )
    assert group == grandchild_group
    _wait_until(
        lambda: not _pid_running(leader)
        and not _pid_running(grandchild)
        and not _pid_running(group),
        timeout=8,
    )
    assert client.process is None


def _start_turn(client: AppServerClient) -> dict[str, Any]:
    result = client.rpc(
        "turn/start",
        {
            "threadId": THREAD_ID,
            "input": [{"type": "text", "text": "offline fixture start"}],
        },
        timeout=2,
    )
    assert result["turn"]["id"] == TURN_ID
    assert result["turn"]["status"] == "inProgress"
    return result


def _steer(client: AppServerClient, expected_turn_id: str = TURN_ID) -> dict[str, Any]:
    return client.rpc(
        "turn/steer",
        {
            "threadId": THREAD_ID,
            "expectedTurnId": expected_turn_id,
            "input": [{"type": "text", "text": "nonce-offline-1"}],
        },
        timeout=2,
    )


def test_notifications_before_response_are_buffered_and_usage_is_not_lost(make_client):
    """One stdout reader must demux notifications that precede RPC replies."""
    client, _trace = make_client("notification-before-response")

    _start_thread(client)
    thread_started = client.wait_for(_notifications("thread/started"), timeout=1)
    assert thread_started["params"]["thread"]["id"] == THREAD_ID

    _start_turn(client)
    turn_started = client.wait_for(
        _turn_notification("turn/started", TURN_ID), timeout=1
    )
    assert turn_started["params"]["turn"]["status"] == "inProgress"

    initial_usage = client.wait_for(
        lambda message: message.get("method") == "thread/tokenUsage/updated"
        and message.get("params", {})
        .get("tokenUsage", {})
        .get("total", {})
        .get("totalTokens")
        == 11,
        timeout=1,
    )
    assert initial_usage["params"]["turnId"] == TURN_ID

    assert _steer(client) == {"turnId": TURN_ID}
    final_usage = client.wait_for(
        lambda message: message.get("method") == "thread/tokenUsage/updated"
        and message.get("params", {})
        .get("tokenUsage", {})
        .get("total", {})
        .get("totalTokens")
        == 29,
        timeout=1,
    )
    assert final_usage["params"]["tokenUsage"]["last"]["totalTokens"] == 18

    completed = client.wait_for(
        _turn_notification("turn/completed", TURN_ID), timeout=1
    )
    assert completed["params"]["turn"]["status"] == "completed"


def test_model_reroute_tracking_is_exact_bounded_and_fail_closed(tmp_path: Path):
    client = AppServerClient([sys.executable], cwd=tmp_path)
    huge_model = "model-" + "x" * 3000
    for index in range(10):
        client._dispatch(
            {
                "method": "model/rerouted",
                "params": {
                    "fromModel": huge_model if index == 0 else f"from-{index}",
                    "reason": "highRiskCyberActivity",
                    "threadId": THREAD_ID,
                    "toModel": f"to-{index}",
                    "turnId": TURN_ID,
                },
            }
        )

    snapshot = client.model_reroutes(THREAD_ID, TURN_ID)
    assert snapshot["observed"] is True
    assert len(snapshot["events"]) == 8
    assert snapshot["events"][0]["fromModel"] == {
        "omitted": True,
        "bytes": len(huge_model.encode()),
        "sha256": hashlib.sha256(huge_model.encode()).hexdigest(),
    }
    assert snapshot["omitted"]["count"] == 2
    assert snapshot["omitted"]["bytes"] > 0
    assert snapshot["omitted"]["sha256"] != hashlib.sha256().hexdigest()
    assert client.model_reroutes(THREAD_ID, "turn-other")["observed"] is False
    assert len(json.dumps(snapshot).encode()) < 64 * 1024

    with pytest.raises(ProtocolError, match="unknown reason"):
        client._dispatch(
            {
                "method": "model/rerouted",
                "params": {
                    "fromModel": "from",
                    "reason": "unrecognized-reason",
                    "threadId": THREAD_ID,
                    "toModel": "to",
                    "turnId": TURN_ID,
                },
            }
        )


def test_stale_expected_turn_and_late_old_terminal_do_not_cross_talk(make_client):
    client, trace = make_client("stale-turn")
    _start_thread(client)
    _start_turn(client)

    # The fake emits this old terminal after the new turn/started notification.
    old = client.wait_for(
        _turn_notification("turn/completed", "turn-old-0"), timeout=1
    )
    assert old["params"]["turn"]["status"] == "completed"

    with pytest.raises(RpcError, match="expected turn"):
        _steer(client, expected_turn_id="turn-stale-client")

    # A waiter scoped to the current turn must not be satisfied by the old event.
    with pytest.raises(TimeoutError):
        client.wait_for(_turn_notification("turn/completed", TURN_ID), timeout=0.05)

    client.rpc(
        "turn/interrupt", {"threadId": THREAD_ID, "turnId": TURN_ID}, timeout=1
    )
    terminal = client.wait_for(
        _turn_notification("turn/completed", TURN_ID), timeout=1
    )
    assert terminal["params"]["turn"]["status"] == "interrupted"

    requests = _trace_messages(trace)
    assert sum(m.get("method") == "turn/start" for m in requests) == 1
    assert sum(m.get("method") == "turn/steer" for m in requests) == 1


def test_accepted_steer_survives_immediate_server_crash_but_terminal_wait_fails(
    make_client,
):
    client, _trace = make_client("accepted-then-crash")
    _start_thread(client)
    _start_turn(client)

    # The result line is flushed before the fake exits.
    assert _steer(client) == {"turnId": TURN_ID}
    with pytest.raises(AppServerClosed):
        client.wait_for(_turn_notification("turn/completed", TURN_ID), timeout=2)

    # close() must be harmless after an already-observed child exit.
    client.close()
    client.close()


def test_eof_wakes_pending_rpc_after_applied_steer_without_ack(make_client):
    client, trace = make_client("applied-then-crash-before-response")
    _start_thread(client)
    _start_turn(client)

    started = time.monotonic()
    with pytest.raises(AppServerClosed):
        # The timeout is deliberately long: EOF, not the deadline, must wake it.
        _steer(client)
    assert time.monotonic() - started < 1.5

    _wait_until(
        lambda: any(m.get("_fake") == "steer_applied" for m in _trace_messages(trace))
    )
    messages = _trace_messages(trace)
    assert sum(m.get("method") == "turn/steer" for m in messages) == 1
    # No blind resend is safe here; the caller must persist delivery_unknown.


def test_concurrent_rpcs_are_routed_by_id_when_responses_arrive_out_of_order(
    make_client,
):
    client, _trace = make_client("out-of-order-responses")
    _start_thread(client)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        delayed = pool.submit(
            client.rpc,
            "thread/read",
            {"threadId": THREAD_ID, "includeTurns": False},
            2,
        )
        held = client.wait_for(
            lambda message: message.get("method") == "warning"
            and message.get("params", {}).get("message")
            == "fake-held-thread-read-response",
            timeout=1,
        )
        assert held["params"]["threadId"] == THREAD_ID

        # The fake answers this newer call first, then the older thread/read.
        loaded = client.rpc("thread/loaded/list", {}, timeout=2)
        read = delayed.result(timeout=2)

    assert loaded == {"data": [THREAD_ID], "nextCursor": None}
    assert read["thread"]["id"] == THREAD_ID


def test_large_stderr_is_drained_without_blocking_stdout_protocol(make_client):
    client, _trace = make_client("stderr-flood")
    _start_thread(client)
    _start_turn(client)  # fake writes 256 KiB to stderr before this response

    client.rpc(
        "turn/interrupt", {"threadId": THREAD_ID, "turnId": TURN_ID}, timeout=1
    )
    terminal = client.wait_for(
        _turn_notification("turn/completed", TURN_ID), timeout=1
    )
    assert terminal["params"]["turn"]["status"] == "interrupted"


def test_timeout_sends_exactly_one_interrupt_and_reaches_terminal(make_client):
    client, trace = make_client("timeout-interrupt")
    _start_thread(client)
    _start_turn(client)
    assert _steer(client) == {"turnId": TURN_ID}

    with pytest.raises(TimeoutError):
        client.wait_for(_turn_notification("turn/completed", TURN_ID), timeout=0.05)

    assert client.rpc(
        "turn/interrupt", {"threadId": THREAD_ID, "turnId": TURN_ID}, timeout=1
    ) == {}
    terminal = client.wait_for(
        _turn_notification("turn/completed", TURN_ID), timeout=1
    )
    assert terminal["params"]["turn"]["status"] == "interrupted"

    requests = _trace_messages(trace)
    assert sum(m.get("method") == "turn/interrupt" for m in requests) == 1


def test_close_escalates_to_kill_and_reaps_uncooperative_child(make_client):
    client, trace = make_client("cleanup-ignore-sigterm")
    _wait_until(lambda: bool(_trace_messages(trace)))
    started_record = _trace_messages(trace)[0]
    assert started_record["_fake"] == "started"
    pid = started_record["pid"]

    client.close()
    client.close()  # cleanup is idempotent

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_close_allows_eof_flush_before_signal_escalation(make_client):
    client, trace = make_client("graceful-eof")
    _wait_until(lambda: bool(_trace_messages(trace)))
    client.close(grace=0.5)
    assert any(
        record.get("_fake") == "graceful_eof_flushed"
        for record in _trace_messages(trace)
    )


def test_worker_round_auto_completes_with_mapping_and_sanitized_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Exercise the production worker adapter without resolving real Codex."""
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(
        tmp_path / "project" / "workers" / "max"
    )
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    audit_path = worker.dir / "round-app-server.jsonl"
    trace_path = tmp_path / "worker-auto-complete-trace.jsonl"
    user_sentinel = "DO-NOT-LEAK-USER-MESSAGE-7f4d1b"

    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": "auto-complete",
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    preflight_calls: list[tuple[str, dict[str, str]]] = []
    argv_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(
        worker_loop.codex, "subprocess_env", lambda _codex_bin: dict(fake_env)
    )
    monkeypatch.setattr(
        worker_loop,
        "preflight_app_server",
        lambda codex_bin, *, env: preflight_calls.append((codex_bin, dict(env))),
    )

    def fake_argv(codex_bin: str, mcp_config: str) -> list[str]:
        argv_calls.append((codex_bin, mcp_config))
        return [sys.executable, str(FAKE), "--scenario", "auto-complete"]

    monkeypatch.setattr(worker_loop, "app_server_argv", fake_argv)

    rc = worker_loop.run_round_app_server(
        worker,
        {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
        user_sentinel,
        audit_path,
        hard_timeout=2,
    )

    assert rc == 0
    assert worker_loop._Child.proc is None
    assert len(preflight_calls) == 1
    assert preflight_calls[0][0] == sys.executable
    assert len(argv_calls) == 1
    assert argv_calls[0][0] == sys.executable

    # The newly created persistent conversation is mapped back to this worker.
    assert HotJoinStore(worker.project_dir).thread_id(worker.name) == THREAD_ID

    audit_text = audit_path.read_text(encoding="utf-8")
    audit = [json.loads(line) for line in audit_text.splitlines()]
    assert audit[0] == {
        "event": "turn_completed",
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "terminal_observed": True,
        "status": "completed",
        "duration_ms": 10,
        "token_usage": {
            "last": {
                "inputTokens": 12,
                "cachedInputTokens": 3,
                "cacheWriteInputTokens": 0,
                "outputTokens": 5,
                "reasoningOutputTokens": 2,
                "totalTokens": 17,
            },
            "total": {
                "inputTokens": 12,
                "cachedInputTokens": 3,
                "cacheWriteInputTokens": 0,
                "outputTokens": 5,
                "reasoningOutputTokens": 2,
                "totalTokens": 17,
            },
            "modelContextWindow": 100_000,
        },
        "token_usage_observed": True,
        "token_usage_finality": "observed_not_schema_attested_final",
        "post_terminal_settle_bound_ms": 250,
        "requested_model": "offline-model",
        "requested_effort": "low",
        "actual_model": "offline-model",
        "thread_reasoning_effort": "low",
        "model_rerouted": False,
        "model_reroute_observation": "not_observed_live_stream",
        "model_reroutes": {
            "observed": False,
            "events": [],
            "omitted": {
                "count": 0,
                "bytes": 0,
                "sha256": hashlib.sha256().hexdigest(),
            },
        },
        "failure": None,
    }
    assert audit[1] == {
        "event": "item_completed",
        "item": {
            "type": "agentMessage",
            "id": "item-fake-auto-final",
            "text": "AUTO_COMPLETE_RESULT",
            "phase": "final_answer",
        },
    }

    # The fake deliberately emitted a completed userMessage containing the
    # sentinel.  Only trusted agent/tool completions may reach the round audit.
    assert user_sentinel not in audit_text
    assert "userMessage" not in audit_text
    assert "danus-round:" not in audit_text
    assert "STALE_OTHER_TURN_MUST_NOT_PERSIST" not in audit_text

    trace = _trace_messages(trace_path)
    turn_start = next(m for m in trace if m.get("method") == "turn/start")
    assert not any(m.get("method") == "thread/read" for m in trace)
    assert turn_start["params"]["input"] == [
        {"type": "text", "text": user_sentinel}
    ]
    assert not any(m.get("method") == "turn/steer" for m in trace)
    assert not any(m.get("method") == "turn/interrupt" for m in trace)
    thread_start = next(m for m in trace if m.get("method") == "thread/start")
    assert thread_start["params"]["cwd"] == str(worker.dir / "model_workspace")
    assert turn_start["params"]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(worker.local_memory)],
        "networkAccess": False,
        "excludeTmpdirEnvVar": True,
        "excludeSlashTmp": True,
    }
    canonical = HotJoinStore(worker.project_dir).latest_round_audit(worker.name)
    assert canonical is not None
    assert canonical["payload"] == audit_text

    # run_round_app_server owns and reaps the fake process on the clean path.
    pid = next(m["pid"] for m in trace if m.get("_fake") == "started")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _configured_worker_fake_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    *,
    stored_thread: bool = False,
    hard_timeout: int = 3,
) -> tuple[Any, Any, Path, Path, Callable[[], int]]:
    """Configure one production adapter round against a named offline fake."""
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(
        tmp_path / "project" / "workers" / "max"
    )
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    if stored_thread:
        HotJoinStore(worker.project_dir).set_thread_id(worker.name, THREAD_ID)
    audit_path = worker.dir / f"round-{scenario}.jsonl"
    trace_path = tmp_path / f"worker-{scenario}-trace.jsonl"
    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": scenario,
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(
        worker_loop.codex, "subprocess_env", lambda _codex_bin: dict(fake_env)
    )
    monkeypatch.setattr(worker_loop, "preflight_app_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker_loop,
        "app_server_argv",
        lambda *_a: [sys.executable, str(FAKE), "--scenario", scenario],
    )
    def run() -> int:
        return worker_loop.run_round_app_server(
            worker,
            {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
            "offline scenario prompt",
            audit_path,
            hard_timeout=hard_timeout,
        )

    return worker_loop, worker, audit_path, trace_path, run


def _run_worker_fake_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    *,
    stored_thread: bool = False,
) -> tuple[Any, Any, Path, Path, int]:
    """Run one production adapter round against a named offline fake."""
    worker_loop, worker, audit_path, trace_path, run = (
        _configured_worker_fake_scenario(
            tmp_path, monkeypatch, scenario, stored_thread=stored_thread
        )
    )
    return worker_loop, worker, audit_path, trace_path, run()


def test_worker_host_loss_before_paid_dispatch_sends_no_turn_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker_loop, worker, _audit_path, trace_path, run = (
        _configured_worker_fake_scenario(
            tmp_path, monkeypatch, "auto-complete", hard_timeout=8
        )
    )
    original_rpc = AppServerClient.rpc
    killed = False

    def kill_before_model_list(
        client: AppServerClient,
        method: str,
        params: dict[str, Any],
        timeout: float = hotjoin_module.DEFAULT_RPC_TIMEOUT,
    ) -> Any:
        nonlocal killed
        if method == "model/list" and not killed:
            killed = True
            host = client.process
            assert host is not None
            os.kill(host.pid, signal.SIGKILL)
            _wait_until(lambda: hotjoin_module.owned_child_exited_no_reap(host))
        return original_rpc(client, method, params, timeout)

    monkeypatch.setattr(AppServerClient, "rpc", kill_before_model_list)
    assert run() == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 0
    assert HotJoinStore(worker.project_dir).unfinished_round_intent(worker.name) is None
    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["attempt_failure_code"] == "app_server_host_lost"
    assert worker_loop._Child.proc is None


def test_worker_host_loss_after_turn_start_application_is_delivery_unknown_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker_loop, worker, audit_path, trace_path, run = (
        _configured_worker_fake_scenario(
            tmp_path,
            monkeypatch,
            "host-loss-after-turn-start-applied",
            hard_timeout=20,
        )
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        _wait_until(
            lambda: any(
                row.get("_fake") == "turn_start_applied_before_ack"
                for row in _trace_messages(trace_path)
            ),
            timeout=5,
        )
        host = worker_loop._Child.proc
        assert host is not None
        actual_pid = next(
            row["pid"]
            for row in _trace_messages(trace_path)
            if row.get("_fake") == "started"
        )
        os.kill(host.pid, signal.SIGKILL)
        with pytest.raises(HotJoinError, match="cleanup is still in progress"):
            worker_loop._acquire_paid_authority(worker)
        rc = future.result(timeout=10)

    assert rc == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1
    intent = HotJoinStore(worker.project_dir).unfinished_round_intent(worker.name)
    assert intent is not None and intent["state"] == "delivery_unknown"
    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["attempt_failure_code"] == "app_server_host_lost"
    audit = HotJoinStore(worker.project_dir).latest_round_audit_event(
        intent["client_id"], kind="attempt"
    )
    assert audit is not None
    assert "owned-child host exited unexpectedly" in audit["payload"]
    assert audit_path.exists()
    _wait_until(lambda: not _pid_running(actual_pid))
    paid_fd = worker_loop._acquire_paid_authority(worker)
    os.close(paid_fd)


def test_active_worker_detects_host_only_sigkill_and_sweeps_stubborn_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker_loop, worker, audit_path, trace_path, run = (
        _configured_worker_fake_scenario(
            tmp_path, monkeypatch, "cleanup-ignore-sigterm", hard_timeout=60
        )
    )
    store = HotJoinStore(worker.project_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        _wait_until(
            lambda: any(
                row.get("_fake") == "stubborn_grandchild"
                for row in _trace_messages(trace_path)
            )
            and (
                (intent := store.unfinished_round_intent(worker.name))
                is not None
                and intent["state"] == "started"
            ),
            timeout=5,
        )
        trace = _trace_messages(trace_path)
        actual_pid = next(
            row["pid"] for row in trace if row.get("_fake") == "started"
        )
        grandchild = next(
            row["pid"]
            for row in trace
            if row.get("_fake") == "stubborn_grandchild"
        )
        host = worker_loop._Child.proc
        assert host is not None
        assert next(
            row["pgid"]
            for row in trace
            if row.get("_fake") == "stubborn_grandchild"
        ) == host.pid
        started = time.monotonic()
        os.kill(host.pid, signal.SIGKILL)
        with pytest.raises(HotJoinError, match="cleanup is still in progress"):
            worker_loop._acquire_paid_authority(worker)
        rc = future.result(timeout=10)
        assert time.monotonic() - started < 8

    assert rc == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    intent = store.unfinished_round_intent(worker.name)
    # The acknowledged active turn is conservatively terminalized as an
    # ambiguous delivery outcome by the nonterminal attempt audit.
    assert intent is not None and intent["state"] == "delivery_unknown"
    assert intent["turn_id"] == TURN_ID
    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["attempt_failure_code"] == "app_server_host_lost"
    audit = store.latest_round_audit_event(
        intent["client_id"], kind="attempt"
    )
    assert audit is not None
    assert "owned-child host exited unexpectedly" in audit["payload"]
    assert audit_path.exists()
    _wait_until(
        lambda: not _pid_running(actual_pid) and not _pid_running(grandchild),
        timeout=8,
    )
    _wait_until(
        lambda: not any(
            thread.name.startswith(("app-server-reader-", "hotjoin-broker-"))
            for thread in threading.enumerate()
        ),
        timeout=3,
    )
    assert worker_loop._Child.proc is None
    paid_fd = worker_loop._acquire_paid_authority(worker)
    os.close(paid_fd)


def test_terminal_cached_then_host_lost_during_settle_uses_cached_audit_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker_loop, worker, audit_path, trace_path, run = (
        _configured_worker_fake_scenario(
            tmp_path, monkeypatch, "auto-complete", hard_timeout=20
        )
    )
    original_settle = AppServerClient.settle_after_terminal
    killed = False

    def kill_during_settle(
        client: AppServerClient, thread_id: str, turn_id: str, timeout: float
    ) -> None:
        nonlocal killed
        if not killed:
            killed = True
            host = client.process
            assert host is not None
            os.kill(host.pid, signal.SIGKILL)
            _wait_until(lambda: hotjoin_module.owned_child_exited_no_reap(host))
        original_settle(client, thread_id, turn_id, timeout)

    monkeypatch.setattr(
        AppServerClient, "settle_after_terminal", kill_during_settle
    )
    assert run() == worker_loop.APP_SERVER_MODEL_REROUTED_RC
    assert killed is True
    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["attempt_failure_code"] == "app_server_host_lost"
    final = HotJoinStore(worker.project_dir).latest_round_audit(worker.name)
    assert final is not None
    header = json.loads(final["payload"].splitlines()[0])
    assert header["status"] == "completed"
    assert header["token_usage_finality"] == "not_attested_after_host_loss"
    assert header["model_rerouted"] is None
    assert header["model_reroute_observation"] == "unknown_after_host_loss"
    assert audit_path.read_text(encoding="utf-8") == final["payload"]
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1


def test_terminal_then_malformed_stream_is_fail_stop_and_not_false_attested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker_loop, worker, audit_path, trace_path, rc = _run_worker_fake_scenario(
        tmp_path, monkeypatch, "terminal-then-malformed"
    )
    assert rc == worker_loop.APP_SERVER_MODEL_REROUTED_RC
    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["attempt_failure_code"] == "app_server_failure"
    final = HotJoinStore(worker.project_dir).latest_round_audit(worker.name)
    assert final is not None
    header = json.loads(final["payload"].splitlines()[0])
    assert header["status"] == "completed"
    assert header["token_usage_finality"] == (
        "not_attested_after_adapter_interruption"
    )
    assert header["model_rerouted"] is None
    assert header["model_reroute_observation"] == (
        "unknown_after_adapter_interruption"
    )
    assert audit_path.read_text(encoding="utf-8") == final["payload"]
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1


def test_broker_failure_without_terminal_is_statused_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker_loop, worker, audit_path, _trace_path, run = (
        _configured_worker_fake_scenario(
            tmp_path, monkeypatch, "notification-before-response"
        )
    )
    canary = "BROKER_FAILURE_CANARY_987"

    def fail_broker_start(broker: Any) -> None:
        broker._error = ProtocolError(
            f"Authorization: Bearer {canary} api_key={canary}"
        )

    original_rpc = AppServerClient.rpc

    def no_terminal_interrupt(
        client: AppServerClient,
        method: str,
        params: dict[str, Any],
        timeout: float = hotjoin_module.DEFAULT_RPC_TIMEOUT,
    ) -> Any:
        if method == "turn/interrupt":
            raise TimeoutError("offline interrupt acknowledgement withheld")
        return original_rpc(client, method, params, timeout)

    monkeypatch.setattr(worker_loop.HotJoinBroker, "start", fail_broker_start)
    monkeypatch.setattr(AppServerClient, "rpc", no_terminal_interrupt)
    assert run() == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    status_text = worker.status.read_text(encoding="utf-8")
    status = json.loads(status_text)
    assert status["attempt_phase"] == "broker_failure"
    assert status["attempt_failure_code"] == "hotjoin_broker_failure"
    intent = HotJoinStore(worker.project_dir).unfinished_round_intent(worker.name)
    assert intent is not None and intent["state"] == "delivery_unknown"
    attempt = HotJoinStore(worker.project_dir).latest_round_audit_event(
        intent["client_id"], kind="attempt"
    )
    assert attempt is not None
    assert audit_path.read_text(encoding="utf-8") == attempt["payload"]
    assert canary not in status_text
    assert canary not in attempt["payload"]


def test_surrogate_runtime_response_fails_closed_and_reaps_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker_loop, worker, _audit_path, trace_path, rc = _run_worker_fake_scenario(
        tmp_path, monkeypatch, "surrogate-thread-runtime"
    )
    assert rc == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["attempt_failure_code"] == "app_server_failure"
    assert status["attempt_dispatch_state"] == "none"
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 0
    actual_pid = next(
        row["pid"] for row in trace if row.get("_fake") == "started"
    )
    _wait_until(lambda: not _pid_running(actual_pid))
    assert worker_loop._Child.proc is None


def test_worker_rejects_exact_turn_model_reroute_and_ignores_stale_reroute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _loop, _worker, audit_path, trace_path, rc = _run_worker_fake_scenario(
        tmp_path, monkeypatch, "model-rerouted"
    )

    assert rc == 125
    audit_text = audit_path.read_text(encoding="utf-8")
    header = json.loads(audit_text.splitlines()[0])
    assert header["status"] == "interrupted"
    assert header["model_rerouted"] is True
    assert header["model_reroute_observation"] == "observed_live_stream"
    assert header["failure"] == (
        "app-server model/rerouted observed for exact thread/turn; round rejected"
    )
    assert header["model_reroutes"] == {
        "observed": True,
        "events": [
            {
                "fromModel": "offline-model",
                "reason": "highRiskCyberActivity",
                "threadId": THREAD_ID,
                "toModel": "offline-safety-model",
                "turnId": TURN_ID,
            }
        ],
        "omitted": {
            "count": 0,
            "bytes": 0,
            "sha256": hashlib.sha256().hexdigest(),
        },
    }
    assert "stale-from-model" not in audit_text
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1
    assert sum(row.get("method") == "turn/interrupt" for row in trace) == 1


def test_known_app_server_rpc_failure_is_fail_stop_before_paid_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _loop, _worker, _audit_path, trace_path, rc = _run_worker_fake_scenario(
        tmp_path, monkeypatch, "model-list-error"
    )
    assert rc == 123
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "model/list" for row in trace) == 1
    assert sum(row.get("method") == "turn/start" for row in trace) == 0


def test_external_rpc_secrets_are_redacted_from_status_round_audit_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _loop, worker, audit_path, _trace_path, rc = _run_worker_fake_scenario(
        tmp_path, monkeypatch, "secret-rpc-error"
    )
    assert rc == 123
    canaries = (
        "CANARY_SECRET_123",
        "CANARY_KEY_456",
        "CANARY_CLIENT_789",
    )
    status_text = worker.status.read_text(encoding="utf-8")
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "<redacted>" in status_text
    assert "<redacted>" in audit_text
    for canary in canaries:
        assert canary not in status_text
        assert canary not in audit_text
    control = worker.project_dir / ".human-intervention"
    durable_bytes = b"".join(
        path.read_bytes()
        for path in control.iterdir()
        if path.is_file() and path.name.startswith("events.sqlite3")
    )
    for canary in canaries:
        assert canary.encode() not in durable_bytes


def test_bounded_thread_state_gate_refuses_active_thread_before_resume_or_paid_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _loop, worker, _audit_path, trace_path, rc = _run_worker_fake_scenario(
        tmp_path,
        monkeypatch,
        "persisted-thread-active",
        stored_thread=True,
    )
    assert rc == 123
    trace = _trace_messages(trace_path)
    reads = [row for row in trace if row.get("method") == "thread/read"]
    assert [row["params"] for row in reads] == [
        {"threadId": THREAD_ID, "includeTurns": False}
    ]
    assert not any(row.get("method") == "thread/resume" for row in trace)
    assert not any(row.get("method") == "turn/start" for row in trace)
    assert HotJoinStore(worker.project_dir).unfinished_round_intent(worker.name) is None
    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["attempt_dispatch_state"] == "none"
    assert "active before the next round" in status["attempt_failure"]


def test_failed_paid_terminal_is_audited_then_fail_stops_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _loop, _worker, audit_path, trace_path, rc = _run_worker_fake_scenario(
        tmp_path, monkeypatch, "terminal-failed"
    )
    assert rc == 123
    header = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["terminal_observed"] is True
    assert header["status"] == "failed"
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1


@pytest.mark.parametrize(
    ("failure_rc", "expected_error"),
    [
        (
            125,
            "app-server model provenance was rerouted or could not be recovered; "
            "automatic retry disabled",
        ),
        (
            123,
            "app-server protocol, configuration, authentication, or delivery "
            "failure; automatic retry disabled",
        ),
    ],
)
def test_outer_loop_never_automatically_retries_fail_stop_app_server_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_rc: int,
    expected_error: str,
):
    from danus.execution import loop as worker_loop

    worker_dir = tmp_path / "project" / "workers" / "max"
    worker_dir.mkdir(parents=True)
    calls: list[str] = []
    statuses: list[dict[str, Any]] = []
    monkeypatch.setenv("DANUS_WORKER_TRANSPORT", "app-server")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(
        worker_loop,
        "_read_role",
        lambda *_a, **_k: {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
    )
    monkeypatch.setattr(worker_loop.scaffold, "write_codex_config", lambda *_a: None)
    monkeypatch.setattr(worker_loop.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(worker_loop, "_cleanup_pid", lambda *_a: None)
    monkeypatch.setattr(worker_loop, "_prior_round_sequence", lambda *_a: 0)
    monkeypatch.setattr(
        worker_loop, "_canonical_app_server_fact_id", lambda *_a: None
    )
    monkeypatch.setattr(
        worker_loop,
        "write_status",
        lambda _worker, **fields: statuses.append(dict(fields)),
    )

    def failed_round(*_args, **_kwargs) -> int:
        calls.append("paid-turn")
        return failure_rc

    monkeypatch.setattr(worker_loop, "run_round_app_server", failed_round)

    assert worker_loop.main(str(worker_dir)) == failure_rc
    assert calls == ["paid-turn"]
    assert statuses[-1] == {
        "state": "error",
        "error": expected_error,
        "recovery_required": None,
    }


def test_outer_status_attributes_recovered_paid_turn_and_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from danus.execution import loop as worker_loop

    worker_dir = tmp_path / "project" / "workers" / "max"
    worker_dir.mkdir(parents=True)
    monkeypatch.setenv("DANUS_WORKER_TRANSPORT", "app-server")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(
        worker_loop,
        "_read_role",
        lambda *_a, **_k: {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
    )
    monkeypatch.setattr(worker_loop.scaffold, "write_codex_config", lambda *_a: None)
    monkeypatch.setattr(worker_loop.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(worker_loop, "_cleanup_pid", lambda *_a: None)
    monkeypatch.setattr(worker_loop, "_prior_round_sequence", lambda *_a: 0)
    monkeypatch.setattr(
        worker_loop, "_canonical_app_server_fact_id", lambda *_a: None
    )
    monkeypatch.setattr(
        worker_loop,
        "_read_status_snapshot",
        lambda _worker: {
            "attempt_phase": "recovered_turn_terminalization",
            "attempt_dispatch_state": "recovered",
            "attempt_failure_code": "adapter_interrupted",
            "attempt_failure": "recovered paid terminal",
            "attempt_client_id": "client-recovered",
            "attempt_thread_id": THREAD_ID,
            "attempt_turn_id": TURN_ID,
            "last_turn_status": "completed",
            "last_turn_token_usage": {"totalTokens": 37},
            "last_turn_token_usage_observed": True,
            "last_turn_token_usage_finality": "observed_not_schema_attested_final",
            "last_turn_model": "offline-model",
            "last_turn_effort": "low",
            "last_turn_model_rerouted": None,
        },
    )
    monkeypatch.setattr(
        worker_loop,
        "run_round_app_server",
        lambda *_a, **_k: worker_loop.APP_SERVER_MODEL_REROUTED_RC,
    )

    assert (
        worker_loop.main(str(worker_dir))
        == worker_loop.APP_SERVER_MODEL_REROUTED_RC
    )
    status = json.loads((worker_dir / ".status.json").read_text(encoding="utf-8"))
    assert status["last_attempt"]["dispatch_state"] == "recovered"
    assert status["last_paid_turn"]["dispatch_state"] == "recovered"
    assert status["last_paid_turn"]["turn_id"] == TURN_ID
    assert status["last_paid_turn"]["terminal_status"] == "completed"
    assert status["last_paid_turn"]["token_usage"] == {"totalTokens": 37}


def test_four_hour_timeout_then_oversize_resume_preserves_paid_outcome_and_sends_no_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A terminal 4h-sized history may break resume, never outcome attribution.

    The fake's first paid turn reaches the real hard-timeout/interrupt path.  Its
    second app-server process answers the bounded metadata read, then emits one
    >8 MiB ``thread/resume`` response.  The outer loop must preserve the first
    paid timeout separately from the second pre-dispatch failure and must not
    send another ``turn/start``.
    """
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(
        tmp_path / "project" / "workers" / "max"
    )
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    # Match the production incident's round numbers without creating any prior
    # conversation or paid intent.
    worker.status.write_text(json.dumps({"round": 1}), encoding="utf-8")
    trace_path = tmp_path / "four-hour-history-trace.jsonl"
    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": "four-hour-history-oversize",
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    monkeypatch.setenv("DANUS_WORKER_TRANSPORT", "app-server")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "0")
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(
        worker_loop,
        "_read_role",
        lambda *_a, **_k: {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
    )
    monkeypatch.setattr(worker_loop.scaffold, "write_codex_config", lambda *_a: None)
    monkeypatch.setattr(worker_loop.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(
        worker_loop.codex, "subprocess_env", lambda _codex_bin: dict(fake_env)
    )
    monkeypatch.setattr(worker_loop, "preflight_app_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker_loop,
        "app_server_argv",
        lambda *_a: [
            sys.executable,
            str(FAKE),
            "--scenario",
            "four-hour-history-oversize",
        ],
    )
    real_round = worker_loop.run_round_app_server

    def fast_hard_timeout(wl, role, prompt, log_path, _configured_timeout):
        return real_round(wl, role, prompt, log_path, hard_timeout=0.02)

    monkeypatch.setattr(worker_loop, "run_round_app_server", fast_hard_timeout)

    assert worker_loop.main(str(worker.dir)) == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1
    assert sum(row.get("method") == "turn/interrupt" for row in trace) == 1
    bounded_reads = [row for row in trace if row.get("method") == "thread/read"]
    assert [row["params"]["includeTurns"] for row in bounded_reads] == [False]
    assert sum(row.get("method") == "thread/resume" for row in trace) == 1

    store = HotJoinStore(worker.project_dir)
    with store._connect() as db:
        intents = [dict(row) for row in db.execute("SELECT * FROM round_intents")]
    assert len(intents) == 1
    assert intents[0]["state"] == "completed"
    assert intents[0]["terminal_status"] == "interrupted"
    paid_audit = store.latest_round_audit(worker.name)
    assert paid_audit is not None
    paid_header = json.loads(str(paid_audit["payload"]).splitlines()[0])
    assert paid_header["status"] == "interrupted"
    assert paid_header["failure"] == "round hard-timeout after 0.02s"
    assert "JSONL line exceeds hard limit" in (
        worker.logs / "round_3.log"
    ).read_text(encoding="utf-8")

    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["state"] == "error"
    assert status["round"] == 3
    assert status["last_rc"] == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    assert status["last_paid_turn"]["round"] == 2
    assert status["last_paid_turn"]["rc"] == 124
    assert status["last_paid_turn"]["terminal_status"] == "interrupted"
    assert status["last_paid_turn"]["failure_code"] == "hard_timeout"
    assert status["last_attempt"]["round"] == 3
    assert status["last_attempt"]["dispatch_state"] == "none"
    assert (
        status["last_attempt"]["failure_code"]
        == worker_loop.THREAD_HISTORY_OVERSIZE_CODE
    )
    recovery = status["recovery_required"]
    assert recovery["action"] == "rotate_thread"
    assert recovery["drops_conversation_context"] is True
    assert recovery["argv"][:3] == ["bin/danus", "rotate-thread", "project/max"]
    assert THREAD_ID in recovery["argv"]
    assert "no new turn/start was sent" in status["error"]


@pytest.mark.parametrize("pending_state", ["started", "delivery_unknown"])
def test_oversize_resume_with_ambiguous_paid_intent_never_offers_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_state: str,
):
    """An unresolved paid dispatch is preserved, never mislabeled terminal."""
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(tmp_path / "project" / "workers" / "max")
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    trace_path = tmp_path / f"ambiguous-{pending_state}-oversize.jsonl"
    # Prime only the fake server's deterministic oversize-resume switch.  This
    # is trace harness state, not a simulated completed Danus paid intent.
    trace_path.write_text(
        json.dumps({"_fake": "four_hour_turn_applied"}) + "\n",
        encoding="utf-8",
    )
    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": "four-hour-history-oversize",
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    role = {"MODEL": "offline-model", "REASONING_EFFORT": "low"}
    prompt = worker_loop.kickoff(worker.project, worker.name)
    store = HotJoinStore(worker.project_dir)
    store.set_thread_id(worker.name, THREAD_ID)
    intent = store.round_intent(
        worker.name,
        THREAD_ID,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        requested_model=role["MODEL"],
        requested_effort=role["REASONING_EFFORT"],
    )
    store.record_round_intent(
        intent["client_id"], "dispatching", expected_states={"prepared"}
    )
    store.record_round_intent(
        intent["client_id"],
        pending_state,
        turn_id=TURN_ID if pending_state == "started" else None,
        expected_states={"dispatching"},
    )
    intent_before = store.get_round_intent(intent["client_id"])
    events_before = store.round_audit_events(intent["client_id"])

    monkeypatch.setenv("DANUS_WORKER_TRANSPORT", "app-server")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop, "_read_role", lambda *_a, **_k: dict(role))
    monkeypatch.setattr(worker_loop.scaffold, "write_codex_config", lambda *_a: None)
    monkeypatch.setattr(worker_loop.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(worker_loop, "_cleanup_pid", lambda *_a: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(
        worker_loop.codex, "subprocess_env", lambda _codex_bin: dict(fake_env)
    )
    monkeypatch.setattr(worker_loop, "preflight_app_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker_loop,
        "app_server_argv",
        lambda *_a: [
            sys.executable,
            str(FAKE),
            "--scenario",
            "four-hour-history-oversize",
        ],
    )

    assert worker_loop.main(str(worker.dir)) == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "thread/read" for row in trace) == 0
    assert sum(row.get("method") == "thread/resume" for row in trace) == 1
    assert sum(row.get("method") == "turn/start" for row in trace) == 0
    assert store.get_round_intent(intent["client_id"]) == intent_before
    assert store.round_audit_events(intent["client_id"]) == events_before

    status = json.loads(worker.status.read_text(encoding="utf-8"))
    assert status["state"] == "error"
    assert status["recovery_required"] is None
    assert "ambiguous paid intent preserved" in status["error"]
    assert "owner must reconcile/abandon explicitly" in status["error"]
    assert "prior paid outcome" not in status["error"]
    assert status["last_attempt"]["failure_code"] == (
        worker_loop.THREAD_HISTORY_OVERSIZE_CODE
    )
    assert status["last_attempt"]["dispatch_state"] == "none"
    assert status["last_attempt"]["client_id"] == intent["client_id"]
    assert status["last_attempt"]["thread_id"] == THREAD_ID
    assert status["last_attempt"]["turn_id"] == (
        TURN_ID if pending_state == "started" else None
    )
    assert "rotate-thread" not in json.dumps(status)


def test_oversize_recovery_intent_query_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from danus.execution import loop as worker_loop

    worker_dir = tmp_path / "project" / "workers" / "max"
    worker_dir.mkdir(parents=True)
    statuses: list[dict[str, Any]] = []
    calls: list[str] = []
    monkeypatch.setenv("DANUS_WORKER_TRANSPORT", "app-server")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(
        worker_loop,
        "_read_role",
        lambda *_a, **_k: {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
    )
    monkeypatch.setattr(worker_loop.scaffold, "write_codex_config", lambda *_a: None)
    monkeypatch.setattr(worker_loop.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(worker_loop, "_cleanup_pid", lambda *_a: None)
    monkeypatch.setattr(worker_loop, "_prior_round_sequence", lambda *_a: 0)
    monkeypatch.setattr(
        worker_loop, "_canonical_app_server_fact_id", lambda *_a: None
    )
    monkeypatch.setattr(
        worker_loop,
        "write_status",
        lambda _worker, **fields: statuses.append(dict(fields)),
    )
    monkeypatch.setattr(
        worker_loop,
        "_read_status_snapshot",
        lambda _worker: {
            "attempt_phase": "thread_resume",
            "attempt_dispatch_state": "none",
            "attempt_failure_code": worker_loop.THREAD_HISTORY_OVERSIZE_CODE,
            "attempt_thread_id": THREAD_ID,
        },
    )

    def failed_round(*_args, **_kwargs) -> int:
        calls.append("adapter-attempt")
        return worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC

    def fail_query(*_args, **_kwargs):
        raise RuntimeError("injected intent query failure")

    monkeypatch.setattr(worker_loop, "run_round_app_server", failed_round)
    monkeypatch.setattr(HotJoinStore, "unfinished_round_intent", fail_query)

    assert worker_loop.main(str(worker_dir)) == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    assert calls == ["adapter-attempt"]
    assert statuses[-1]["state"] == "error"
    assert statuses[-1]["recovery_required"] is None
    assert "ambiguous paid intent preserved" in statuses[-1]["error"]
    assert "could not be read safely" in statuses[-1]["error"]
    assert "rotate-thread" not in json.dumps(statuses[-1])


def test_worker_drains_delayed_usage_after_terminal_without_claiming_finality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _loop, _worker, audit_path, _trace_path, rc = _run_worker_fake_scenario(
        tmp_path, monkeypatch, "terminal-before-delayed-usage"
    )

    assert rc == 0
    header = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["status"] == "completed"
    assert header["token_usage"]["last"]["totalTokens"] == 29
    assert header["token_usage_observed"] is True
    assert header["token_usage_finality"] == "observed_not_schema_attested_final"
    assert header["post_terminal_settle_bound_ms"] == 250
    assert header["model_rerouted"] is False
    assert header["model_reroute_observation"] == "not_observed_live_stream"


def test_active_paid_turn_honors_cooperative_owner_stop_without_external_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(tmp_path / "project" / "workers" / "max")
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    trace_path = tmp_path / "cooperative-stop-trace.jsonl"
    audit_path = worker.dir / "cooperative-stop-audit.jsonl"
    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": "timeout-interrupt",
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(
        worker_loop.codex, "subprocess_env", lambda _codex_bin: dict(fake_env)
    )
    monkeypatch.setattr(worker_loop, "preflight_app_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker_loop,
        "app_server_argv",
        lambda *_a: [
            sys.executable,
            str(FAKE),
            "--scenario",
            "timeout-interrupt",
        ],
    )
    result: dict[str, Any] = {}

    def run() -> None:
        result["rc"] = worker_loop.run_round_app_server(
            worker,
            {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
            "cooperative stop prompt",
            audit_path,
            hard_timeout=30,
        )

    import threading

    thread = threading.Thread(target=run)
    thread.start()
    _wait_until(
        lambda: any(
            row.get("method") == "turn/start" for row in _trace_messages(trace_path)
        )
    )
    worker.stop.write_text("force\n", encoding="utf-8")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["rc"] == worker_loop.WORKER_STOP_REQUESTED_RC
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1
    assert sum(row.get("method") == "turn/interrupt" for row in trace) == 1
    header = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["failure"] == "cooperative owner stop requested"


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_interrupts"),
    [
        ("turn-start-applied-then-crash", "completed", 0),
        ("turn-start-rerouted-active-then-crash", "interrupted", 1),
    ],
)
def test_paid_turn_start_ack_loss_is_recovered_once_then_provenance_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_status: str,
    expected_interrupts: int,
):
    """A stable round client id prevents an ack-loss paid-turn duplicate."""
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(
        tmp_path / "project" / "workers" / "max"
    )
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    audit_path = worker.dir / "round-recovered.jsonl"
    trace_path = tmp_path / "round-recovery-trace.jsonl"
    prompt = "same immutable round prompt"
    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": scenario,
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(
        worker_loop.codex, "subprocess_env", lambda _codex_bin: dict(fake_env)
    )
    monkeypatch.setattr(worker_loop, "preflight_app_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker_loop,
        "app_server_argv",
        lambda *_a: [
            sys.executable,
            str(FAKE),
            "--scenario",
            scenario,
        ],
    )
    role = {"MODEL": "offline-model", "REASONING_EFFORT": "low"}

    assert worker_loop.run_round_app_server(
        worker, role, prompt, audit_path, hard_timeout=2
    ) == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    store = HotJoinStore(worker.project_dir)
    pending = store.unfinished_round_intent(worker.name)
    assert pending is not None
    assert pending["state"] == "delivery_unknown"
    assert pending["turn_id"] is None

    assert worker_loop.run_round_app_server(
        worker, role, prompt, audit_path, hard_timeout=2
    ) == worker_loop.APP_SERVER_MODEL_REROUTED_RC
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 1
    assert sum(row.get("method") == "thread/start" for row in trace) == 1
    assert sum(row.get("method") == "thread/resume" for row in trace) == 1
    assert sum(row.get("_fake") == "round_turn_applied" for row in trace) == 1
    assert sum(row.get("method") == "turn/interrupt" for row in trace) == expected_interrupts

    assert store.unfinished_round_intent(worker.name) is None
    intent = store.get_round_intent(str(pending["client_id"]))
    assert intent["state"] == "completed"
    assert intent["turn_id"] == TURN_ID
    audit_text = audit_path.read_text(encoding="utf-8")
    audit = [json.loads(line) for line in audit_text.splitlines()]
    assert audit[0]["terminal_observed"] is True
    assert audit[0]["status"] == expected_status
    assert audit[0]["token_usage"] is None
    assert audit[0]["token_usage_observed"] is False
    assert (
        audit[0]["token_usage_finality"]
        == "not_observed_after_bounded_post_terminal_settle"
    )
    assert audit[0]["model_rerouted"] is None
    assert (
        audit[0]["model_reroute_observation"]
        == "unknown_after_adapter_interruption"
    )
    assert audit[0]["model_reroutes"]["observed"] is None
    expected_recovery_failure = (
        "recovered after prior adapter interruption"
        if expected_status == "completed"
        else "recovered in-progress paid turn after prior adapter interruption; "
        "interrupted"
    )
    assert audit[0]["failure"] == (
        f"{expected_recovery_failure}; model reroute observation unavailable after "
        "adapter interruption; round quarantined"
    )
    if expected_status == "completed":
        assert audit[1]["item"]["text"] == "RECOVERED_ROUND_RESULT"
    assert "redacted-by-test" not in audit_text
    assert "danus-round:" not in audit_text


def test_dispatching_paid_intent_without_history_is_quarantined_not_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(tmp_path / "project" / "workers" / "max")
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    trace_path = tmp_path / "dispatching-trace.jsonl"
    audit_path = worker.dir / "dispatching-audit.jsonl"
    prompt = "immutable prompt"
    store = HotJoinStore(worker.project_dir)
    store.set_thread_id(worker.name, THREAD_ID)
    intent = store.round_intent(
        worker.name,
        THREAD_ID,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        requested_model="offline-model",
        requested_effort="low",
    )
    store.record_round_intent(
        intent["client_id"], "dispatching", expected_states={"prepared"}
    )
    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": "auto-complete",
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(worker_loop.codex, "subprocess_env", lambda _bin: fake_env)
    monkeypatch.setattr(worker_loop, "preflight_app_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker_loop,
        "app_server_argv",
        lambda *_a: [sys.executable, str(FAKE), "--scenario", "auto-complete"],
    )

    assert worker_loop.run_round_app_server(
        worker,
        {"MODEL": "offline-model", "REASONING_EFFORT": "low"},
        prompt,
        audit_path,
        hard_timeout=2,
    ) == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    assert store.get_round_intent(intent["client_id"])["state"] == "delivery_unknown"
    assert not any(
        row.get("method") == "turn/start" for row in _trace_messages(trace_path)
    )
    attempts = store.round_audit_events(intent["client_id"])
    assert [row["kind"] for row in attempts] == ["attempt"]
    assert json.loads(attempts[0]["payload"].splitlines()[0])["terminal_observed"] is False


def test_owner_abandoned_paid_intent_restart_never_dispatches_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An explicit abandonment fences the old thread until reset/rotation."""
    from danus.execution import layout as worker_layout
    from danus.execution import loop as worker_loop

    worker = worker_layout.WorkerLayout(tmp_path / "project" / "workers" / "max")
    worker.dir.mkdir(parents=True)
    worker.task.write_text("offline assignment", encoding="utf-8")
    trace_path = tmp_path / "abandoned-restart-trace.jsonl"
    audit_path = worker.dir / "abandoned-restart-audit.jsonl"
    prompt = "immutable abandoned prompt"
    role = {"MODEL": "offline-model", "REASONING_EFFORT": "low"}
    store = HotJoinStore(worker.project_dir)
    store.set_thread_id(worker.name, THREAD_ID)
    intent = store.round_intent(
        worker.name,
        THREAD_ID,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        requested_model=role["MODEL"],
        requested_effort=role["REASONING_EFFORT"],
    )
    store.record_round_intent(
        intent["client_id"], "dispatching", expected_states={"prepared"}
    )
    store.record_round_intent(
        intent["client_id"],
        "delivery_unknown",
        expected_states={"dispatching"},
    )
    store.abandon_round_intent(
        target=worker.name,
        thread_id=THREAD_ID,
        client_id=intent["client_id"],
        expected_state="delivery_unknown",
        reason="owner reconciled all available remote history",
        acknowledge_paid_outcome_unknown=True,
    )
    intent_before = store.get_round_intent(intent["client_id"])
    operator_before = store.round_operator_events(client_id=intent["client_id"])

    fake_env = os.environ.copy()
    fake_env.update(
        {
            "DANUS_FAKE_APP_SERVER_SCENARIO": "auto-complete",
            "DANUS_FAKE_APP_SERVER_TRACE": str(trace_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(worker_loop.codex, "subprocess_env", lambda _bin: fake_env)
    monkeypatch.setattr(worker_loop, "preflight_app_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker_loop,
        "app_server_argv",
        lambda *_a: [sys.executable, str(FAKE), "--scenario", "auto-complete"],
    )

    assert worker_loop.run_round_app_server(
        worker, role, prompt, audit_path, hard_timeout=2
    ) == worker_loop.APP_SERVER_PROTOCOL_FAILURE_RC
    trace = _trace_messages(trace_path)
    assert sum(row.get("method") == "turn/start" for row in trace) == 0
    assert store.get_round_intent(intent["client_id"]) == intent_before
    assert store.round_operator_events(client_id=intent["client_id"]) == operator_before
    assert store.unfinished_round_intent(worker.name) is None


def test_round_audit_bounds_adversarial_metadata_and_total_projection_bytes():
    from danus.execution import loop as worker_loop

    huge = "UNTRUSTED-ERROR-" + "x" * (9 * 1024 * 1024)

    class AuditClient:
        def token_usage(self, _thread_id: str, _turn_id: str):
            return {"malformed": huge}

        def model_reroutes(self, _thread_id: str, _turn_id: str):
            return {"observed": False, "events": [], "omitted": {}}

        def notifications(self):
            return [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "type": "agentMessage",
                            "id": f"item-{index}",
                            "text": "z" * 240_000,
                        },
                    },
                }
                for index in range(50)
            ]

        def notification_omissions(self):
            return {"count": 0, "bytes": 0, "sha256": hashlib.sha256().hexdigest()}

    payload = worker_loop._build_app_server_audit(
        AuditClient(),
        thread_id="thread-1",
        turn_id="turn-1",
        terminal={"status": "failed", "durationMs": 1, "items": []},
        requested_model=huge,
        requested_effort=huge,
        actual_model=huge,
        thread_reasoning_effort=huge,
        failure=huge,
    )
    assert len(payload.encode("utf-8")) <= worker_loop.MAX_ROUND_AUDIT_BYTES
    assert huge not in payload
    rows = [json.loads(line) for line in payload.splitlines()]
    assert rows[0]["token_usage"]["omitted"] is True
    assert rows[0]["failure"].startswith("<external error omitted bytes=")
    assert rows[-1]["event"] == "audit_items_omitted"
    assert rows[-1]["count"] > 0


def test_round_audit_distinguishes_verifier_acceptance_from_fact_promotion():
    from danus.execution import loop as worker_loop

    class AuditClient:
        def token_usage(self, _thread_id: str, _turn_id: str):
            return None

        def model_reroutes(self, _thread_id: str, _turn_id: str):
            return {"observed": False, "events": [], "omitted": {}}

        def notifications(self):
            return [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "type": "mcpToolCall",
                            "id": "submit-write-failed",
                            "server": "danus",
                            "tool": "fact_submit",
                            "status": "completed",
                            "result": {
                                "structuredContent": {
                                    "accepted": True,
                                    "promoted": False,
                                    "submission_status": "verified_not_promoted",
                                    "verification_verdict": "correct",
                                    "fact_id": None,
                                    "write_error": "glossary_conflict: Q_X",
                                }
                            },
                        },
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "type": "mcpToolCall",
                            "id": "submit-promoted",
                            "server": "danus",
                            "tool": "fact_submit",
                            "status": "completed",
                            "result": {
                                "structuredContent": {
                                    "accepted": True,
                                    "promoted": True,
                                    "submission_status": "promoted",
                                    "verification_verdict": "correct",
                                    "fact_id": "0123456789abcdef",
                                }
                            },
                        },
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "type": "mcpToolCall",
                            "id": "submit-legacy-write-failed",
                            "server": "danus",
                            "tool": "fact_submit",
                            "status": "completed",
                            "result": {
                                "structuredContent": {
                                    "accepted": True,
                                    "fact_id": None,
                                    "write_error": "glossary_conflict: Q_X",
                                }
                            },
                        },
                    },
                },
            ]

        def notification_omissions(self):
            return {"count": 0, "bytes": 0, "sha256": hashlib.sha256().hexdigest()}

    payload = worker_loop._build_app_server_audit(
        AuditClient(),
        thread_id="thread-1",
        turn_id="turn-1",
        terminal={"status": "completed", "durationMs": 1, "items": []},
        requested_model="offline-model",
        requested_effort="low",
        actual_model="offline-model",
        thread_reasoning_effort="low",
    )
    summaries = {
        row["item"]["id"]: row["item"]["fact_submit_result"]
        for row in map(json.loads, payload.splitlines())
        if row.get("event") == "item_completed"
    }
    assert summaries["submit-write-failed"] == {
        "accepted": True,
        "promoted": False,
        "submission_status": "verified_not_promoted",
        "verification_verdict": "correct",
        "fact_id": None,
    }
    assert summaries["submit-promoted"] == {
        "accepted": True,
        "promoted": True,
        "submission_status": "promoted",
        "verification_verdict": "correct",
        "fact_id": "0123456789abcdef",
    }
    assert summaries["submit-legacy-write-failed"] == {
        "accepted": True,
        "promoted": False,
        "submission_status": "verified_not_promoted",
        "verification_verdict": None,
        "fact_id": None,
    }
    assert worker_loop._last_promoted_fact_id(payload) == "0123456789abcdef"
