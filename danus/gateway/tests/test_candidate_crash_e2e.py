"""Offline subprocess crash-cut coverage for the reasoning-first candidate lane.

These tests deliberately kill a real gateway subprocess only after a durable
operation has returned.  The verifier itself is a local in-process fake: it
records every launch durably, never opens a socket, and never invokes a model.
Coordination, FactGraph, global-memory, reopen, and owner resolution all use the
production implementations.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from danus.coordination import CoordinationStore
from danus.core import FactGraph, GlobalMemory
from danus.gateway import server
from danus.orchestration import cli


_CHILD = r"""
import json
import os
import signal
from pathlib import Path

from danus.coordination import CoordinationStore
from danus.core import FactGraph
from danus.gateway import server


cut = os.environ["DANUS_TEST_CANDIDATE_CUT"]
counter = Path(os.environ["DANUS_TEST_LAUNCH_COUNTER"])
statement = os.environ["DANUS_TEST_STATEMENT"]
proof = os.environ["DANUS_TEST_PROOF"]


def barrier() -> None:
    print(json.dumps({"barrier": cut}), flush=True)
    while True:
        signal.pause()


def record_launch() -> None:
    with counter.open("a", encoding="utf-8") as stream:
        stream.write("launch\n")
        stream.flush()
        os.fsync(stream.fileno())


def verifier_response(context):
    return {
        "output_schema_version": 3,
        "verification_status": "final",
        "verification_report": {
            "summary": "offline subprocess verifier",
            "critical_errors": [],
            "gaps": [],
        },
        "verdict": "correct",
        "needs_expanded_proofs": [],
        "repair_hints": "",
        "verification_context_digest": context["digest"],
    }


def fake_local_verifier(
    candidate_statement,
    candidate_proof,
    fact_context=None,
    glossary_introduces=None,
):
    del candidate_statement, candidate_proof, glossary_introduces
    record_launch()
    if cut == "verifier_response_lost":
        raise OSError("verifier accepted the request, but its response was lost")
    return verifier_response(fact_context)


server._verify = fake_local_verifier

if cut == "candidate_registered":
    original_register = CoordinationStore.register_candidate

    def register_then_cut(self, *args, **kwargs):
        result = original_register(self, *args, **kwargs)
        barrier()
        return result

    CoordinationStore.register_candidate = register_then_cut
elif cut == "fact_added":
    original_add = FactGraph.add_if_context_unchanged

    def add_then_cut(self, *args, **kwargs):
        result = original_add(self, *args, **kwargs)
        barrier()
        return result

    FactGraph.add_if_context_unchanged = add_then_cut
elif cut in {"verifier_response_lost", "candidate_terminalized"}:
    original_terminalize = CoordinationStore.terminalize_candidate
    target_outcome = (
        "outcome_unknown" if cut == "verifier_response_lost" else "correct"
    )

    def terminalize_then_cut(self, *args, **kwargs):
        result = original_terminalize(self, *args, **kwargs)
        if kwargs.get("outcome") == target_outcome:
            barrier()
        return result

    CoordinationStore.terminalize_candidate = terminalize_then_cut
else:
    raise RuntimeError(f"unknown candidate cut: {cut}")

