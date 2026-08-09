#!/usr/bin/env python3
"""Deterministic, offline stand-in for ``codex app-server`` contract tests.

The process speaks newline-delimited JSON over stdin/stdout and never starts
Codex, opens a network connection, or invokes a model.  A production client can
point its explicit ``argv`` at this file, for example::

    [sys.executable, str(FAKE), "--scenario", "notification-before-response"]

The scenario and an optional request trace can also be supplied through
``DANUS_FAKE_APP_SERVER_SCENARIO`` and ``DANUS_FAKE_APP_SERVER_TRACE``.  Extra
arguments such as the real CLI's ``app-server``/``--listen`` flags are ignored,
which keeps the fixture usable by a client that normally constructs Codex CLI
argv itself.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO


THREAD_ID = "thread-fake-1"
TURN_ID = "turn-current-1"
CRASH_EXIT_CODE = 86

_SCENARIOS = {
    "auto-complete",
    "notification-before-response",
    "stale-turn",
    "accepted-then-crash",
    "applied-then-crash-before-response",
    "out-of-order-responses",
    "stderr-flood",
    "timeout-interrupt",
    "cleanup-ignore-sigterm",
    "graceful-eof",
    "model-rerouted",
    "model-list-error",
    "secret-rpc-error",
    "terminal-before-delayed-usage",
    "terminal-then-malformed",
    "terminal-failed",
    "host-loss-after-turn-start-applied",
    "surrogate-thread-runtime",
    "turn-start-applied-then-crash",
    "turn-start-rerouted-active-then-crash",
    "four-hour-history-oversize",
    "persisted-thread-active",
}


def _selected_scenario(argv: list[str]) -> str:
    scenario = os.environ.get(
        "DANUS_FAKE_APP_SERVER_SCENARIO", "notification-before-response"
    )
    if "--scenario" in argv:
        pos = argv.index("--scenario")
        if pos + 1 >= len(argv):
            raise ValueError("--scenario requires a value")
        scenario = argv[pos + 1]
    if scenario not in _SCENARIOS:
        raise ValueError(
            f"unknown fake app-server scenario {scenario!r}; "
            f"choose one of {sorted(_SCENARIOS)!r}"
        )
    return scenario


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _rpc_result(request_id: Any, result: dict[str, Any]) -> None:
    _emit({"id": request_id, "result": result})


def _rpc_error(
    request_id: Any, message: str, *, code: int = -32000, data: Any = None
) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _emit({"id": request_id, "error": error})


def _thread(status: str = "idle") -> dict[str, Any]:
    runtime_status: dict[str, Any] = {"type": status}
    if status == "active":
        runtime_status["activeFlags"] = []
    return {
        "id": THREAD_ID,
        "sessionId": THREAD_ID,
        "preview": "",
        "modelProvider": "fake-offline",
        "createdAt": 1,
        "updatedAt": 1,
        "status": runtime_status,
        "ephemeral": True,
        "turns": [],
        "cwd": os.getcwd(),
        "cliVersion": "fake-1.0",
        "source": "appServer",
    }


def _thread_runtime_response() -> dict[str, Any]:
    return {
        "thread": _thread(),
        "model": "offline-model",
        "reasoningEffort": "low",
        "cwd": os.getcwd(),
        "approvalPolicy": "never",
        "sandbox": {
            "type": "workspaceWrite",
            "writableRoots": [],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        },
        "runtimeWorkspaceRoots": [],
    }


def _turn(status: str, *, error: dict[str, Any] | None = None) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "id": TURN_ID,
        "items": [],
        "status": status,
        "error": error,
        "startedAt": 1,
        "completedAt": 2 if status != "inProgress" else None,
        "durationMs": 10 if status != "inProgress" else None,
    }
    return turn


def _breakdown(
    *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
) -> dict[str, int]:
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached_input_tokens,
        "cacheWriteInputTokens": 0,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": min(output_tokens, 2),
        "totalTokens": input_tokens + output_tokens,
    }


def _usage(*, last: dict[str, int], total: dict[str, int]) -> None:
    _emit(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "tokenUsage": {
                    "last": last,
                    "total": total,
                    "modelContextWindow": 100_000,
                },
            },
        }
    )


def _reroute(
    *,
    thread_id: str,
    turn_id: str,
    from_model: str,
    to_model: str,
    reason: str = "highRiskCyberActivity",
) -> None:
    _emit(
        {
            "method": "model/rerouted",
            "params": {
                "fromModel": from_model,
                "reason": reason,
                "threadId": thread_id,
                "toModel": to_model,
                "turnId": turn_id,
            },
        }
    )


def _status(status: str) -> None:
    value: dict[str, Any] = {"type": status}
    if status == "active":
        value["activeFlags"] = []
    _emit(
        {
            "method": "thread/status/changed",
            "params": {"threadId": THREAD_ID, "status": value},
        }
    )


def _terminal(status: str) -> None:
    _emit(
        {
            "method": "turn/completed",
            "params": {"threadId": THREAD_ID, "turn": _turn(status)},
        }
    )
    _status("idle")


def _input_text(params: dict[str, Any]) -> str:
    texts = []
    for item in params.get("input", []):
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(str(item.get("text", "")))
    return "\n".join(texts)


class _Trace:
    def __init__(self) -> None:
        raw_path = os.environ.get("DANUS_FAKE_APP_SERVER_TRACE")
        self._stream: TextIO | None = None
        if raw_path:
            path = Path(raw_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("a", encoding="utf-8")

    def record(self, message: dict[str, Any]) -> None:
        if self._stream is None:
            return
        self._stream.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()


def main(argv: list[str] | None = None) -> int:
    try:
        scenario = _selected_scenario(list(sys.argv[1:] if argv is None else argv))
    except ValueError as exc:
        sys.stderr.write(f"fake_codex_app_server: {exc}\n")
        return 2

    if scenario == "cleanup-ignore-sigterm":
        signal.signal(signal.SIGTERM, lambda _signum, _frame: None)

    trace_path = os.environ.get("DANUS_FAKE_APP_SERVER_TRACE")
    prior_records: list[dict[str, Any]] = []
    if trace_path and Path(trace_path).exists():
        prior_records = [
            json.loads(line)
            for line in Path(trace_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    recovered_round = next(
        (
            row
            for row in reversed(prior_records)
            if row.get("_fake") == "round_turn_applied"
        ),
        None,
    )
    four_hour_turn_applied = any(
        row.get("_fake") == "four_hour_turn_applied" for row in prior_records
    )
    trace = _Trace()
    trace.record({"_fake": "started", "pid": os.getpid(), "scenario": scenario})
    if scenario == "cleanup-ignore-sigterm":
        stubborn_grandchild = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
            ],
            close_fds=True,
        )
        trace.record(
            {
                "_fake": "stubborn_grandchild",
                "pid": stubborn_grandchild.pid,
                "pgid": os.getpgid(stubborn_grandchild.pid),
            }
        )
    initialize_seen = False
    initialized = False
    thread_started = False
    active = False
    held_thread_read_id: Any = None

    try:
        for raw_line in sys.stdin:
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                _rpc_error(None, f"invalid JSON: {exc}", code=-32700)
                continue
            if not isinstance(message, dict):
                _rpc_error(None, "request must be a JSON object", code=-32600)
                continue
            trace.record(message)

            method = message.get("method")
            request_id = message.get("id")
            params = message.get("params") or {}

            if method == "initialize":
                initialize_seen = True
                _rpc_result(
                    request_id,
                    {
                        "userAgent": "fake-codex-app-server/1.0",
                        "platformFamily": "unix",
                        "platformOs": "fake",
                        "codexHome": os.getcwd(),
                    },
                )
                continue

            if method == "initialized":
                if initialize_seen:
                    initialized = True
                continue

            if request_id is not None and not initialized:
                _rpc_error(request_id, "Not initialized", code=-32002)
                continue

            if method == "model/list":
                if scenario == "model-list-error":
                    _rpc_error(
                        request_id,
                        "authentication/configuration rejected by fake provider",
                        code=-32009,
                    )
                    continue
                _rpc_result(
                    request_id,
                    {
                        "data": [
                            {
                                "id": "offline-model",
                                "model": "offline-model",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low", "description": "fake"}
                                ],
                            }
                        ]
                    },
                )
                continue

            if method == "thread/start":
                thread_started = True
                # The notification intentionally precedes the matching response.
                _emit({"method": "thread/started", "params": {"thread": _thread()}})
                runtime = _thread_runtime_response()
                if scenario == "surrogate-thread-runtime":
                    runtime["reasoningEffort"] = "\ud800"
                _rpc_result(request_id, runtime)
                continue

            if method == "thread/resume":
                thread_started = True
                resumed = _thread_runtime_response()
                if scenario == "four-hour-history-oversize" and four_hour_turn_applied:
                    historical_turn = _turn("interrupted")
                    historical_turn["items"] = [
                        {
                            "type": "agentMessage",
                            "id": "item-four-hour-history",
                            "text": "H" * (8 * 1024 * 1024 + 4096),
                            "phase": "final_answer",
                        }
                    ]
                    resumed["thread"]["turns"] = [historical_turn]
                _rpc_result(request_id, resumed)
                continue

            if request_id is not None and not thread_started and method != "thread/read":
                _rpc_error(request_id, "thread not started", code=-32003)
                continue

            if scenario == "out-of-order-responses" and method == "thread/read":
                if held_thread_read_id is not None:
                    _rpc_error(request_id, "only one delayed read is supported", code=-32008)
                    continue
                held_thread_read_id = request_id
                _emit(
                    {
                        "method": "warning",
                        "params": {
                            "threadId": THREAD_ID,
                            "message": "fake-held-thread-read-response",
                        },
                    }
                )
                continue

            if (
                scenario == "out-of-order-responses"
                and method == "thread/loaded/list"
                and held_thread_read_id is not None
            ):
                # Reply to the second request first, then release the response
                # for the older pending request.  Correct clients route both by
                # id; a FIFO response waiter will swap or lose them.
                _rpc_result(request_id, {"data": [THREAD_ID], "nextCursor": None})
                _rpc_result(held_thread_read_id, {"thread": _thread()})
                held_thread_read_id = None
                continue

            if method == "thread/read":
                thread = _thread(
                    "active" if scenario == "persisted-thread-active" else "idle"
                )
                if scenario == "turn-start-applied-then-crash" and recovered_round:
                    recovered_turn = _turn("completed")
                    recovered_turn["items"] = [
                        {
                            "type": "userMessage",
                            "id": "item-recovered-user",
                            "clientId": recovered_round["client_id"],
                            "content": [{"type": "text", "text": "redacted-by-test"}],
                        },
                        {
                            "type": "agentMessage",
                            "id": "item-recovered-final",
                            "text": "RECOVERED_ROUND_RESULT",
                            "phase": "final_answer",
                        },
                    ]
                    thread["turns"] = [recovered_turn]
                elif (
                    scenario == "turn-start-rerouted-active-then-crash"
                    and recovered_round
                ):
                    recovered_turn = _turn("inProgress")
                    recovered_turn["items"] = [
                        {
                            "type": "userMessage",
                            "id": "item-recovered-active-user",
                            "clientId": recovered_round["client_id"],
                            "content": [{"type": "text", "text": "redacted-by-test"}],
                        }
                    ]
                    thread["turns"] = [recovered_turn]
                    active = True
                _rpc_result(request_id, {"thread": thread})
                continue

            if method == "turn/start":
                if scenario == "secret-rpc-error":
                    _rpc_error(
                        request_id,
                        "Authorization: Bearer CANARY_SECRET_123\n"
                        '{"apiKey":"CANARY_KEY_456","clientSecret":'
                        '"CANARY_CLIENT_789"}',
                        code=-32010,
                    )
                    continue
                if active:
                    _rpc_error(request_id, "turn already active", code=-32004)
                    continue
                active = True
                if scenario == "stderr-flood":
                    # Larger than ordinary pipe buffers.  A client that does not
                    # drain stderr concurrently deadlocks before seeing the
                    # turn/start response on stdout.
                    sys.stderr.write("F" * (256 * 1024) + "\n")
                    sys.stderr.flush()
                _status("active")
                # This is the central race: both notifications arrive before the
                # turn/start RPC response and must not be consumed or discarded by
                # an RPC waiter.
                _emit(
                    {
                        "method": "turn/started",
                        "params": {"threadId": THREAD_ID, "turn": _turn("inProgress")},
                    }
                )
                if scenario == "four-hour-history-oversize":
                    trace.record(
                        {
                            "_fake": "four_hour_turn_applied",
                            "client_id": params.get("clientUserMessageId"),
                            "threadId": THREAD_ID,
                            "turnId": TURN_ID,
                        }
                    )
                if scenario in {
                    "turn-start-applied-then-crash",
                    "turn-start-rerouted-active-then-crash",
                }:
                    if scenario == "turn-start-rerouted-active-then-crash":
                        _reroute(
                            thread_id=THREAD_ID,
                            turn_id=TURN_ID,
                            from_model="offline-model",
                            to_model="offline-safety-model",
                        )
                    trace.record(
                        {
                            "_fake": "round_turn_applied",
                            "client_id": params.get("clientUserMessageId"),
                            "threadId": THREAD_ID,
                            "turnId": TURN_ID,
                        }
                    )
                    os._exit(CRASH_EXIT_CODE)
                if scenario == "host-loss-after-turn-start-applied":
                    # The paid request has been applied, but its response has
                    # not been written. The test SIGKILLs only the retained
                    # host in this exact acknowledgement-loss window.
                    trace.record(
                        {
                            "_fake": "turn_start_applied_before_ack",
                            "client_id": params.get("clientUserMessageId"),
                            "threadId": THREAD_ID,
                            "turnId": TURN_ID,
                        }
                    )
                    while True:
                        time.sleep(1)
                if scenario == "stale-turn":
                    # A terminal notification for an earlier turn must not
                    # satisfy a waiter scoped to the new active turn.
                    old_turn = _turn("completed")
                    old_turn["id"] = "turn-old-0"
                    _emit(
                        {
                            "method": "turn/completed",
                            "params": {"threadId": THREAD_ID, "turn": old_turn},
                        }
                    )
                if scenario == "notification-before-response":
                    initial = _breakdown(input_tokens=7, output_tokens=4)
                    _usage(last=initial, total=initial)
                _rpc_result(request_id, {"turn": _turn("inProgress")})
                if scenario == "auto-complete":
                    _emit(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "thread-stale",
                                "turnId": "turn-stale",
                                "item": {
                                    "type": "agentMessage",
                                    "id": "item-stale",
                                    "text": "STALE_OTHER_TURN_MUST_NOT_PERSIST",
                                    "phase": "final_answer",
                                },
                            },
                        }
                    )
                    # A normal worker round can finish without a hot-join.  Emit
                    # the original user item too, so the production audit test
                    # proves it filters user text instead of merely observing a
                    # fixture that never supplied any.
                    _emit(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": THREAD_ID,
                                "turnId": TURN_ID,
                                "item": {
                                    "type": "userMessage",
                                    "id": "item-fake-user",
                                    "clientId": params.get("clientUserMessageId"),
                                    "content": list(params.get("input", [])),
                                },
                            },
                        }
                    )
                    _emit(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": THREAD_ID,
                                "turnId": TURN_ID,
                                "item": {
                                    "type": "agentMessage",
                                    "id": "item-fake-auto-final",
                                    "text": "AUTO_COMPLETE_RESULT",
                                    "phase": "final_answer",
                                },
                            },
                        }
                    )
                    final = _breakdown(
                        input_tokens=12, output_tokens=5, cached_input_tokens=3
                    )
                    _usage(last=final, total=final)
                    _terminal("completed")
                    active = False
                elif scenario == "model-rerouted":
                    # An unrelated reroute must never contaminate the exact
                    # current thread/turn audit or rejection decision.
                    _reroute(
                        thread_id="thread-stale",
                        turn_id="turn-stale",
                        from_model="stale-from-model",
                        to_model="stale-to-model",
                    )
                    _reroute(
                        thread_id=THREAD_ID,
                        turn_id=TURN_ID,
                        from_model="offline-model",
                        to_model="offline-safety-model",
                    )
                    final = _breakdown(input_tokens=4, output_tokens=1)
                    _usage(last=final, total=final)
                elif scenario == "terminal-before-delayed-usage":
                    # Flush a terminal event first.  The client must keep its
                    # single reader alive for a bounded settle window instead
                    # of prematurely claiming that token usage is absent/final.
                    _terminal("completed")
                    active = False
                    time.sleep(0.075)
                    delayed = _breakdown(
                        input_tokens=21, output_tokens=8, cached_input_tokens=5
                    )
                    _usage(last=delayed, total=delayed)
                elif scenario == "terminal-then-malformed":
                    _terminal("completed")
                    active = False
                    sys.stdout.write("{malformed-after-terminal\n")
                    sys.stdout.flush()
                elif scenario == "terminal-failed":
                    _terminal("failed")
                    active = False
                continue

            if method == "turn/steer":
                expected = params.get("expectedTurnId")
                if not active:
                    _rpc_error(request_id, "no active turn to steer", code=-32005)
                    continue
                if expected != TURN_ID:
                    _rpc_error(
                        request_id,
                        "expected turn does not match active turn",
                        code=-32006,
                        data={
                            "reason": "staleTurn",
                            "expectedTurnId": TURN_ID,
                            "receivedTurnId": expected,
                        },
                    )
                    continue

                if scenario == "applied-then-crash-before-response":
                    # The trace is the test oracle that the fake applied the
                    # operation.  The client intentionally receives neither a
                    # response nor a terminal notification, so it must surface
                    # delivery_unknown rather than retrying blindly.
                    trace.record(
                        {"_fake": "steer_applied", "threadId": THREAD_ID, "turnId": TURN_ID}
                    )
                    os._exit(CRASH_EXIT_CODE)

                # Flush acceptance before any simulated crash or completion.
                _rpc_result(request_id, {"turnId": TURN_ID})
                if scenario == "accepted-then-crash":
                    os._exit(CRASH_EXIT_CODE)
                if scenario == "timeout-interrupt":
                    continue

                _emit(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": THREAD_ID,
                            "turnId": TURN_ID,
                            "item": {
                                "type": "agentMessage",
                                "id": "item-fake-final",
                                "text": "HOT_JOIN_ACK " + _input_text(params),
                                "phase": "final_answer",
                            },
                        },
                    }
                )
                final_last = _breakdown(input_tokens=9, output_tokens=9, cached_input_tokens=2)
                final_total = _breakdown(
                    input_tokens=16, output_tokens=13, cached_input_tokens=2
                )
                _usage(last=final_last, total=final_total)
                _terminal("completed")
                active = False
                continue

            if method == "turn/interrupt":
                if not active:
                    _rpc_error(request_id, "no active turn to interrupt", code=-32007)
                    continue
                _rpc_result(request_id, {})
                _terminal("interrupted")
                active = False
                continue

            if request_id is not None:
                _rpc_error(request_id, f"unknown method: {method}", code=-32601)

        if scenario == "cleanup-ignore-sigterm":
            # Closing stdin is the graceful shutdown signal.  This scenario
            # deliberately ignores both EOF and SIGTERM so the client's bounded
            # cleanup path must escalate to kill() and reap the process.
            while True:
                time.sleep(1)
        if scenario == "graceful-eof":
            time.sleep(0.1)
            trace.record({"_fake": "graceful_eof_flushed"})
    finally:
        trace.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
