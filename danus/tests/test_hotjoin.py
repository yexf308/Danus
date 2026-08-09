"""Offline correctness tests for the thin human hot-join control layer."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from danus import hotjoin as hotjoin_module
from danus.core import FactGraph
from danus.hotjoin import (
    AppServerClient,
    HotJoinBroker,
    HotJoinError,
    HotJoinStore,
    IdempotencyConflict,
    ProtocolError,
    RpcError,
    StaleClaim,
)
from danus.orchestration import cli


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "projects" / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    return project.resolve(), worker.resolve()


def _wait_state(store: HotJoinStore, message_id: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        row = store.get(message_id)
        if row["state"] == state:
            return row
        time.sleep(0.01)
    raise AssertionError(f"message did not reach state {state}: {store.get(message_id)}")


class _StubClient:
    def __init__(self, active_turn: str | None = "turn-1", read_payload: Any = None):
        self.turn = active_turn
        self.read_payload = read_payload or {
            "thread": {"id": "thread-1", "turns": []}
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.terminals: dict[tuple[str, str], dict[str, Any]] = {}

    def active_turn(self, _thread_id: str) -> str | None:
        return self.turn

    def ensure_owned_host_alive(self) -> None:
        return None

    def rpc(self, method: str, params: dict[str, Any], timeout: float = 30) -> Any:
        self.calls.append((method, params))
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "thread/read":
            return self.read_payload
        if method == "turn/interrupt":
            self.terminals[(params["threadId"], params["turnId"])] = {
                "id": params["turnId"],
                "status": "interrupted",
            }
            self.turn = None
            return {}
        raise AssertionError(method)

    def terminal_turn(self, thread_id: str, turn_id: str) -> dict[str, Any] | None:
        return self.terminals.get((thread_id, turn_id))


def test_store_is_idempotent_append_only_and_rejects_content_conflict(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    first = store.enqueue(
        target="max", body="Try spectral rounding.", client_id="owner-msg-1"
    )
    duplicate = store.enqueue(
        target="max", body="Try spectral rounding.", client_id="owner-msg-1"
    )
    assert duplicate["message_id"] == first["message_id"]
    assert [event["state"] for event in store.events(first["message_id"])] == [
        "persisted"
    ]
    with pytest.raises(IdempotencyConflict):
        store.enqueue(target="max", body="Different", client_id="owner-msg-1")


def test_store_serializes_full_sqlite_lifecycle_across_threads_and_instances(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    shared = HotJoinStore(project)
    start = threading.Event()
    errors: list[BaseException] = []

    def record_errors(action: Any) -> None:
        try:
            start.wait()
            action()
        except BaseException as exc:
            errors.append(exc)

    def writer() -> None:
        for index in range(60):
            shared.enqueue(
                target="max",
                body=f"stress-{index}",
                client_id=f"stress-client-{index}",
            )

    def constructor_reader() -> None:
        for _ in range(60):
            HotJoinStore(project).list_messages(target="max", limit=5)

    def broker_rate_reader() -> None:
        local = HotJoinStore(project)
        for _ in range(180):
            local.thread_id("max")
            local.list_messages(target="max", limit=5)

    threads = [
        threading.Thread(
            target=record_errors, args=(action,), daemon=True
        )
        for action in (writer, constructor_reader, broker_rate_reader)
    ]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(shared.list_messages(target="max", limit=100)) == 60


def test_frontier_commits_lifecycle_and_content_hash_without_message_body(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    sentinel = "PRIVATE-HUMAN-GUIDANCE-7805"
    message = store.enqueue(
        target="max", body=sentinel, client_id="frontier-message"
    )
    persisted = store.frontier("max")
    assert persisted["schema_version"] == 1
    assert persisted["event_count"] == 1
    assert persisted["accepted_message_ids"] == []
    assert persisted["accepted_message_count"] == 0
    assert persisted["accepted_message_ids_omitted"] == 0
    assert sentinel not in json.dumps(persisted, ensure_ascii=False)

    assert store.claim(target="max", owner="broker", allow_queued=True) is not None
    routing = store.frontier("max")
    assert routing["digest"] != persisted["digest"]
    assert routing["routing_message_ids"] == [message["message_id"]]
    assert routing["routing_message_count"] == 1

    store.record(
        message["message_id"],
        "steer_accepted",
        thread_id="thread-1",
        turn_id="turn-1",
    )
    accepted = store.frontier("max")
    assert accepted["digest"] != routing["digest"]
    assert accepted["accepted_message_ids"] == [message["message_id"]]
    assert accepted["accepted_message_count"] == 1
    assert accepted["routing_message_ids"] == []
    assert sentinel not in json.dumps(accepted, ensure_ascii=False)


def test_frontier_caps_visible_ids_and_commits_omitted_ids(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    messages = [
        store.enqueue(target="max", body=f"guidance {index}")
        for index in range(130)
    ]
    for message in messages:
        claimed = store.claim(target="max", owner="broker", allow_queued=True)
        assert claimed is not None
        store.record(
            claimed["message_id"],
            "steer_accepted",
            thread_id="thread-1",
            turn_id="turn-1",
        )

    frontier = store.frontier("max")

    assert frontier["event_count"] == 390
    assert frontier["accepted_message_count"] == 130
    assert len(frontier["accepted_message_ids"]) == 128
    assert frontier["accepted_message_ids_omitted"] == 2
    assert frontier["accepted_message_ids_sha256"] != hashlib.sha256().hexdigest()
    assert len(json.dumps(frontier).encode("utf-8")) < 64 * 1024


def test_expired_delivery_claim_is_fenced_and_never_overwritten(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="one delivery only")
    claimed = store.claim(
        target="max", owner="old-broker", allow_queued=True, lease_seconds=0.001
    )
    assert claimed is not None
    time.sleep(0.01)
    assert (
        store.claim(target="max", owner="new-broker", allow_queued=True) is None
    )
    assert store.get(message["message_id"])["state"] == "delivery_unknown"
    with pytest.raises(StaleClaim):
        store.record(
            message["message_id"],
            "steer_accepted",
            expected_owner=claimed["claim_owner"],
        )
    assert store.get(message["message_id"])["state"] == "delivery_unknown"


def test_store_forbids_verifier_target_and_caps_message_bytes(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    with pytest.raises(ValueError, match="forbidden"):
        store.enqueue(target="verifier", body="accept it")
    with pytest.raises(ValueError, match="exceeds"):
        store.enqueue(target="max", body="x" * (64 * 1024 + 1))


def test_concurrent_first_store_open_is_race_safe(tmp_path: Path):
    project, _worker = _project(tmp_path)
    barrier = threading.Barrier(12)
    errors: list[BaseException] = []

    def open_store() -> None:
        try:
            barrier.wait()
            HotJoinStore(project)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert HotJoinStore(project).list_messages() == []


def test_store_closes_every_short_lived_polling_connection(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="poll without leaking")
    for _ in range(250):
        with store._connect() as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
        store.get(message["message_id"])
        store.frontier("max")


def test_store_closes_connection_when_sqlite_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)

    class FailingConnection:
        row_factory: Any = None
        closed = False

        def execute(self, statement: str) -> None:
            if statement == "PRAGMA foreign_keys=ON":
                raise sqlite3.OperationalError("injected setup failure")

        def close(self) -> None:
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(store, "_open_sqlite", lambda: connection)

    with pytest.raises(sqlite3.OperationalError, match="injected setup failure"):
        with store._connect():
            raise AssertionError("setup failure must occur before yield")
    assert connection.closed is True


def test_concurrent_legacy_round_intent_migration_is_serialized(tmp_path: Path):
    project, _worker = _project(tmp_path)
    control = project / ".human-intervention"
    control.mkdir(mode=0o700)
    database = control / "events.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE round_intents (client_id TEXT PRIMARY KEY,target TEXT NOT NULL,"
            "thread_id TEXT NOT NULL,turn_id TEXT,state TEXT NOT NULL,terminal_status TEXT,"
            "created_ns INTEGER NOT NULL,updated_ns INTEGER NOT NULL)"
        )
    barrier = threading.Barrier(20)
    errors: list[BaseException] = []

    def migrate() -> None:
        try:
            barrier.wait()
            HotJoinStore(project)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(round_intents)")}
    assert {"prompt_sha256", "requested_model", "requested_effort"} <= columns


def test_attempt_then_terminal_audits_are_append_only_and_finalize_atomically(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-1")
    intent = store.round_intent(
        "max",
        "thread-1",
        prompt_sha256=hashlib.sha256(b"prompt").hexdigest(),
        requested_model="offline-model",
        requested_effort="low",
    )
    store.record_round_intent(
        intent["client_id"], "started", turn_id="turn-1", expected_states={"prepared"}
    )
    message = store.enqueue(target="max", body="human direction")
    claimed = store.claim(
        target="max",
        owner="broker",
        allow_queued=True,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert claimed is not None
    store.record(
        message["message_id"],
        "steer_accepted",
        thread_id="thread-1",
        turn_id="turn-1",
        expected_owner=claimed["claim_owner"],
    )

    attempt_payload = '{"event":"turn_completed","terminal_observed":false}\n'
    store.record_round_attempt_audit(intent["client_id"], attempt_payload)
    assert store.get_round_intent(intent["client_id"])["state"] == "delivery_unknown"
    assert store.get(message["message_id"])["state"] == "steer_accepted"

    final_payload = '{"event":"turn_completed","terminal_observed":true}\n'
    canonical = store.finalize_round(
        intent["client_id"],
        final_payload,
        thread_id="thread-1",
        turn_id="turn-1",
        terminal_status="completed",
    )
    assert canonical["payload"] == final_payload
    assert store.get_round_intent(intent["client_id"])["state"] == "completed"
    assert store.get(message["message_id"])["state"] == "turn_completed"
    assert [row["kind"] for row in store.round_audit_events(intent["client_id"])] == [
        "attempt",
        "final",
    ]

    # A replay after atomic commit keeps the first final audit authoritative;
    # missing notification replay cannot deadlock recovery on a digest conflict.
    replay = store.finalize_round(
        intent["client_id"],
        '{"event":"turn_completed","terminal_observed":true,"usage":null}\n',
        thread_id="thread-1",
        turn_id="turn-1",
        terminal_status="completed",
    )
    assert replay["payload"] == final_payload
    assert len(store.round_audit_events(intent["client_id"])) == 2


def test_prepared_round_failure_is_known_undispatched_not_delivery_unknown(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    intent = store.round_intent(
        "max",
        "thread-unmaterialized",
        prompt_sha256="a" * 64,
        requested_model="offline-model",
        requested_effort="low",
    )
    store.record_round_attempt_audit(
        intent["client_id"],
        '{"event":"turn_completed","terminal_observed":false}\n',
    )
    saved = store.get_round_intent(intent["client_id"])
    assert saved["state"] == "failed"
    assert saved["terminal_status"] == "not_dispatched"
    assert store.unfinished_round_intent("max") is None


def test_dangling_database_symlink_cannot_create_outside_target(tmp_path: Path):
    project, _worker = _project(tmp_path)
    control = project / ".human-intervention"
    control.mkdir(mode=0o700)
    outside = tmp_path / "outside" / "new.sqlite3"
    (control / "events.sqlite3").symlink_to(outside)
    with pytest.raises(Exception, match="regular file"):
        HotJoinStore(project)
    assert not outside.exists()


def test_database_hardlink_alias_is_rejected_before_ledger_mutation(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    alias = tmp_path / "model-writable-alias.sqlite3"
    os.link(store.path, alias)
    before = alias.read_bytes()

    with pytest.raises(HotJoinError, match="unaliased regular file"):
        store.enqueue(target="max", body="must not be written")
    with pytest.raises(HotJoinError, match="unaliased regular file"):
        HotJoinStore(project)

    assert alias.read_bytes() == before


def test_broker_steers_exact_active_turn_with_stable_client_id(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="Switch to Sigma-Delta.")
    client = _StubClient()
    broker = HotJoinBroker(store, client, target="max", thread_id="thread-1")
    broker.start()
    try:
        row = _wait_state(store, message["message_id"], "steer_accepted")
    finally:
        broker.stop()
    assert row["turn_id"] == "turn-1"
    method, params = client.calls[0]
    assert method == "turn/steer"
    assert params["expectedTurnId"] == "turn-1"
    assert params["clientUserMessageId"] == f"danus-human:{message['message_id']}"
    assert params["input"] == [{"type": "text", "text": "Switch to Sigma-Delta."}]


def test_no_active_turn_queues_by_default_and_fail_is_explicit(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    queued = store.enqueue(target="max", body="later", fallback="queue")
    failed = store.enqueue(target="max", body="now only", fallback="fail")
    client = _StubClient(active_turn=None)
    broker = HotJoinBroker(store, client, target="max", thread_id="thread-1")
    broker.start()
    try:
        _wait_state(store, queued["message_id"], "queued")
        _wait_state(store, failed["message_id"], "failed")
    finally:
        broker.stop()
    assert client.calls == []

    # A message queued before any turn has a NULL attempted turn and must be
    # delivered exactly once when the next active turn appears.
    client.turn = "turn-2"
    resumed = HotJoinBroker(store, client, target="max", thread_id="thread-1")
    resumed.start()
    try:
        row = _wait_state(store, queued["message_id"], "steer_accepted")
    finally:
        resumed.stop()
    assert row["turn_id"] == "turn-2"
    assert [method for method, _params in client.calls] == ["turn/steer"]


def test_known_rejection_queues_for_next_turn_without_hot_retry(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="deliver once", fallback="queue")

    class RejectOnce(_StubClient):
        def rpc(self, method: str, params: dict[str, Any], timeout: float = 30) -> Any:
            self.calls.append((method, params))
            if method == "turn/steer" and self.turn == "turn-1":
                raise RpcError(method, {"message": "no active turn"})
            if method == "turn/steer":
                return {"turnId": params["expectedTurnId"]}
            return super().rpc(method, params, timeout)

    client = RejectOnce()
    broker = HotJoinBroker(store, client, target="max", thread_id="thread-1")
    broker.start()
    try:
        _wait_state(store, message["message_id"], "queued")
        time.sleep(0.1)
        assert [method for method, _params in client.calls].count("turn/steer") == 1
        client.turn = "turn-2"
        _wait_state(store, message["message_id"], "steer_accepted")
    finally:
        broker.stop()
    assert [method for method, _params in client.calls].count("turn/steer") == 2
    assert store.get(message["message_id"])["turn_id"] == "turn-2"


def test_plain_text_stop_is_not_control_but_typed_interrupt_is(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    plain = store.enqueue(target="max", body="stop and reconsider")
    interrupt = store.enqueue(target="max", body="", kind="interrupt", fallback="fail")
    client = _StubClient()
    broker = HotJoinBroker(store, client, target="max", thread_id="thread-1")
    broker.start()
    try:
        _wait_state(store, interrupt["message_id"], "turn_completed")
    finally:
        broker.stop()
    assert store.get(plain["message_id"])["state"] == "turn_completed"
    assert [event["state"] for event in store.events(plain["message_id"])] == [
        "persisted",
        "routing",
        "steer_accepted",
        "turn_completed",
    ]
    assert [method for method, _params in client.calls] == [
        "turn/steer",
        "turn/interrupt",
    ]


def test_crash_reconciliation_uses_thread_read_client_id(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="durable")
    assert store.claim(target="max", owner="dead", allow_queued=True) is not None
    client_id = HotJoinBroker.client_message_id(message["message_id"])
    client = _StubClient(
        read_payload={
            "thread": {
                    "id": "thread-1",
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "inProgress",
                            "items": [
                                {"type": "userMessage", "clientId": client_id}
                            ],
                        }
                    ]
            }
        }
    )
    broker = HotJoinBroker(store, client, target="max", thread_id="thread-1")
    broker.reconcile_routing()
    assert store.get(message["message_id"])["state"] == "steer_accepted"


def test_ack_lost_message_reconciles_to_exact_completed_turn(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="durable")
    claimed = store.claim(
        target="max",
        owner="dead",
        allow_queued=True,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert claimed is not None
    store.record(
        message["message_id"],
        "delivery_unknown",
        thread_id="thread-1",
        turn_id="turn-1",
        expected_owner=claimed["claim_owner"],
    )
    client_id = HotJoinBroker.client_message_id(message["message_id"])
    client = _StubClient(
        read_payload={
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {"type": "userMessage", "clientId": client_id}
                        ],
                    }
                ]
            }
        }
    )
    HotJoinBroker(
        store, client, target="max", thread_id="thread-1"
    ).reconcile_routing()
    row = store.get(message["message_id"])
    assert row["state"] == "turn_completed"
    assert row["turn_id"] == "turn-1"


def test_conversation_bytes_never_enter_fact_context(tmp_path: Path):
    project, _worker = _project(tmp_path)
    sentinel = "HUMAN-SECRET-DIRECTION-91f0b7"
    HotJoinStore(project).enqueue(target="max", body=sentinel)
    graph = FactGraph(project)
    fact_id = graph.add(
        problem_id="P", author="max", statement="A statement", proof="A proof"
    )
    context = graph.verification_context(
        [fact_id], glossary_texts=["A statement"], max_chars=100_000
    )
    assert sentinel not in json.dumps(context, ensure_ascii=False)


@contextmanager
def _agents_root(root: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(root)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def test_cli_say_messages_and_interrupt_are_durable(tmp_path: Path):
    root = tmp_path / "agents"
    worker = root / "P" / "workers" / "max"
    worker.mkdir(parents=True)
    with _agents_root(root):
        said = cli.do_say("P/max", "human guidance", client_id="cli-1")
        interrupted = cli.do_interrupt_turn("P/max", client_id="cli-2")
        rows = cli.do_messages("P/max")
    assert {row["message_id"] for row in rows} == {
        said["message_id"],
        interrupted["message_id"],
    }
    assert {row["kind"] for row in rows} == {"message", "interrupt"}


def test_owner_reset_thread_is_cas_fenced_audited_and_blocks_unfinished_turn(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-old")

    with pytest.raises(StaleClaim):
        store.clear_thread_id("max", expected_thread_id="thread-stale")
    assert store.thread_id("max") == "thread-old"

    intent = store.round_intent(
        "max",
        "thread-old",
        prompt_sha256="a" * 64,
        requested_model="gpt-5.6-sol",
        requested_effort="max",
    )
    with pytest.raises(HotJoinError, match="unfinished paid-turn intent"):
        store.clear_thread_id("max", expected_thread_id="thread-old")
    with pytest.raises(HotJoinError, match="unfinished paid-turn intent"):
        store.rotate_thread_id(
            "max", expected_thread_id="thread-old", reason="oversize history"
        )
    store.record_round_intent(
        intent["client_id"], "failed", expected_states={"prepared"}
    )

    result = store.clear_thread_id("max", expected_thread_id="thread-old")
    assert result == {
        "target": "max",
        "cleared_thread_id": "thread-old",
        "state": "cleared",
    }
    assert store.thread_id("max") is None
    assert [(event["action"], event["thread_id"]) for event in store.thread_events("max")] == [
        ("set", "thread-old"),
        ("cleared", "thread-old"),
    ]


def test_cli_reset_thread_requires_exact_id_and_no_unfinished_turn(tmp_path: Path):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-lost")
    with _agents_root(root):
        with pytest.raises(SystemExit, match="changed concurrently"):
            cli.do_reset_thread("P/max", expected_thread_id="wrong")
        result = cli.do_reset_thread("P/max", expected_thread_id="thread-lost")
    assert result["state"] == "cleared"


def test_owner_rotate_thread_is_explicit_cas_fenced_and_preserves_research_memory(
    tmp_path: Path,
):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    local_memory = worker / "local_memory"
    fact_graph = project / "fact_graph"
    global_memory = project / "global_memory"
    local_memory.mkdir(parents=True)
    fact_graph.mkdir(parents=True)
    global_memory.mkdir(parents=True)
    sentinels = {
        local_memory / "notes.jsonl": "local survives\n",
        fact_graph / "sentinel": "facts survive\n",
        global_memory / "sentinel": "global survives\n",
    }
    for path, content in sentinels.items():
        path.write_text(content, encoding="utf-8")
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-terminal-large")
    message = store.enqueue(
        target="max", body="owner guidance survives rotation", client_id="rotate-msg"
    )
    messages_before = store.list_messages(target="max")
    deliveries_before = store.events(message["message_id"])

    with _agents_root(root):
        with pytest.raises(SystemExit, match="changed concurrently"):
            cli.do_rotate_thread(
                "P/max", expected_thread_id="thread-stale", reason="8 MiB history"
            )
        with pytest.raises(SystemExit, match="rotation reason is required"):
            cli.do_rotate_thread(
                "P/max", expected_thread_id="thread-terminal-large", reason="  "
            )
        result = cli.do_rotate_thread(
            "P/max",
            expected_thread_id="thread-terminal-large",
            reason="terminal history exceeds bounded resume transport",
        )

    assert result == {
        "target": "max",
        "rotated_thread_id": "thread-terminal-large",
        "state": "rotated",
    }
    assert store.thread_id("max") is None
    assert store.thread_events("max")[-1]["action"] == "rotated"
    assert "bounded resume" in store.thread_events("max")[-1]["detail"]
    assert store.list_messages(target="max") == messages_before
    assert store.events(message["message_id"]) == deliveries_before
    for path, content in sentinels.items():
        assert path.read_text(encoding="utf-8") == content


def _fake_registered_pid(worker: Path, pid: int = 424_242) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pid": pid,
        "pgid": pid,
        "start_token": "race-test-birth",
        "worker_dir": os.path.abspath(str(worker)),
    }


class _FakeSpawnProcess:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self):
        return None

    def wait(self):
        return -9


def _assert_worker_lock_held(worker: Path) -> None:
    wl = cli.L.WorkerLayout(worker)
    probe = cli._open_worker_lock(wl)
    try:
        with pytest.raises(BlockingIOError):
            cli.fcntl.flock(probe, cli.fcntl.LOCK_EX | cli.fcntl.LOCK_NB)
    finally:
        probe.close()


def _mailbox_snapshot(store: HotJoinStore, message_id: str) -> dict[str, Any]:
    return {
        "messages": store.list_messages(target="max"),
        "deliveries": store.events(message_id),
        "thread_events": store.thread_events("max"),
    }


def _round_ledger_snapshot(store: HotJoinStore, client_id: str) -> dict[str, Any]:
    with store._connect() as db:
        return {
            "intent": dict(db.execute(
                "SELECT * FROM round_intents WHERE client_id=?", (client_id,)
            ).fetchone()),
            "round_events": [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM round_events WHERE client_id=? ORDER BY seq",
                    (client_id,),
                ).fetchall()
            ],
            "round_audits": [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM round_audit_events WHERE client_id=? ORDER BY seq",
                    (client_id,),
                ).fetchall()
            ],
            "operator_events": [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM round_operator_events WHERE client_id=? ORDER BY seq",
                    (client_id,),
                ).fetchall()
            ],
        }


def _ambiguous_intent(
    store: HotJoinStore, state: str, *, thread_id: str = "thread-ambiguous"
) -> dict[str, Any]:
    intent = store.round_intent(
        "max",
        thread_id,
        prompt_sha256="b" * 64,
        requested_model="offline-model",
        requested_effort="low",
    )
    store.record_round_intent(
        intent["client_id"], "dispatching", expected_states={"prepared"}
    )
    if state == "started":
        store.record_round_intent(
            intent["client_id"],
            "started",
            turn_id="turn-ambiguous",
            expected_states={"dispatching"},
        )
    elif state == "delivery_unknown":
        store.record_round_intent(
            intent["client_id"],
            "delivery_unknown",
            expected_states={"dispatching"},
        )
    elif state != "dispatching":
        raise AssertionError(state)
    return store.get_round_intent(intent["client_id"])


@pytest.mark.parametrize("state", ["dispatching", "started", "delivery_unknown"])
def test_owner_abandon_intent_exact_cas_is_terminal_append_only_and_blocks_restart(
    tmp_path: Path, state: str
):
    project, worker = _project(tmp_path)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-ambiguous")
    message = store.enqueue(
        target="max", body="preserve this owner guidance", client_id=f"keep-{state}"
    )
    intent = _ambiguous_intent(store, state)
    mailbox_before = _mailbox_snapshot(store, message["message_id"])
    ledger_before = _round_ledger_snapshot(store, intent["client_id"])
    sentinels = {
        worker / "local_memory" / "sentinel": "local survives\n",
        project / "fact_graph" / "sentinel": "facts survive\n",
        project / "global_memory" / "sentinel": "global survives\n",
    }
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    receipt = store.abandon_round_intent(
        target="max",
        thread_id="thread-ambiguous",
        client_id=intent["client_id"],
        expected_state=state,
        reason="owner reviewed history; remote paid outcome remains unknowable",
        acknowledge_paid_outcome_unknown=True,
    )

    assert receipt["prior_state"] == state
    assert receipt["terminal_status"] == "owner_abandoned_outcome_unknown"
    saved = store.get_round_intent(intent["client_id"])
    assert saved["state"] == "failed"
    assert saved["terminal_status"] == "owner_abandoned_outcome_unknown"
    assert store.unfinished_round_intent("max") is None
    after = _round_ledger_snapshot(store, intent["client_id"])
    assert after["round_events"][:-1] == ledger_before["round_events"]
    assert after["round_events"][-1]["state"] == "failed"
    assert after["round_audits"] == ledger_before["round_audits"]
    assert len(after["operator_events"]) == 1
    assert after["operator_events"][0]["prior_state"] == state
    assert after["operator_events"][0]["reason"].startswith("owner reviewed")
    assert store.thread_id("max") == "thread-ambiguous"
    mailbox_after = _mailbox_snapshot(store, message["message_id"])
    for key in ("messages", "deliveries"):
        assert mailbox_after[key] == mailbox_before[key]
    for path, content in sentinels.items():
        assert path.read_text(encoding="utf-8") == content

    # A fail-stopped worker restarted before the required reset/rotation may
    # resume the old thread, but it cannot create or resend a paid turn there.
    with pytest.raises(HotJoinError, match="reset or rotate"):
        store.round_intent(
            "max",
            "thread-ambiguous",
            prompt_sha256="b" * 64,
            requested_model="offline-model",
            requested_effort="low",
        )
    assert _round_ledger_snapshot(store, intent["client_id"]) == after


def test_owner_abandon_intent_rejects_wrong_cas_or_missing_risk_ack_without_mutation(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-ambiguous")
    intent = _ambiguous_intent(store, "started")
    before = _round_ledger_snapshot(store, intent["client_id"])
    base = {
        "target": "max",
        "thread_id": "thread-ambiguous",
        "client_id": intent["client_id"],
        "expected_state": "started",
        "reason": "explicit operator review",
        "acknowledge_paid_outcome_unknown": True,
    }
    cases = [
        ({"acknowledge_paid_outcome_unknown": False}, ValueError),
        ({"reason": "  "}, ValueError),
        ({"expected_state": "prepared"}, ValueError),
        ({"expected_state": "delivery_unknown"}, StaleClaim),
        ({"thread_id": "wrong-thread"}, StaleClaim),
        ({"target": "other"}, StaleClaim),
        ({"client_id": "missing-client"}, KeyError),
    ]
    for changed, error in cases:
        arguments = dict(base)
        arguments.update(changed)
        with pytest.raises(error):
            store.abandon_round_intent(**arguments)
        assert _round_ledger_snapshot(store, intent["client_id"]) == before


def test_cli_abandon_intent_requires_failstop_holds_lock_never_signals_then_rotates(
    tmp_path: Path,
):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-ambiguous")
    message = store.enqueue(
        target="max", body="ledger survives abandon", client_id="abandon-ledger"
    )
    intent = _ambiguous_intent(store, "delivery_unknown")
    mailbox_before = _mailbox_snapshot(store, message["message_id"])
    ledger_before = _round_ledger_snapshot(store, intent["client_id"])
    record = _fake_registered_pid(worker, pid=474_747)
    cli._write_pid_record(cli.L.WorkerLayout(worker), record)
    signals: list[tuple[Any, ...]] = []
    originals = (
        cli._pid_record_is_live,
        cli.os.kill,
        cli.os.killpg,
        HotJoinStore.abandon_round_intent,
    )

    cli._pid_record_is_live = lambda observed: observed == record
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    try:
        with _agents_root(root), pytest.raises(SystemExit, match="worker is still live"):
            cli.do_abandon_intent(
                "P/max",
                thread_id="thread-ambiguous",
                client_id=intent["client_id"],
                expected_state="delivery_unknown",
                reason="explicit owner reconciliation",
                acknowledge_paid_outcome_unknown=True,
            )
        assert _round_ledger_snapshot(store, intent["client_id"]) == ledger_before
        assert _mailbox_snapshot(store, message["message_id"]) == mailbox_before

        # Once the stale/dead record is proven non-live, a competing lifecycle
        # owner still blocks the command before SQLite is entered.
        cli._pid_record_is_live = lambda _observed: False
        lifecycle_lock = cli._open_worker_lock(cli.L.WorkerLayout(worker))
        cli.fcntl.flock(lifecycle_lock, cli.fcntl.LOCK_EX | cli.fcntl.LOCK_NB)
        try:
            with _agents_root(root), pytest.raises(SystemExit, match="lifecycle lock is busy"):
                cli.do_abandon_intent(
                    "P/max",
                    thread_id="thread-ambiguous",
                    client_id=intent["client_id"],
                    expected_state="delivery_unknown",
                    reason="explicit owner reconciliation",
                    acknowledge_paid_outcome_unknown=True,
                )
        finally:
            cli.fcntl.flock(lifecycle_lock, cli.fcntl.LOCK_UN)
            lifecycle_lock.close()
        assert _round_ledger_snapshot(store, intent["client_id"]) == ledger_before

        original_abandon = HotJoinStore.abandon_round_intent

        def checked_abandon(self, **kwargs):
            _assert_worker_lock_held(worker)
            return original_abandon(self, **kwargs)

        HotJoinStore.abandon_round_intent = checked_abandon
        with _agents_root(root):
            receipt = cli.do_abandon_intent(
                "P/max",
                thread_id="thread-ambiguous",
                client_id=intent["client_id"],
                expected_state="delivery_unknown",
                reason="explicit owner reconciliation",
                acknowledge_paid_outcome_unknown=True,
            )
            assert receipt["terminal_status"] == "owner_abandoned_outcome_unknown"
            assert store.thread_id("max") == "thread-ambiguous"
            rotated = cli.do_rotate_thread(
                "P/max",
                expected_thread_id="thread-ambiguous",
                reason="owner acknowledged unknown paid outcome",
            )
        assert rotated["state"] == "rotated"
    finally:
        (
            cli._pid_record_is_live,
            cli.os.kill,
            cli.os.killpg,
            HotJoinStore.abandon_round_intent,
        ) = originals

    assert store.thread_id("max") is None
    assert _mailbox_snapshot(store, message["message_id"])["messages"] == (
        mailbox_before["messages"]
    )
    assert _mailbox_snapshot(store, message["message_id"])["deliveries"] == (
        mailbox_before["deliveries"]
    )
    assert signals == []


def test_cancel_prepared_intent_exact_cas_unblocks_drift_without_paid_dispatch(
    tmp_path: Path,
):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-before-drift")
    message = store.enqueue(
        target="max", body="preserve owner guidance", client_id="prepared-guidance"
    )
    intent = store.round_intent(
        "max",
        "thread-before-drift",
        prompt_sha256="c" * 64,
        requested_model="old-model",
        requested_effort="low",
    )
    ledger_before = _round_ledger_snapshot(store, intent["client_id"])
    mailbox_before = _mailbox_snapshot(store, message["message_id"])
    with pytest.raises(StaleClaim):
        store.cancel_prepared_round_intent(
            target="max",
            thread_id="wrong-thread",
            client_id=intent["client_id"],
            reason="configuration drifted before dispatch",
        )
    assert _round_ledger_snapshot(store, intent["client_id"]) == ledger_before

    record = _fake_registered_pid(worker, pid=575_757)
    cli._write_pid_record(cli.L.WorkerLayout(worker), record)
    signals: list[tuple[Any, ...]] = []
    originals = (
        cli._pid_record_is_live,
        cli.os.kill,
        cli.os.killpg,
        HotJoinStore.cancel_prepared_round_intent,
    )
    cli._pid_record_is_live = lambda observed: observed == record
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    try:
        with _agents_root(root), pytest.raises(SystemExit, match="worker is still live"):
            cli.do_cancel_prepared_intent(
                "P/max",
                thread_id="thread-before-drift",
                client_id=intent["client_id"],
                reason="configuration drifted before dispatch",
            )
        cli._pid_record_is_live = lambda _observed: False
        original_cancel = HotJoinStore.cancel_prepared_round_intent

        def checked_cancel(self, **kwargs):
            _assert_worker_lock_held(worker)
            return original_cancel(self, **kwargs)

        HotJoinStore.cancel_prepared_round_intent = checked_cancel
        with _agents_root(root):
            receipt = cli.do_cancel_prepared_intent(
                "P/max",
                thread_id="thread-before-drift",
                client_id=intent["client_id"],
                reason="configuration drifted before dispatch",
            )
            rotated = cli.do_rotate_thread(
                "P/max",
                expected_thread_id="thread-before-drift",
                reason="discard authoritatively unspent intent after drift",
            )
    finally:
        (
            cli._pid_record_is_live,
            cli.os.kill,
            cli.os.killpg,
            HotJoinStore.cancel_prepared_round_intent,
        ) = originals

    assert receipt["terminal_status"] == "owner_cancelled_not_dispatched"
    events = _round_ledger_snapshot(store, intent["client_id"])
    assert [event["state"] for event in events["round_events"]] == [
        "prepared",
        "failed",
    ]
    assert events["operator_events"][-1]["action"] == "cancelled_not_dispatched"
    assert events["operator_events"][-1][
        "acknowledged_paid_outcome_unknown"
    ] == 0
    assert rotated["state"] == "rotated"
    mailbox_after = _mailbox_snapshot(store, message["message_id"])
    for key in ("messages", "deliveries"):
        assert mailbox_after[key] == mailbox_before[key]
    assert signals == []

    # A new immutable configuration can now start on a new thread without any
    # retry of the cancelled client id.
    store.set_thread_id("max", "thread-after-drift")
    replacement = store.round_intent(
        "max",
        "thread-after-drift",
        prompt_sha256="d" * 64,
        requested_model="new-model",
        requested_effort="high",
    )
    assert replacement["state"] == "prepared"
    assert replacement["client_id"] != intent["client_id"]


def _remove_mapping(action: str, target: str, expected_thread_id: str) -> dict:
    if action == "reset":
        return cli.do_reset_thread(
            target, expected_thread_id=expected_thread_id
        )
    return cli.do_rotate_thread(
        target, expected_thread_id=expected_thread_id, reason="terminal"
    )


@pytest.mark.parametrize("action", ["reset", "rotate"])
def test_start_lock_fences_rotation_before_spawn_without_mutation_or_signal(
    tmp_path: Path, action: str
):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-old")
    message = store.enqueue(
        target="max",
        body="must survive busy thread removal",
        client_id=f"busy-before-spawn-{action}",
    )
    ledger_before = _mailbox_snapshot(store, message["message_id"])
    entered = threading.Barrier(2)
    release = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    signals: list[tuple[Any, ...]] = []
    record = _fake_registered_pid(worker)
    originals = (
        cli.spawn_loop,
        cli._capture_pid_record,
        cli._pid_record_is_live,
        cli.os.kill,
        cli.os.killpg,
    )

    def blocked_spawn(_worker_dir: Path) -> int:
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return _FakeSpawnProcess(int(record["pid"]))

    def start() -> None:
        try:
            outcomes["start"] = cli.do_start("P/max")
        except BaseException as exc:  # surfaced in the asserting thread
            outcomes["start_error"] = exc

    cli.spawn_loop = blocked_spawn
    cli._capture_pid_record = lambda _wl, _pid: record
    cli._pid_record_is_live = lambda _record: True
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    thread = threading.Thread(target=start)
    try:
        with _agents_root(root):
            thread.start()
            entered.wait(timeout=2)
            with pytest.raises(SystemExit, match="lifecycle lock is busy"):
                _remove_mapping(action, "P/max", "thread-old")
            assert store.thread_id("max") == "thread-old"
            assert _mailbox_snapshot(store, message["message_id"]) == ledger_before
            assert not (worker / ".pid").exists()
            release.wait(timeout=2)
            thread.join(timeout=2)
    finally:
        if thread.is_alive():
            try:
                release.abort()
            except threading.BrokenBarrierError:
                pass
            thread.join(timeout=2)
        (
            cli.spawn_loop,
            cli._capture_pid_record,
            cli._pid_record_is_live,
            cli.os.kill,
            cli.os.killpg,
        ) = originals

    assert not thread.is_alive()
    assert "start_error" not in outcomes
    assert outcomes["start"] == [{"worker": "max", "result": "started"}]
    assert store.thread_id("max") == "thread-old"
    assert signals == []


def test_rotation_lock_fences_start_until_thread_cas_finishes(tmp_path: Path):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-old")
    message = store.enqueue(
        target="max", body="must survive successful rotation", client_id="rotate-first"
    )
    messages_before = store.list_messages(target="max")
    deliveries_before = store.events(message["message_id"])
    entered = threading.Barrier(2)
    release = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    spawn_calls: list[Path] = []
    signals: list[tuple[Any, ...]] = []
    original_rotate = HotJoinStore.rotate_thread_id
    originals = (cli.spawn_loop, cli.os.kill, cli.os.killpg)

    def blocked_rotate(self, target, *, expected_thread_id, reason):
        # The store CAS is entered only while the worker lifecycle lock remains
        # exclusively held; another start cannot slip into this boundary.
        _assert_worker_lock_held(worker)
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return original_rotate(
            self,
            target,
            expected_thread_id=expected_thread_id,
            reason=reason,
        )

    def rotate() -> None:
        try:
            outcomes["rotate"] = cli.do_rotate_thread(
                "P/max", expected_thread_id="thread-old", reason="terminal"
            )
        except BaseException as exc:  # surfaced in the asserting thread
            outcomes["rotate_error"] = exc

    HotJoinStore.rotate_thread_id = blocked_rotate
    cli.spawn_loop = lambda worker_dir: spawn_calls.append(
        Path(worker_dir)
    ) or _FakeSpawnProcess(424_242)
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    thread = threading.Thread(target=rotate)
    try:
        with _agents_root(root):
            thread.start()
            entered.wait(timeout=2)
            assert cli.do_start("P/max") == [
                {"worker": "max", "result": "locked"}
            ]
            assert store.thread_id("max") == "thread-old"
            assert [event["action"] for event in store.thread_events("max")] == ["set"]
            assert store.list_messages(target="max") == messages_before
            assert store.events(message["message_id"]) == deliveries_before
            release.wait(timeout=2)
            thread.join(timeout=2)
    finally:
        if thread.is_alive():
            try:
                release.abort()
            except threading.BrokenBarrierError:
                pass
            thread.join(timeout=2)
        HotJoinStore.rotate_thread_id = original_rotate
        cli.spawn_loop, cli.os.kill, cli.os.killpg = originals

    assert not thread.is_alive()
    assert "rotate_error" not in outcomes
    assert outcomes["rotate"]["state"] == "rotated"
    assert store.thread_id("max") is None
    assert [event["action"] for event in store.thread_events("max")] == [
        "set",
        "rotated",
    ]
    assert store.list_messages(target="max") == messages_before
    assert store.events(message["message_id"]) == deliveries_before
    assert spawn_calls == []
    assert signals == []


def test_reset_lock_fences_start_until_thread_cas_finishes(tmp_path: Path):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-old")
    message = store.enqueue(
        target="max", body="must survive successful reset", client_id="reset-first"
    )
    messages_before = store.list_messages(target="max")
    deliveries_before = store.events(message["message_id"])
    entered = threading.Barrier(2)
    release = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    spawn_calls: list[Path] = []
    signals: list[tuple[Any, ...]] = []
    original_clear = HotJoinStore.clear_thread_id
    originals = (cli.spawn_loop, cli.os.kill, cli.os.killpg)

    def blocked_clear(self, target, *, expected_thread_id, detail="owner reset"):
        _assert_worker_lock_held(worker)
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return original_clear(
            self,
            target,
            expected_thread_id=expected_thread_id,
            detail=detail,
        )

    def reset() -> None:
        try:
            outcomes["reset"] = cli.do_reset_thread(
                "P/max", expected_thread_id="thread-old"
            )
        except BaseException as exc:
            outcomes["reset_error"] = exc

    HotJoinStore.clear_thread_id = blocked_clear
    cli.spawn_loop = lambda worker_dir: spawn_calls.append(
        Path(worker_dir)
    ) or _FakeSpawnProcess(424_242)
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    thread = threading.Thread(target=reset)
    try:
        with _agents_root(root):
            thread.start()
            entered.wait(timeout=2)
            assert cli.do_start("P/max") == [
                {"worker": "max", "result": "locked"}
            ]
            assert store.thread_id("max") == "thread-old"
            assert store.list_messages(target="max") == messages_before
            assert store.events(message["message_id"]) == deliveries_before
            release.wait(timeout=2)
            thread.join(timeout=2)
    finally:
        if thread.is_alive():
            release.abort()
            thread.join(timeout=2)
        HotJoinStore.clear_thread_id = original_clear
        cli.spawn_loop, cli.os.kill, cli.os.killpg = originals

    assert not thread.is_alive()
    assert "reset_error" not in outcomes
    assert outcomes["reset"]["state"] == "cleared"
    assert store.thread_id("max") is None
    assert store.list_messages(target="max") == messages_before
    assert store.events(message["message_id"]) == deliveries_before
    assert spawn_calls == []
    assert signals == []


@pytest.mark.parametrize("action", ["reset", "rotate"])
def test_pid_registration_window_is_locked_against_rotation(
    tmp_path: Path, action: str
):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-old")
    message = store.enqueue(
        target="max",
        body="must survive pid registration race",
        client_id=f"pid-registration-{action}",
    )
    ledger_before = _mailbox_snapshot(store, message["message_id"])
    entered = threading.Barrier(2)
    release = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    signals: list[tuple[Any, ...]] = []
    record = _fake_registered_pid(worker, pid=434_343)
    original_write = cli._write_pid_record
    originals = (
        cli.spawn_loop,
        cli._capture_pid_record,
        cli._write_pid_record,
        cli._pid_record_is_live,
        cli.os.kill,
        cli.os.killpg,
    )

    def blocked_write(wl, captured) -> None:
        entered.wait(timeout=2)
        release.wait(timeout=2)
        original_write(wl, captured)

    def start() -> None:
        try:
            outcomes["start"] = cli.do_start("P/max")
        except BaseException as exc:  # surfaced in the asserting thread
            outcomes["start_error"] = exc

    cli.spawn_loop = lambda _worker_dir: _FakeSpawnProcess(int(record["pid"]))
    cli._capture_pid_record = lambda _wl, _pid: record
    cli._write_pid_record = blocked_write
    cli._pid_record_is_live = lambda _record: True
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    thread = threading.Thread(target=start)
    try:
        with _agents_root(root):
            thread.start()
            entered.wait(timeout=2)
            assert not (worker / ".pid").exists()
            with pytest.raises(SystemExit, match="lifecycle lock is busy"):
                _remove_mapping(action, "P/max", "thread-old")
            assert store.thread_id("max") == "thread-old"
            assert _mailbox_snapshot(store, message["message_id"]) == ledger_before
            release.wait(timeout=2)
            thread.join(timeout=2)
    finally:
        if thread.is_alive():
            try:
                release.abort()
            except threading.BrokenBarrierError:
                pass
            thread.join(timeout=2)
        (
            cli.spawn_loop,
            cli._capture_pid_record,
            cli._write_pid_record,
            cli._pid_record_is_live,
            cli.os.kill,
            cli.os.killpg,
        ) = originals

    assert not thread.is_alive()
    assert "start_error" not in outcomes
    assert outcomes["start"] == [{"worker": "max", "result": "started"}]
    assert (worker / ".pid").exists()
    assert store.thread_id("max") == "thread-old"
    assert signals == []


@pytest.mark.parametrize("action", ["reset", "rotate"])
def test_live_pid_rotation_rejection_holds_lock_and_preserves_all_ledgers(
    tmp_path: Path, action: str
):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-live")
    message = store.enqueue(
        target="max",
        body="must survive live rejection",
        client_id=f"live-reject-{action}",
    )
    ledger_before = _mailbox_snapshot(store, message["message_id"])
    record = _fake_registered_pid(worker, pid=444_444)
    cli._write_pid_record(cli.L.WorkerLayout(worker), record)
    signals: list[tuple[Any, ...]] = []
    originals = (cli._pid_record_is_live, cli.os.kill, cli.os.killpg)

    def report_live(observed: dict[str, Any]) -> bool:
        assert observed == record
        _assert_worker_lock_held(worker)
        return True

    cli._pid_record_is_live = report_live
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    try:
        with _agents_root(root), pytest.raises(SystemExit, match="worker is still live"):
            _remove_mapping(action, "P/max", "thread-live")
    finally:
        cli._pid_record_is_live, cli.os.kill, cli.os.killpg = originals

    assert store.thread_id("max") == "thread-live"
    assert _mailbox_snapshot(store, message["message_id"]) == ledger_before
    assert signals == []


def test_reset_unfinished_intent_rejection_preserves_mailbox_and_never_signals(
    tmp_path: Path,
):
    root = tmp_path / "agents"
    project = root / "P"
    worker = project / "workers" / "max"
    worker.mkdir(parents=True)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-pending")
    message = store.enqueue(
        target="max", body="survive pending reset", client_id="pending-reset"
    )
    intent = store.round_intent(
        "max",
        "thread-pending",
        prompt_sha256="a" * 64,
        requested_model="offline-model",
        requested_effort="low",
    )
    ledger_before = _mailbox_snapshot(store, message["message_id"])
    intent_before = store.get_round_intent(intent["client_id"])
    signals: list[tuple[Any, ...]] = []
    originals = (cli.os.kill, cli.os.killpg)
    cli.os.kill = lambda *args: signals.append(args)
    cli.os.killpg = lambda *args: signals.append(args)
    try:
        with _agents_root(root), pytest.raises(
            SystemExit, match="unfinished paid-turn intent"
        ):
            cli.do_reset_thread("P/max", expected_thread_id="thread-pending")
    finally:
        cli.os.kill, cli.os.killpg = originals

    assert store.thread_id("max") == "thread-pending"
    assert store.get_round_intent(intent["client_id"]) == intent_before
    assert _mailbox_snapshot(store, message["message_id"]) == ledger_before
    assert signals == []


def test_existing_thread_event_ledger_migrates_rotation_action_without_event_loss(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-before-migration")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "ALTER TABLE worker_thread_events RENAME TO worker_thread_events_new"
        )
        db.execute(
            "CREATE TABLE worker_thread_events ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,target TEXT NOT NULL,"
            "action TEXT NOT NULL CHECK(action IN ('set','cleared')),"
            "thread_id TEXT NOT NULL,detail TEXT,created_ns INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO worker_thread_events SELECT * FROM worker_thread_events_new"
        )
        db.execute("DROP TABLE worker_thread_events_new")
        db.commit()

    migrated = HotJoinStore(project)
    assert [(row["seq"], row["action"]) for row in migrated.thread_events("max")] == [
        (1, "set")
    ]
    result = migrated.rotate_thread_id(
        "max",
        expected_thread_id="thread-before-migration",
        reason="oversize terminal history",
    )
    assert result["state"] == "rotated"
    assert [row["action"] for row in migrated.thread_events("max")] == [
        "set",
        "rotated",
    ]


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: cli.do_say("../escape", "outside"), id="say"),
        pytest.param(lambda: cli.do_messages("../escape"), id="messages"),
        pytest.param(lambda: cli.do_interrupt_turn("../escape"), id="interrupt"),
    ],
)
def test_cli_hotjoin_rejects_target_traversal_without_outside_mutation(
    tmp_path: Path, operation
):
    root = tmp_path / "agents"
    outside_worker = tmp_path / "workers" / "escape"
    outside_worker.mkdir(parents=True)
    outside_sentinel = tmp_path / "sentinel"
    outside_sentinel.write_text("unchanged", encoding="utf-8")
    before = {
        path.relative_to(tmp_path): (
            path.is_dir(),
            path.read_bytes() if path.is_file() else None,
        )
        for path in tmp_path.rglob("*")
    }

    with _agents_root(root), pytest.raises(ValueError):
        operation()

    after = {
        path.relative_to(tmp_path): (
            path.is_dir(),
            path.read_bytes() if path.is_file() else None,
        )
        for path in tmp_path.rglob("*")
    }
    assert after == before


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: cli.do_assign("P/max", "changed"), id="assign"),
        pytest.param(lambda: cli.do_say("P/max", "changed"), id="say"),
        pytest.param(lambda: cli.do_messages("P/max"), id="messages"),
        pytest.param(lambda: cli.do_interrupt_turn("P/max"), id="interrupt"),
        pytest.param(lambda: cli.do_start("P/max"), id="start"),
        pytest.param(lambda: cli.do_status("P/max"), id="status"),
        pytest.param(lambda: cli.do_stop("P/max", force=True), id="stop"),
    ],
)
def test_public_worker_verbs_reject_symlink_escape_without_outside_mutation(
    tmp_path: Path, operation
):
    root = tmp_path / "agents"
    real_worker = root / "P" / "workers" / "max"
    real_worker.mkdir(parents=True)
    outside = tmp_path / "outside-worker"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    shutil.rmtree(real_worker)
    real_worker.symlink_to(outside, target_is_directory=True)

    with _agents_root(root), pytest.raises(ValueError, match="unsafe worker directory"):
        operation()

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    with _agents_root(root):
        assert "max" not in cli.L.list_workers("P")
        assert cli.L.target_worker_dirs("P") == []


def test_worker_protocol_preflight_failure_starts_zero_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from danus.execution import loop as worker_loop

    project, worker_dir = _project(tmp_path)
    del project
    worker = worker_loop.L.WorkerLayout(worker_dir)
    log = worker.dir / "preflight-failure.log"
    starts: list[object] = []
    monkeypatch.setattr(worker_loop, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(worker_loop.codex, "resolve_bin", lambda: sys.executable)
    monkeypatch.setattr(
        worker_loop,
        "preflight_app_server",
        lambda *_a, **_k: (_ for _ in ()).throw(ProtocolError("incompatible")),
    )
    monkeypatch.setattr(
        worker_loop.AppServerClient,
        "start",
        lambda _self: starts.append("paid-process"),
    )
    rc = worker_loop.run_round_app_server(
        worker,
        {"MODEL": "never-called", "REASONING_EFFORT": "low"},
        "never sent",
        log,
        hard_timeout=1,
    )
    assert rc == 126
    assert starts == []
    assert "app-server protocol unavailable" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "constant", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"]
)
def test_strict_json_rejects_nonfinite_numbers_and_boolean_rpc_ids(
    tmp_path: Path, constant: str
):
    with pytest.raises(ProtocolError, match="non-finite"):
        hotjoin_module._strict_json(f'{{"value":{constant}}}'.encode())

    client = AppServerClient([sys.executable], cwd=tmp_path)
    with pytest.raises(ProtocolError, match="non-boolean integer"):
        client._dispatch({"id": True, "result": {}})
    with pytest.raises(ProtocolError, match="non-boolean integer"):
        client._dispatch({"id": "server-request", "method": "ask", "params": {}})
    with pytest.raises(ProtocolError, match="exactly one"):
        client._dispatch({"id": 1, "result": {}, "error": {"code": 1}})
    with pytest.raises(ProtocolError, match="exactly one"):
        client._dispatch({"id": 1, "result": {}, "method": "warning"})


def test_strict_json_and_identity_paths_reject_lone_surrogates(tmp_path: Path):
    with pytest.raises(ProtocolError, match="non-UTF-8"):
        hotjoin_module._strict_json(b'{"threadId":"\\ud800"}')
    client = AppServerClient([sys.executable], cwd=tmp_path)
    with pytest.raises(ProtocolError, match="valid UTF-8"):
        client._dispatch(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "\ud800",
                    "turn": {"id": "turn-1", "status": "inProgress"},
                },
            }
        )


def test_thread_runtime_attestation_rejects_invalid_platform_paths(
    tmp_path: Path,
):
    from danus.execution import loop as worker_loop

    with pytest.raises(ProtocolError, match="exact worker cwd"):
        worker_loop._attest_thread_runtime(
            {
                "thread": {"id": "thread-1", "cwd": str(tmp_path)},
                "model": "offline-model",
                "cwd": "/\x00",
                "approvalPolicy": "never",
                "sandbox": {
                    "type": "workspaceWrite",
                    "networkAccess": False,
                    "writableRoots": [],
                },
                "runtimeWorkspaceRoots": [],
            },
            worker_dir=tmp_path,
            requested_model="offline-model",
        )


def test_token_usage_projection_drops_secret_extras_and_rejects_bad_counts(
    tmp_path: Path,
):
    client = AppServerClient([sys.executable], cwd=tmp_path)
    breakdown = {
        "cachedInputTokens": 1,
        "inputTokens": 2,
        "outputTokens": 3,
        "reasoningOutputTokens": 1,
        "totalTokens": 5,
        "cacheWriteInputTokens": 0,
        "apiKey": "TOKEN_USAGE_SECRET_CANARY",
    }
    notification = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "last": dict(breakdown),
                "total": dict(breakdown),
                "modelContextWindow": 1000,
                "clientSecret": "TOKEN_USAGE_SECRET_CANARY",
            },
        },
    }
    client._dispatch(notification)
    projected = client.token_usage("thread-1", "turn-1")
    assert projected is not None
    assert set(projected) == {"last", "total", "modelContextWindow"}
    assert set(projected["last"]) == {
        "cachedInputTokens",
        "inputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "totalTokens",
        "cacheWriteInputTokens",
    }
    assert "TOKEN_USAGE_SECRET_CANARY" not in json.dumps(client.notifications())
    from danus.execution import loop as worker_loop

    audit = worker_loop._build_app_server_audit(
        client,
        thread_id="thread-1",
        turn_id="turn-1",
        terminal={"id": "turn-1", "status": "completed", "items": []},
        requested_model="offline-model",
        requested_effort="low",
        actual_model="offline-model",
        thread_reasoning_effort="low",
    )
    assert "TOKEN_USAGE_SECRET_CANARY" not in audit
    assert json.loads(audit.splitlines()[0])["token_usage"] == projected

    malformed = json.loads(json.dumps(notification))
    malformed["params"]["tokenUsage"]["last"]["inputTokens"] = True
    with pytest.raises(ProtocolError, match="non-negative integer"):
        client._dispatch(malformed)
    assert client.token_usage("thread-1", "turn-1") == projected


def test_turn_notifications_and_direct_history_reject_unknown_status(
    tmp_path: Path,
):
    client = AppServerClient([sys.executable], cwd=tmp_path)
    with pytest.raises(ProtocolError, match="non-active status"):
        client._dispatch(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )
    with pytest.raises(ProtocolError, match="non-terminal status"):
        client._dispatch(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "mystery"},
                },
            }
        )
    with pytest.raises(ProtocolError, match="malformed turn"):
        hotjoin_module.message_turn(
            {
                "thread": {
                    "id": "thread-1",
                    "turns": [{"id": "turn-1", "items": [], "status": "mystery"}],
                }
            },
            "client-id",
            expected_thread_id="thread-1",
        )


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "turn/started",
            {"threadId": "", "turn": {"id": "turn-1", "status": "inProgress"}},
        ),
        (
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "x" * 513, "status": "completed"},
            },
        ),
        (
            "thread/tokenUsage/updated",
            {"threadId": "x" * 513, "turnId": "turn-1", "tokenUsage": {}},
        ),
        (
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "userMessage", "clientId": ""},
            },
        ),
        (
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "userMessage", "clientId": "x" * 513},
            },
        ),
    ],
)
def test_notification_identities_are_nonempty_and_bounded(
    tmp_path: Path, method: str, params: dict[str, object]
):
    client = AppServerClient([sys.executable], cwd=tmp_path)
    with pytest.raises(ProtocolError, match="nonempty|string|hard limit|exact id"):
        client._dispatch({"method": method, "params": params})


def test_reroute_and_runtime_display_metadata_is_redacted_before_bounding(
    tmp_path: Path,
):
    from danus.execution import loop as worker_loop
    from danus.redaction import redact_external_error

    client = AppServerClient([sys.executable], cwd=tmp_path)
    from_model = "Authorization: Bearer REROUTE_CANARY_123"
    to_model = "OPENAI_API_KEY=REROUTE_KEY_CANARY_456" + ("x" * 3000)
    client._dispatch(
        {
            "method": "model/rerouted",
            "params": {
                "fromModel": from_model,
                "reason": "highRiskCyberActivity",
                "threadId": "thread-1",
                "toModel": to_model,
                "turnId": "turn-1",
            },
        }
    )
    snapshot = client.model_reroutes("thread-1", "turn-1")
    serialized = json.dumps(snapshot)
    assert "REROUTE_CANARY_123" not in serialized
    assert "REROUTE_KEY_CANARY_456" not in serialized
    expected_safe = redact_external_error(to_model)
    expected_raw = expected_safe.encode("utf-8")
    assert snapshot["events"][0]["toModel"] == (
        expected_safe
        if len(expected_raw) <= hotjoin_module.MAX_MODEL_REROUTE_FIELD_BYTES
        else {
            "omitted": True,
            "bytes": len(expected_raw),
            "sha256": hashlib.sha256(expected_raw).hexdigest(),
        }
    )
    assert "REROUTE_CANARY_123" not in json.dumps(client.notifications())
    assert "REROUTE_KEY_CANARY_456" not in json.dumps(client.notifications())

    audit = worker_loop._build_app_server_audit(
        client,
        thread_id="thread-1",
        turn_id="turn-1",
        terminal={"id": "turn-1", "status": "completed", "items": []},
        requested_model="offline-model",
        requested_effort="low",
        actual_model="Authorization: Digest ACTUAL_MODEL_CANARY",
        thread_reasoning_effort="api_key=EFFORT_CANARY",
    )
    assert "REROUTE_CANARY_123" not in audit
    assert "REROUTE_KEY_CANARY_456" not in audit
    assert "ACTUAL_MODEL_CANARY" not in audit
    assert "EFFORT_CANARY" not in audit
    header = json.loads(audit.splitlines()[0])
    assert header["thread_id"] == "thread-1"
    assert header["turn_id"] == "turn-1"


def test_direct_history_ignores_nested_tool_spoof_and_optional_null_client_id(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="never infer delivery from tool JSON")
    claimed = store.claim(
        target="max",
        owner="dead",
        allow_queued=True,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert claimed is not None
    store.record(
        message["message_id"],
        "delivery_unknown",
        thread_id="thread-1",
        turn_id="turn-1",
        expected_owner=claimed["claim_owner"],
    )
    client_id = HotJoinBroker.client_message_id(message["message_id"])
    history = {
        "thread": {
            "id": "thread-1",
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {"type": "userMessage", "clientId": None},
                        {
                            "type": "mcpToolCall",
                            "result": {
                                "structuredContent": {
                                    "type": "userMessage",
                                    "clientId": client_id,
                                }
                            },
                        },
                    ],
                }
            ],
        }
    }
    broker = HotJoinBroker(
        store,
        _StubClient(read_payload=history),
        target="max",
        thread_id="thread-1",
    )
    broker.reconcile_routing()
    assert store.get(message["message_id"])["state"] == "delivery_unknown"

    history["thread"]["turns"][0]["items"].append(
        {"type": "userMessage", "clientId": client_id}
    )
    assert hotjoin_module.message_turn(
        history, client_id, expected_thread_id="thread-1"
    ) == ("turn-1", "completed")


def test_thread_rotation_never_rebinds_old_delivery_provenance(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    store.set_thread_id("max", "thread-old")
    message = store.enqueue(target="max", body="old-thread provenance")
    claimed = store.claim(
        target="max", owner="old", allow_queued=True, thread_id="thread-old"
    )
    assert claimed is not None
    store.record(
        message["message_id"],
        "queued",
        thread_id="thread-old",
        expected_owner=claimed["claim_owner"],
    )
    before = store.get(message["message_id"])
    store.rotate_thread_id(
        "max", expected_thread_id="thread-old", reason="terminal history"
    )
    store.set_thread_id("max", "thread-new")

    assert store.claim(
        target="max", owner="new", allow_queued=True, thread_id="thread-new"
    ) is None
    with pytest.raises(StaleClaim):
        store.record(
            message["message_id"],
            "failed",
            thread_id="thread-new",
            expected_state="queued",
        )
    after = store.get(message["message_id"])
    assert after["thread_id"] == "thread-old"
    assert after["state"] == before["state"] == "queued"
    assert store.events(message["message_id"])[-1]["thread_id"] == "thread-old"


def test_broker_stop_timeout_retains_live_thread_handle_for_second_join(
    tmp_path: Path,
):
    project, _worker = _project(tmp_path)
    broker = HotJoinBroker(
        HotJoinStore(project),
        _StubClient(),
        target="max",
        thread_id="thread-1",
    )
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    broker._thread = thread
    try:
        assert broker.stop(timeout=0.01) is False
        assert broker._thread is thread
        assert thread.is_alive()
        assert isinstance(broker.error, TimeoutError)
        release.set()
        assert broker.stop(timeout=1) is True
        assert broker._thread is None
    finally:
        release.set()
        thread.join(timeout=1)


def test_rpc_error_redaction_is_idempotent_and_delivery_ledger_never_stores_secrets(
    tmp_path: Path,
):
    raw_error = {
        "message": "Authorization: Bearer CANARY_BEARER_1",
        "apiKey": "CANARY_API_KEY_2",
        "nested": {
            "access-token": "CANARY_ACCESS_3",
            "detail": "password=CANARY_PASSWORD_4&safe=yes",
        },
    }
    safe = hotjoin_module.redact_external_error(raw_error)
    assert hotjoin_module.redact_external_error(safe) == safe
    for canary in (
        "CANARY_BEARER_1",
        "CANARY_API_KEY_2",
        "CANARY_ACCESS_3",
        "CANARY_PASSWORD_4",
    ):
        assert canary not in safe

    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    message = store.enqueue(target="max", body="route once", fallback="queue")

    class SecretReject(_StubClient):
        def rpc(self, method: str, params: dict[str, Any], timeout: float = 30) -> Any:
            self.calls.append((method, params))
            raise RpcError(method, raw_error)

    broker = HotJoinBroker(
        store, SecretReject(), target="max", thread_id="thread-1"
    )
    broker.start()
    try:
        _wait_state(store, message["message_id"], "queued")
    finally:
        broker.stop()
    persisted = json.dumps(
        {
            "row": store.get(message["message_id"]),
            "events": store.events(message["message_id"]),
        }
    )
    assert "<redacted>" in persisted
    for canary in (
        "CANARY_BEARER_1",
        "CANARY_API_KEY_2",
        "CANARY_ACCESS_3",
        "CANARY_PASSWORD_4",
    ):
        assert canary not in persisted


def test_owner_recovery_reasons_are_redacted_before_any_sqlite_write(tmp_path: Path):
    project, _worker = _project(tmp_path)
    store = HotJoinStore(project)
    canaries = (
        "OWNER_BEARER_CANARY",
        "OWNER_API_CANARY",
        "OWNER_ACCESS_CANARY",
    )
    store.set_thread_id("max", "thread-prepared-secret")
    prepared = store.round_intent(
        "max",
        "thread-prepared-secret",
        prompt_sha256="e" * 64,
        requested_model="model",
        requested_effort="low",
    )
    store.cancel_prepared_round_intent(
        target="max",
        thread_id="thread-prepared-secret",
        client_id=prepared["client_id"],
        reason=(
            "Authorization: Bearer OWNER_BEARER_CANARY "
            "OPENAI_API_KEY=OWNER_API_CANARY"
        ),
    )
    store.rotate_thread_id(
        "max",
        expected_thread_id="thread-prepared-secret",
        reason="DANUS_ACCESS_TOKEN=OWNER_ACCESS_CANARY",
    )

    store.set_thread_id("other", "thread-ambiguous-secret")
    ambiguous = store.round_intent(
        "other",
        "thread-ambiguous-secret",
        prompt_sha256="f" * 64,
        requested_model="model",
        requested_effort="high",
    )
    store.record_round_intent(
        ambiguous["client_id"], "dispatching", expected_states={"prepared"}
    )
    store.abandon_round_intent(
        target="other",
        thread_id="thread-ambiguous-secret",
        client_id=ambiguous["client_id"],
        expected_state="dispatching",
        reason="api_key=OWNER_API_CANARY",
        acknowledge_paid_outcome_unknown=True,
    )
    serialized = json.dumps(
        {
            "operators": store.round_operator_events(),
            "threads": store.thread_events("max"),
        }
    )
    assert "<redacted>" in serialized
    for canary in canaries:
        assert canary not in serialized
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(store.path) + suffix)
            if path.exists():
                assert canary.encode() not in path.read_bytes()