result = server.fact_submit(statement=statement, proof=proof)
print(json.dumps({"unexpected_result": result}, sort_keys=True), flush=True)
raise SystemExit(86)
"""


def _new_active_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> tuple[Path, CoordinationStore, Any]:
    contract = tmp_path / "worker.md"
    contract.write_text("# offline worker contract\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    agents_root = tmp_path / "agents"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(agents_root))
    monkeypatch.setenv("DANUS_WORKER_CONTRACT", str(contract))
    monkeypatch.setenv("DANUS_WORKER_SKILLS", str(skills))
    monkeypatch.setenv("DANUS_WORKER_TRANSPORT", "exec")
    created = cli.do_new(name, roles="high:1", model="offline-model")
    project = Path(created["project_dir"])
    store = CoordinationStore.open_existing(project)
    assert store is not None
    admission = store.admit("high")
    assert admission is not None
    store.pin_prompt(admission.slot_id, admission.directive)
    active = store.activate(admission.slot_id)

    monkeypatch.setenv("DANUS_PROJECT_DIR", str(project))
    monkeypatch.setenv("DANUS_AUTHOR", "high")
    monkeypatch.setenv("DANUS_ROLE", "worker")
    monkeypatch.setenv("DANUS_PROBLEM_ID", name)
    monkeypatch.setenv("DANUS_VERIFY_URL", "http://offline.invalid/verify")
    monkeypatch.setenv("DANUS_VERIFY_CONTEXT_MAX_CHARS", "200000")
    monkeypatch.setenv("DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS", "200000")
    monkeypatch.setenv("DANUS_HOTJOIN_ENABLED", "0")
    return project, store, active


def _launch_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _record_launch(path: Path) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write("launch\n")
        stream.flush()
        os.fsync(stream.fileno())


def _correct_response(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "output_schema_version": 3,
        "verification_status": "final",
        "verification_report": {
            "summary": "offline parent verifier",
            "critical_errors": [],
            "gaps": [],
        },
        "verdict": "correct",
        "needs_expanded_proofs": [],
        "repair_hints": "",
        "verification_context_digest": context["digest"],
    }


def _counting_verifier(path: Path) -> Callable[..., dict[str, Any]]:
    def verify(
        statement: str,
        proof: str,
        fact_context: dict[str, Any] | None = None,
        glossary_introduces: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del statement, proof, glossary_introduces
        assert fact_context is not None
        _record_launch(path)
        return _correct_response(fact_context)

    return verify


def _must_not_verify(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise AssertionError("an exact crash retry must not launch the verifier")


def _kill_at_barrier(
    *,
    cut: str,
    counter: Path,
    statement: str,
    proof: str,
    timeout: float = 15.0,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DANUS_TEST_CANDIDATE_CUT": cut,
            "DANUS_TEST_LAUNCH_COUNTER": str(counter),
            "DANUS_TEST_STATEMENT": statement,
            "DANUS_TEST_PROOF": proof,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    observed: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=2)
                pytest.fail(
                    "crash child exited before its durable barrier: "
                    f"rc={process.returncode}, stdout={observed!r}{stdout!r}, "
                    f"stderr={stderr!r}"
                )
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select(
                [process.stdout], [], [], min(0.1, remaining)
            )
            if not readable:
                continue
            line = process.stdout.readline()
            if not line:
                continue
            observed.append(line)
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload == {"barrier": cut}:
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                assert process.returncode == -signal.SIGKILL
                assert process.stderr.read() == ""
                return
        pytest.fail(f"timed out waiting for crash barrier {cut!r}: {observed!r}")
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _assert_full_identity(candidate: dict[str, Any]) -> None:
    identity = candidate["candidate_fact_identity"]
    assert isinstance(identity, str)
    assert len(identity) == 64
    assert set(identity) <= set("0123456789abcdef")


def test_register_commit_survives_sigkill_before_first_verifier_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _store, active = _new_active_project(
        tmp_path, monkeypatch, "candidate-register-cut"
    )
    counter = tmp_path / "register-launches.log"
    statement = "Candidate registration is durable before verifier launch"
    proof = "This offline proof exercises the candidate registration crash cut."

    _kill_at_barrier(
        cut="candidate_registered",
        counter=counter,
        statement=statement,
        proof=proof,
    )

    reopened = CoordinationStore.open_existing(project)
    assert reopened is not None
    candidate = reopened.project_status()["candidate"]
    assert candidate is not None
    assert candidate["state"] == "active"
    assert candidate["slot_id"] == active.slot_id
    _assert_full_identity(candidate)
    assert _launch_count(counter) == 0
    assert FactGraph(project).list() == []

    monkeypatch.setattr(server, "_verify", _counting_verifier(counter))
    retry = server.fact_submit(statement=statement, proof=proof)

    assert _launch_count(counter) == 1
    assert retry["verification_calls"] == 1
    assert retry["candidate_receipt_id"] == candidate["candidate_receipt_id"]
    assert retry["candidate_outcome"] == "correct"
    terminal = reopened.candidate_entry(candidate["candidate_receipt_id"])
    assert terminal is not None
    assert terminal["candidate_receipt_id"] == candidate["candidate_receipt_id"]
    assert terminal["slot_id"] == candidate["slot_id"]
    assert terminal["state"] == "terminal"
    assert terminal["candidate_fact_identity"] == candidate["candidate_fact_identity"]
    assert reopened.project_status()["candidate"] is None


def test_lost_verifier_response_is_unknown_and_exact_retry_never_relaunches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_name = "candidate-response-lost"
    project, _store, active = _new_active_project(tmp_path, monkeypatch, project_name)
    counter = tmp_path / "response-lost-launches.log"
    statement = "One accepted verifier request has an unknowable response"
    proof = "The transport is cut after the local verifier accepts this request."

    _kill_at_barrier(
        cut="verifier_response_lost",
        counter=counter,
        statement=statement,
        proof=proof,
    )

    reopened = CoordinationStore.open_existing(project)
    assert reopened is not None
    candidate = reopened.project_status()["candidate"]
    assert candidate is not None
    assert candidate["state"] == "outcome_unknown"
    assert candidate["outcome"] == "outcome_unknown"
    _assert_full_identity(candidate)
    assert _launch_count(counter) == 1
    assert FactGraph(project).list() == []
    assert GlobalMemory(project).read("verification") == []

    monkeypatch.setattr(server, "_verify", _must_not_verify)
    retry = server.fact_submit(statement=statement, proof=proof)
    assert retry["accepted"] is False
    assert retry["verification_calls"] == 0
    assert retry["candidate_receipt_id"] == candidate["candidate_receipt_id"]
    assert retry["candidate_outcome"] == "outcome_unknown"
    assert "already outcome_unknown" in retry["error"]
    assert _launch_count(counter) == 1

    receipt = candidate["candidate_receipt_id"]
    with pytest.raises(SystemExit, match="acknowledge-paid-outcome-unknown"):
        cli.do_resolve_candidate(
            project_name,
            receipt,
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=False,
        )
    wrong_receipt = "0" * 64 if receipt != "0" * 64 else "1" * 64
    with pytest.raises(SystemExit, match="receipt does not exist"):
        cli.do_resolve_candidate(
            project_name,
            wrong_receipt,
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=True,
        )
    with pytest.raises(SystemExit, match="source slot is not terminal"):
        cli.do_resolve_candidate(
            project_name,
            receipt,
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=True,
        )

    reopened.mark_ambiguous(active.slot_id)
    with pytest.raises(SystemExit, match="source slot is not terminal"):
        cli.do_resolve_candidate(
            project_name,
            receipt,
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=True,
        )
    reopened.complete(active.slot_id, outcome="terminal_rc_137")
    generation_before_resolution = reopened.project_status()["generation"]
    resolved = cli.do_resolve_candidate(
        project_name,
        receipt,
        outcome="known-no-promotion",
        acknowledge_paid_outcome_unknown=True,
    )
    assert resolved["candidate_receipt_id"] == receipt
    assert resolved["slot_id"] == candidate["slot_id"]
    assert resolved["candidate_fact_identity"] == candidate["candidate_fact_identity"]
    assert resolved["state"] == "terminal"
    assert resolved["owner_resolution"] == "known_no_promotion"
    assert resolved["owner_acknowledged_unknown"] is True
    assert resolved["candidate_fact_active_at_resolution"] is False
    assert reopened.project_status()["candidate"] is None
    assert reopened.project_status()["generation"] == generation_before_resolution + 1
    replay = cli.do_resolve_candidate(
        project_name,
        receipt,
        outcome="known-no-promotion",
        acknowledge_paid_outcome_unknown=True,
    )
    assert replay == resolved
    with pytest.raises(SystemExit, match="owner resolution conflicts"):
        cli.do_resolve_candidate(
            project_name,
            receipt,
            outcome="abandon-unknown",
            acknowledge_paid_outcome_unknown=True,
        )
    assert reopened.admit("high") is not None


def test_durable_fact_add_cut_retries_same_identity_without_second_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _store, _active = _new_active_project(
        tmp_path, monkeypatch, "candidate-fact-add-cut"
    )
    counter = tmp_path / "fact-add-launches.log"
    statement = "A verified fact survives a crash before candidate terminalization"
    proof = "The exact promoted record is durable before the process is killed."

    _kill_at_barrier(
        cut="fact_added",
        counter=counter,
        statement=statement,
        proof=proof,
    )

    reopened = CoordinationStore.open_existing(project)
    assert reopened is not None
    candidate = reopened.project_status()["candidate"]
    assert candidate is not None and candidate["state"] == "active"
    _assert_full_identity(candidate)
    graph = FactGraph(project)
    assert graph.list() == [candidate["candidate_fact_id"]]
    exact = graph.lookup_active_exact_identity(
        problem_id="candidate-fact-add-cut",
        predecessors=[],
        glossary_introduces={},
        statement=statement,
        proof=proof,
    )
    assert exact == (
        candidate["candidate_fact_id"],
        candidate["candidate_fact_identity"],
    )
    fact_path = graph._path(candidate["candidate_fact_id"])
    before = (fact_path.stat().st_mtime_ns, fact_path.read_bytes())
    assert _launch_count(counter) == 1

    monkeypatch.setattr(server, "_verify", _must_not_verify)
    retry = server.fact_submit(statement=statement, proof=proof)

    assert retry["verification_calls"] == 0
    assert retry["verification_reuse"] == "active_exact_fact"
    assert retry["fact_id"] == candidate["candidate_fact_id"]
    assert retry["candidate_receipt_id"] == candidate["candidate_receipt_id"]
    assert retry["candidate_outcome"] == "correct"
    assert _launch_count(counter) == 1
    assert (fact_path.stat().st_mtime_ns, fact_path.read_bytes()) == before
    terminal = reopened.candidate_entry(candidate["candidate_receipt_id"])
    assert terminal is not None
    assert terminal["candidate_receipt_id"] == candidate["candidate_receipt_id"]
    assert terminal["slot_id"] == candidate["slot_id"]
    assert terminal["state"] == "terminal"
    assert terminal["candidate_fact_identity"] == candidate["candidate_fact_identity"]
    assert reopened.project_status()["candidate"] is None


def test_terminal_candidate_commit_survives_sigkill_before_response_and_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _store, _active = _new_active_project(
        tmp_path, monkeypatch, "candidate-terminal-cut"
    )
    counter = tmp_path / "terminal-launches.log"
    statement = "Candidate terminalization is durable before gateway response"
    proof = "The candidate and fact commits precede the response and audit trace."

    _kill_at_barrier(
        cut="candidate_terminalized",
        counter=counter,
        statement=statement,
        proof=proof,
    )

    reopened = CoordinationStore.open_existing(project)
    assert reopened is not None
    assert reopened.project_status()["candidate"] is None
    candidates = reopened.list_candidates()
    assert len(candidates) == 1
    receipt = candidates[0]["candidate_id"]
    terminal = reopened.candidate_entry(receipt)
    assert terminal is not None
    assert terminal["state"] == "terminal"
    assert terminal["outcome"] == "correct"
    assert terminal["owner_resolution"] is None
    _assert_full_identity(terminal)
    graph = FactGraph(project)
    assert graph.list() == [terminal["candidate_fact_id"]]
    assert GlobalMemory(project).read("verification") == []
    assert _launch_count(counter) == 1

    monkeypatch.setattr(server, "_verify", _must_not_verify)
    retry = server.fact_submit(statement=statement, proof=proof)

    assert retry["verification_calls"] == 0
    assert retry["verification_reuse"] == "active_exact_fact"
    assert retry["fact_id"] == terminal["candidate_fact_id"]
    assert _launch_count(counter) == 1
    assert reopened.candidate_entry(receipt) == terminal
    traces = GlobalMemory(project).read("verification")
    assert len(traces) == 1
    assert traces[0]["verification_calls"] == 0
    assert traces[0]["verification_reuse"] == "active_exact_fact"


def test_post_add_crash_requires_abandon_for_the_exact_active_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_name = "candidate-active-fact-owner-cut"
    project, _store, active = _new_active_project(tmp_path, monkeypatch, project_name)
    counter = tmp_path / "active-fact-owner-launches.log"
    statement = "The crashed candidate already has its exact active fact"
    proof = "A durable fact makes known-no-promotion an invalid owner attestation."

    _kill_at_barrier(
        cut="fact_added",
        counter=counter,
        statement=statement,
        proof=proof,
    )

    reopened = CoordinationStore.open_existing(project)
    assert reopened is not None
    candidate = reopened.project_status()["candidate"]
    assert candidate is not None and candidate["state"] == "active"
    _assert_full_identity(candidate)
    graph = FactGraph(project)
    with graph.locked_active_fact_identity(
        candidate["candidate_fact_id"]
    ) as active_identity:
        assert active_identity == candidate["candidate_fact_identity"]
    assert _launch_count(counter) == 1

    reopened.mark_ambiguous(active.slot_id)
    with pytest.raises(SystemExit, match="source slot is not terminal"):
        cli.do_resolve_candidate(
            project_name,
            candidate["candidate_receipt_id"],
            outcome="abandon-unknown",
            acknowledge_paid_outcome_unknown=True,
        )
    reopened.complete(active.slot_id, outcome="terminal_rc_137")
    generation_before_resolution = reopened.project_status()["generation"]

    with pytest.raises(SystemExit, match="active candidate fact"):
        cli.do_resolve_candidate(
            project_name,
            candidate["candidate_receipt_id"],
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=True,
        )
    resolved = cli.do_resolve_candidate(
        project_name,
        candidate["candidate_receipt_id"],
        outcome="abandon-unknown",
        acknowledge_paid_outcome_unknown=True,
    )
    assert resolved["candidate_receipt_id"] == candidate["candidate_receipt_id"]
    assert resolved["slot_id"] == candidate["slot_id"]
    assert resolved["candidate_fact_identity"] == candidate["candidate_fact_identity"]
    assert resolved["state"] == "terminal"
    assert resolved["outcome"] == "outcome_unknown"
    assert resolved["owner_resolution"] == "abandon_unknown"
    assert resolved["owner_acknowledged_unknown"] is True
    assert resolved["candidate_fact_active_at_resolution"] is True
    assert reopened.project_status()["candidate"] is None
    assert reopened.project_status()["generation"] == generation_before_resolution + 1
    assert _launch_count(counter) == 1
    assert (
        cli.do_resolve_candidate(
            project_name,
            candidate["candidate_receipt_id"],
            outcome="abandon-unknown",
            acknowledge_paid_outcome_unknown=True,
        )
        == resolved
    )
    with pytest.raises(SystemExit, match="active candidate fact"):
        cli.do_resolve_candidate(
            project_name,
            candidate["candidate_receipt_id"],
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=True,
        )
    assert reopened.admit("high") is not None
