"""Tests for danus.gateway — role gating + tool wiring over danus.core.

The verify service is mocked (we replace ``server._verify``), so fact_submit is
exercised without a live verifier or codex. Config is read from the environment
at call time, so each test sets DANUS_* around a temp project dir.

Runs standalone (``python -m danus.gateway.tests.test_gateway``) and under pytest.
"""

from __future__ import annotations

import hashlib
import json
import io
import os
import select
import subprocess
import sys
import tempfile
import threading
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.coordination import (
    DEFAULT_COORDINATION,
    CoordinationStore,
    candidate_receipt_id,
    coordination_payload,
)
from danus.core import (
    FactGraph,
    FactPromotionOutcomeUnknown,
    GlobalMemory,
    canonical_global_memory_record,
    compute_fact_id,
)
from danus.core import glossary as _glossary
from danus.gateway import build_app, tools_for
from danus.gateway import server
from danus.hotjoin import HotJoinStore
from danus.strategy import browser_advisor as browser_advisor_module
from danus.strategy.browser_advisor import (
    BrowserAdvisorBroker,
    BrowserAdvisorConflict,
    BrowserAdvisorError,
    BrowserAdvisorStateError,
)


_TEST_VERIFIER_BUNDLE_DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def _supervisor_advisor_control_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use one trusted fence root outside every temporary project."""

    control_root = tmp_path.parent / f"advisor-control-{tmp_path.name}"
    monkeypatch.setattr(
        browser_advisor_module, "_canonical_control_root", lambda: control_root
    )


def _health_response(
    *,
    protocol=server.VERIFICATION_OUTPUT_PROTOCOL_VERSION,
    digest=_TEST_VERIFIER_BUNDLE_DIGEST,
    pid=1234,
    instance_nonce="0" * 32,
    include_contract=True,
):
    body = {
        "status": "ok",
        "pid": pid,
        "instance_nonce": instance_nonce,
    }
    if include_contract:
        body.update(
            output_protocol_version=protocol,
            verifier_bundle_digest=digest,
        )
    return io.BytesIO(json.dumps(body).encode("utf-8"))


def _active_reasoning_store(
    project: Path, *, workers: int = 1
) -> tuple[CoordinationStore, list]:
    project.mkdir()
    names = ["xhigh", "xhigh2"][:workers]
    metadata = {
        "name": project.name,
        "model": "model",
        "roles": f"xhigh:{workers}",
        "workers": names,
        "coordination": dict(DEFAULT_COORDINATION),
    }
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    store = CoordinationStore(project, metadata)
    for worker in names:
        store.stage_task_assignment(
            worker,
            f"# Gateway test task\n\nExact generation 1 assignment for {worker}.\n",
        )
    admissions = []
    for worker in names:
        admissions.append(_admit_and_activate(store, worker))
    return store, admissions


def _active_explorer_store(project: Path) -> tuple[CoordinationStore, object]:
    project.mkdir()
    workers = ["max", "max2", "high", "high2", "high3", "high4", "high5"]
    metadata = {
        "name": project.name,
        "model": "model",
        "roles": "max:2,high:5",
        "workers": workers,
        "coordination": coordination_payload(active_explorers=2),
    }
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    store = CoordinationStore(project, metadata)
    for worker in store.project_status()["task_staging"]["required_workers"]:
        store.stage_task_assignment(
            str(worker),
            f"# Gateway explorer task\n\nExact assignment for {worker}.\n",
        )
    return store, _admit_and_activate(store, "high")


def _bound_coordination_prompt(admission) -> str:
    return (
        f"{admission.directive}\n\n"
        f"coordination_slot_id={admission.slot_id}\n"
        f"generation={admission.generation}\n"
        f"task_sha256={admission.task_sha256}\n"
    )


def _admit_and_activate(store: CoordinationStore, worker: str):
    admission = store.admit(worker)
    assert admission is not None
    store.pin_prompt(admission.slot_id, _bound_coordination_prompt(admission))
    return store.activate(admission.slot_id)


def _stage_next_gateway_generation(store: CoordinationStore) -> None:
    status = store.project_status()
    assert status["phase"] == "owner_action_required"
    generation = int(status["generation"]) + 1
    for worker in (status["root_worker"], status["critic_worker"]):
        if isinstance(worker, str):
            store.stage_task_assignment(
                worker,
                (
                    "# Gateway test task\n\n"
                    f"Exact generation {generation} assignment for {worker}.\n"
                ),
            )


def _reasoning_recommendation(
    project: Path, *, complete_review: bool
) -> tuple[CoordinationStore, str, object]:
    store, admissions = _active_reasoning_store(project, workers=2)
    root, critic = admissions
    evidence = store.record_root_evidence(
        "xhigh",
        "obstacle",
        entry_id="checkpoint_root_obstacle",
        slot_id=root.slot_id,
    )
    store.complete(root.slot_id, outcome="terminal_rc_0")
    store.complete(critic.slot_id, outcome="terminal_rc_0")
    review = _admit_and_activate(store, "xhigh2")
    confirmation = store.confirm_root_evidence(
        "xhigh2",
        str(evidence["entry_id"]),
        entry_id="checkpoint_critic_confirmation",
        slot_id=review.slot_id,
    )
    recommendation_id = confirmation["recommendation_id"]
    assert isinstance(recommendation_id, str)
    if complete_review:
        store.complete(review.slot_id, outcome="terminal_rc_0")
        _stage_next_gateway_generation(store)
    return store, recommendation_id, review


def _submitted_browser_request(
    project: Path, *, prompt: str = "Bounded offline advisor question"
) -> tuple[BrowserAdvisorBroker, dict]:
    broker = BrowserAdvisorBroker(project)
    request = _prepare_browser_request(broker, prompt, context_id="gateway-race-cycle")
    broker.authorize(
        request["request_id"],
        prompt_sha256=request["prompt_sha256"],
        authorization_scope="Owner approved this exact offline test question.",
        acknowledge_external_transmission=True,
    )
    broker.dispatch_started(request["request_id"])
    broker.submitted(
        request["request_id"],
        observed_prompt_sha256=request["prompt_sha256"],
        ui_mode="Pro",
        full_prompt_observed=True,
        conversation_url="https://chatgpt.com/c/gateway-race-offline",
    )
    return broker, request


def _checkpoint_prompt(question: str) -> str:
    if question.startswith("## Verified facts\n"):
        return question
    return (
        "## Verified facts\n"
        "- No verified facts are needed for this offline broker-state test.\n\n"
        "## Failed routes and evidence\n"
        f"- {question}\n\n"
        "## Unresolved bottleneck\n"
        "The bounded browser-advisor transition remains under test.\n\n"
        "## Candidate decision question\n"
        f"{question}"
    )


def _prepare_browser_request(
    broker: BrowserAdvisorBroker,
    prompt: str,
    **kwargs,
) -> dict:
    exact_prompt = _checkpoint_prompt(prompt)
    memory = GlobalMemory(broker.project_dir)
    checkpoint_id = memory.append(
        "advisor_checkpoint",
        claim="Gateway offline browser checkpoint",
        evidence=exact_prompt,
        author="main_agent",
        links={"fact_ids": []},
    )
    raw = canonical_global_memory_record(
        memory.get_immutable_in_kind("advisor_checkpoint", checkpoint_id)
    )
    return broker.prepare(
        exact_prompt,
        checkpoint_id=checkpoint_id,
        checkpoint_sha256=hashlib.sha256(raw).hexdigest(),
        checkpoint_bytes=len(raw),
        **kwargs,
    )


def _record_browser_terminal(
    broker: BrowserAdvisorBroker,
    request: dict,
    *,
    target: str,
    response: str,
) -> dict:
    method = broker.complete if target == "completed" else broker.needs_input
    return method(
        request["request_id"],
        response=response,
        observed_prompt_sha256=request["prompt_sha256"],
        ui_mode="Pro",
        conversation_url="https://chatgpt.com/c/gateway-race-offline",
        stable_snapshots=2,
        completion_actions_observed=True,
        composer_available=True,
        working_indicator_absent=True,
    )


@contextmanager
def _env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _mock_verify(verdict, repair_hints="", raise_exc=None, capture=None):
    """Replace server._verify with a stub; restore after."""
    orig = server._verify

    def fake(statement, proof, fact_context=None, glossary_introduces=None):
        if capture is not None:
            capture.update(
                {
                    "statement": statement,
                    "proof": proof,
                    "fact_context": fact_context,
                    "glossary_introduces": glossary_introduces,
                }
            )
        if raise_exc is not None:
            raise raise_exc
        findings = (
            []
            if verdict == "correct"
            else [
                {
                    "location": "proof",
                    "issue": "mock rejection",
                    "candidate_evidence": {
                        "source": "proof",
                        "line": 1,
                        "exact_line": proof,
                    },
                }
            ]
        )
        return {
            "output_schema_version": 3,
            "verification_status": "final",
            "verdict": verdict,
            "needs_expanded_proofs": [],
            "repair_hints": repair_hints,
            "verification_context_digest": fact_context["digest"],
            "verification_report": {
                "summary": "mock",
                "critical_errors": [],
                "gaps": findings,
            },
        }

    server._verify = fake
    try:
        yield
    finally:
        server._verify = orig


def _verify_response(
    context,
    *,
    status="final",
    verdict="correct",
    requests=None,
    repair_hints="",
    candidate_proof="proof",
):
    findings = []
    if status == "final" and verdict == "wrong":
        findings = [
            {
                "location": "proof",
                "issue": "mock rejection",
                "candidate_evidence": {
                    "source": "proof",
                    "line": 1,
                    "exact_line": candidate_proof,
                },
            }
        ]
        repair_hints = repair_hints or "repair the mock gap"
    return {
        "output_schema_version": 3,
        "verification_status": status,
        "verification_report": {
            "summary": "mock",
            "critical_errors": [],
            "gaps": findings,
        },
        "verdict": verdict,
        "needs_expanded_proofs": list(requests or []),
        "repair_hints": repair_hints,
        "verification_context_digest": context["digest"],
    }


def test_role_table():
    # main can never fabricate a fact
    assert "fact_submit" not in tools_for("main")
    assert "fact_revoke" in tools_for("main")
    assert "fact_context" in tools_for("main")
    # verifier is read-only: literature lookup ONLY
    assert tools_for("verifier") == ["search_arxiv_theorems"]
    # worker is the only role that can submit a fact
    assert "fact_submit" in tools_for("worker")
    # all three get literature grounding; worker/main get lazy fact context.
    for r in ("worker", "main", "verifier"):
        assert "search_arxiv_theorems" in tools_for(r)
    for r in ("worker", "main"):
        assert "fact_context" in tools_for(r)
        assert "gm_get" in tools_for(r)
    assert "fact_context" not in tools_for("verifier")
    assert "gm_get" not in tools_for("verifier")
    # unknown / misconfigured role fails CLOSED to the read-only verifier set
    assert tools_for("nope") == tools_for("verifier")
    assert "fact_submit" not in tools_for("nope") and "gm_add" not in tools_for("nope")
    # build_app registers without error for every role
    for r in ("worker", "main", "verifier", "all"):
        assert build_app(r) is not None


def test_gateway_import_does_not_load_hotjoin_runtime():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import danus.gateway.server; "
            "assert 'danus.hotjoin' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("elapsed", [float("nan"), float("inf"), float("-inf")])
def test_verify_metrics_reject_nonfinite_elapsed_seconds(elapsed: float):
    context = {
        "digest": "d" * 64,
        "scope": {"expansion_round": 0, "expanded_proof_ids": []},
    }
    result = _verify_response(context)
    result["verification_metrics"] = {
        "model": "model",
        "effort": "high",
        "elapsed_seconds": elapsed,
        "tokens_used": 1,
        "context_round": 0,
        "expanded_proof_ids": [],
    }
    with pytest.raises(ValueError, match="elapsed_seconds"):
        server._validate_service_result(
            result,
            context,
            statement="statement",
            proof="proof",
        )


def test_gm_and_fact_search_over_temp_project():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="tester"),
    ):
        out = server.gm_add("plan", claim="reduce to q>=2", evidence="")
        assert out["kind"] == "plan" and out["id"]
        exact = server.gm_get(out["id"])
        assert exact["id"] == out["id"] and exact["claim"] == "reduce to q>=2"
        hits = server.gm_search("reduce")
        assert hits["results_by_kind"]["plan"]["count"] == 1
        with pytest.raises(ValueError, match="unknown"):
            server.gm_get("f" * 16)
        # fact_search over an empty graph is well-formed
        assert server.fact_search("anything")["results"] == []


def test_reasoning_first_gm_add_requires_fresh_exact_designated_review(
    tmp_path: Path,
):
    project = tmp_path / "coordinated-gateway"
    project.mkdir()
    metadata = {
        "name": project.name,
        "model": "model",
        "roles": "xhigh:2",
        "workers": ["xhigh", "xhigh2"],
        "coordination": dict(DEFAULT_COORDINATION),
    }
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    store = CoordinationStore(project, metadata)
    for worker in ("xhigh", "xhigh2"):
        store.stage_task_assignment(
            worker,
            f"# Gateway test task\n\nExact generation assignment for {worker}.\n",
        )
    root = _admit_and_activate(store, "xhigh")
    critic = _admit_and_activate(store, "xhigh2")

    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh",
        DANUS_ROLE="worker",
    ):
        with pytest.raises(ValueError, match="protected canonical provenance"):
            server.gm_add(
                "obstacle",
                claim="forged slot must not persist",
                links={
                    "coordination": {
                        "slot_id": critic.slot_id,
                        "generation": critic.generation,
                        "lane": "critic",
                    }
                },
            )
        root_entry = server.gm_add("dead_end", claim="root route is exhausted")

    obstacle_path = project / "global_memory" / "obstacle.jsonl"
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh2",
        DANUS_ROLE="worker",
    ):
        with pytest.raises(RuntimeError, match="exact designated review slot"):
            server.gm_add(
                "obstacle",
                claim="generic critic pre-names a future root entry",
                links={"confirms_entry_id": root_entry["id"]},
            )
    assert not obstacle_path.exists()
    assert store.project_status()["recommendation"] is None

    stored_root = GlobalMemory(project).read("dead_end")[-1]
    assert root_entry == {"id": stored_root["id"], "kind": "dead_end"}
    assert stored_root["links"]["coordination"] == {
        "slot_id": root.slot_id,
        "generation": root.generation,
        "lane": "root",
    }

    store.complete(critic.slot_id, outcome="terminal_rc_0")
    review_status = store.complete(root.slot_id, outcome="terminal_rc_0")
    assert review_status["phase"] == "critic_obstacle_review"
    review = _admit_and_activate(store, "xhigh2")
    assert review.slot_id != critic.slot_id

    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh2",
        DANUS_ROLE="worker",
    ):
        with pytest.raises(RuntimeError, match="exact designated review slot"):
            server.gm_add(
                "obstacle",
                claim="wrong designated id must not append",
                links={"confirms_entry_id": "0" * 16},
            )
        assert not obstacle_path.exists()
        critic_entry = server.gm_add(
            "obstacle",
            claim="independent critic confirms the root dead end",
            links={"confirms_entry_id": root_entry["id"]},
        )

    stored_critic = GlobalMemory(project).read("obstacle")[-1]
    assert critic_entry == {"id": stored_critic["id"], "kind": "obstacle"}
    assert stored_critic["links"]["coordination"] == {
        "slot_id": review.slot_id,
        "generation": review.generation,
        "lane": "critic",
    }
    assert stored_critic["links"]["confirms_entry_id"] == root_entry["id"]
    recommendation = store.project_status()["recommendation"]
    assert recommendation["root_entry_id"] == root_entry["id"]
    assert recommendation["critic_entry_id"] == critic_entry["id"]
    assert recommendation["review_id"] == review.review_id


def test_reasoning_review_record_cap_rejects_before_global_memory_append(
    tmp_path: Path,
):
    project = tmp_path / "coordinated-review-cap"
    store, admissions = _active_reasoning_store(project, workers=2)
    root, critic = admissions
    root_path = project / "global_memory" / "obstacle.jsonl"
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh",
        DANUS_ROLE="worker",
    ):
        with pytest.raises(ValueError, match="16 KiB hard limit"):
            server.gm_add("obstacle", claim="x" * (20 * 1024))
        assert not root_path.exists()
        root_entry = server.gm_add("obstacle", claim="bounded exact obstacle")
        after_root = root_path.read_bytes()
        with pytest.raises(RuntimeError, match="conflicts with the durable orphan"):
            server.gm_add("dead_end", claim="second root obstacle is rejected")
        assert root_path.read_bytes() == after_root
        assert not (project / "global_memory" / "dead_end.jsonl").exists()

    store.complete(root.slot_id, outcome="terminal_rc_0")
    store.complete(critic.slot_id, outcome="terminal_rc_0")
    _admit_and_activate(store, "xhigh2")
    before = root_path.read_bytes()
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh2",
        DANUS_ROLE="worker",
    ):
        with pytest.raises(ValueError, match="16 KiB hard limit"):
            server.gm_add(
                "obstacle",
                claim="y" * (20 * 1024),
                links={"confirms_entry_id": root_entry["id"]},
            )
    assert root_path.read_bytes() == before
    assert store.project_status()["recommendation"] is None


def test_reasoning_first_gm_append_cut_retains_reconcilable_slot_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "coordinated-cut"
    store, admissions = _active_reasoning_store(project, workers=2)
    root = admissions[0]
    original_record = CoordinationStore.record_root_evidence

    def fail_after_global_memory_append(*_args, **_kwargs):
        raise OSError("injected SQLite cut")

    monkeypatch.setattr(
        CoordinationStore, "record_root_evidence", fail_after_global_memory_append
    )
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh",
        DANUS_ROLE="worker",
    ):
        result = server.gm_add("obstacle", claim="durable before SQLite cut")

    assert result["coordination_pending_reconciliation"] is True
    assert "id" in result and "entry_id" not in result
    entry = GlobalMemory(project).read("obstacle")[-1]
    assert entry["id"] == result["id"]
    assert entry["links"]["coordination"] == {
        "slot_id": root.slot_id,
        "generation": root.generation,
        "lane": "root",
    }
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh",
        DANUS_ROLE="worker",
    ):
        with pytest.raises(RuntimeError, match="conflicts with the durable orphan"):
            server.gm_add("obstacle", claim="conflicting retry after SQLite cut")
    assert len(GlobalMemory(project).read("obstacle")) == 1
    monkeypatch.setattr(CoordinationStore, "record_root_evidence", original_record)
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh",
        DANUS_ROLE="worker",
    ):
        replay = server.gm_add("obstacle", claim="durable before SQLite cut")
    assert replay == {"id": result["id"], "kind": "obstacle"}
    assert len(GlobalMemory(project).read("obstacle")) == 1
    assert store.evidence_entry(result["id"])["slot_id"] == root.slot_id
    assert store.project_status()["review"]["root_entry_id"] == result["id"]
    reconciled = store.reconcile_terminal_memory_entries(
        root.slot_id,
        "xhigh",
        [entry],
    )
    assert reconciled["accepted_entry_ids"] == [result["id"]]


def test_designated_critic_append_cut_exact_retry_recovers_one_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "critic-confirmation-cut"
    store, admissions = _active_reasoning_store(project, workers=2)
    root, critic = admissions
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh",
        DANUS_ROLE="worker",
    ):
        root_entry = server.gm_add("dead_end", claim="exact critic cut root")
    store.complete(critic.slot_id, outcome="terminal_rc_0")
    store.complete(root.slot_id, outcome="terminal_rc_0")
    review = _admit_and_activate(store, "xhigh2")

    original_confirm = CoordinationStore.confirm_root_evidence

    def fail_after_critic_memory_append(*_args, **_kwargs):
        raise OSError("injected critic SQLite cut")

    monkeypatch.setattr(
        CoordinationStore,
        "confirm_root_evidence",
        fail_after_critic_memory_append,
    )
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh2",
        DANUS_ROLE="worker",
    ):
        first = server.gm_add(
            "obstacle",
            claim="exact designated critic confirmation",
            links={"confirms_entry_id": root_entry["id"]},
        )
        assert first["coordination_pending_reconciliation"] is True
        with pytest.raises(RuntimeError, match="conflicts with the durable orphan"):
            server.gm_add(
                "obstacle",
                claim="conflicting designated critic retry",
                links={"confirms_entry_id": root_entry["id"]},
            )
    critic_entries = GlobalMemory(project).read("obstacle")
    assert len(critic_entries) == 1
    assert store.project_status()["recommendation"] is None

    monkeypatch.setattr(
        CoordinationStore,
        "confirm_root_evidence",
        original_confirm,
    )
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="xhigh2",
        DANUS_ROLE="worker",
    ):
        replay = server.gm_add(
            "obstacle",
            claim="exact designated critic confirmation",
            links={"confirms_entry_id": root_entry["id"]},
        )
    assert replay == {"id": first["id"], "kind": "obstacle"}
    assert len(GlobalMemory(project).read("obstacle")) == 1
    recommendation = store.project_status()["recommendation"]
    assert recommendation is not None
    assert recommendation["critic_entry_id"] == first["id"]
    reconciled = store.reconcile_terminal_memory_entries(
        review.slot_id,
        "xhigh2",
        critic_entries,
    )
    assert reconciled["recommendation_id"] == recommendation["recommendation_id"]


def test_reasoning_candidate_is_active_before_verify_and_terminalizes_correct(
    tmp_path: Path,
):
    project = tmp_path / "candidate-visible"
    store, admissions = _active_reasoning_store(project)
    observed = {}

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        active = store.project_status()["candidate"]
        assert active is not None and active["state"] == "active"
        observed.update(active)
        return _verify_response(fact_context, candidate_proof=proof)

    original_verify = server._verify
    server._verify = verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement="Candidate registration is visible",
                proof="A complete proof of candidate visibility.",
            )
    finally:
        server._verify = original_verify

    assert observed["slot_id"] == admissions[0].slot_id
    assert observed["candidate_fact_id"] == result["fact_id"]
    with FactGraph(project).locked_active_fact_identity(result["fact_id"]) as identity:
        assert observed["candidate_fact_identity"] == identity
    assert observed["source_id"] is None
    assert result["candidate_receipt_id"] == observed["candidate_receipt_id"]
    assert result["candidate_outcome"] == "correct"
    assert result["candidate_terminalization_error"] is None
    assert store.project_status()["candidate"] is None
    terminal = next(
        item
        for item in store.list_candidates()
        if item["candidate_id"] == result["candidate_receipt_id"]
    )
    assert terminal["state"] == "terminal"


def test_explorer_gateway_publishes_findings_and_candidate_without_review_authority(
    tmp_path: Path,
):
    project = tmp_path / "explorer-gateway"
    store, explorer = _active_explorer_store(project)
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="high",
        DANUS_ROLE="worker",
    ):
        finding = server.gm_add(
            "obstacle",
            claim="An explorer reports an ordinary alternate-route obstruction",
        )
        with pytest.raises(RuntimeError, match="explorer publications cannot confirm"):
            server.gm_add(
                "obstacle",
                claim="An explorer cannot confirm a root obstruction",
                links={"confirms_entry_id": "a" * 16},
            )

    stored = GlobalMemory(project).read("obstacle")
    assert [entry["id"] for entry in stored] == [finding["id"]]
    assert stored[0]["links"]["coordination"] == {
        "slot_id": explorer.slot_id,
        "generation": 1,
        "lane": "explorer1",
    }
    assert store.project_status()["review"] is None
    assert store.project_status()["recommendation"] is None

    observed = {}

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        candidate = store.project_status()["candidate"]
        assert candidate is not None
        assert candidate["lane"] == "explorer1"
        assert store.admit("max") is None
        observed.update(candidate)
        return _verify_response(fact_context, candidate_proof=proof)

    original_verify = server._verify
    server._verify = verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="high",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement="An explorer verifier-gated supporting lemma",
                proof="A complete proof of the supporting lemma.",
                source_id=finding["id"],
            )
    finally:
        server._verify = original_verify

    assert result["accepted"] is True
    assert result["candidate_outcome"] == "correct"
    assert observed["lane"] == "explorer1"
    assert store.project_status()["candidate"] is None
    assert store.admit("max") is not None


@pytest.mark.parametrize(
    ("mode", "expected_outcome"),
    [
        ("wrong", "wrong"),
        ("verify_error", "error"),
        ("delivery_unknown", "outcome_unknown"),
        ("promotion_unknown", "promotion_unknown"),
        ("write_error", "error"),
    ],
)
def test_reasoning_candidate_terminalizes_every_final_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_outcome: str,
):
    project = tmp_path / f"candidate-{mode}"
    store, _admissions = _active_reasoning_store(project)

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        assert store.project_status()["candidate"]["state"] == "active"
        if mode == "verify_error":
            raise RuntimeError("known verifier failure")
        if mode == "delivery_unknown":
            raise OSError("ambiguous verifier delivery")
        if mode == "wrong":
            return _verify_response(
                fact_context,
                verdict="wrong",
                repair_hints="repair the candidate",
                candidate_proof=proof,
            )
        return _verify_response(fact_context, candidate_proof=proof)

    if mode == "promotion_unknown":
        monkeypatch.setattr(
            FactGraph,
            "add_if_context_unchanged",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                FactPromotionOutcomeUnknown("injected promotion ambiguity")
            ),
        )
    elif mode == "write_error":
        monkeypatch.setattr(
            FactGraph,
            "add_if_context_unchanged",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("injected deterministic write failure")
            ),
        )

    original_verify = server._verify
    server._verify = verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement=f"Candidate terminal exit {mode}",
                proof="A candidate proof with a controlled exit.",
            )
    finally:
        server._verify = original_verify

    assert result["candidate_outcome"] == expected_outcome
    assert result["candidate_terminalization_error"] is None
    trace = GlobalMemory(project).read("verification")[-1]
    assert trace["candidate_receipt_id"] == result["candidate_receipt_id"]
    assert trace["candidate_outcome"] == expected_outcome
    candidate = next(
        item
        for item in store.list_candidates()
        if item["candidate_id"] == result["candidate_receipt_id"]
    )
    expected_state = (
        "outcome_unknown" if expected_outcome == "outcome_unknown" else "terminal"
    )
    assert candidate["state"] == expected_state
    if expected_outcome == "outcome_unknown":
        assert store.project_status()["candidate"]["outcome"] == expected_outcome
    assert (store.project_status()["candidate"] is not None) == (
        expected_outcome == "outcome_unknown"
    )


def test_reasoning_candidate_needs_context_exit_terminalizes_without_second_call(
    tmp_path: Path,
):
    project = tmp_path / "candidate-needs-context"
    store, _admissions = _active_reasoning_store(project)
    predecessor = FactGraph(project).add(
        problem_id="P",
        author="xhigh",
        statement="Predecessor statement",
        proof="Predecessor proof.",
    )
    calls = {"count": 0}

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        calls["count"] += 1
        return _verify_response(
            fact_context,
            status="needs_context",
            verdict="wrong",
            requests=[{"id": predecessor, "reason": "inspect predecessor proof"}],
            candidate_proof=proof,
        )

    original_verify = server._verify
    server._verify = verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
            DANUS_VERIFY_MAX_EXPANSION_ROUNDS="0",
        ):
            result = server.fact_submit(
                statement=f"Candidate depends on {predecessor}",
                proof=f"Use {predecessor} to finish the proof.",
                predecessors=[predecessor],
            )
    finally:
        server._verify = original_verify

    assert calls["count"] == 1
    assert result["candidate_outcome"] == "needs_context"
    candidate = next(
        item
        for item in store.list_candidates()
        if item["candidate_id"] == result["candidate_receipt_id"]
    )
    assert candidate["state"] == "terminal"


def test_reasoning_candidate_registration_failure_blocks_verifier(
    tmp_path: Path,
):
    project = tmp_path / "candidate-no-active-slot"
    project.mkdir()
    metadata = {
        "name": project.name,
        "model": "model",
        "roles": "xhigh:1",
        "workers": ["xhigh"],
        "coordination": dict(DEFAULT_COORDINATION),
    }
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    CoordinationStore(project, metadata)
    calls = {"count": 0}

    def must_not_verify(*_args, **_kwargs):
        calls["count"] += 1
        raise AssertionError("candidate registration must precede verifier call")

    original_verify = server._verify
    server._verify = must_not_verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement="Blocked candidate",
                proof="This verifier call must remain blocked.",
            )
    finally:
        server._verify = original_verify

    assert calls["count"] == 0
    assert result["verification_calls"] == 0
    assert "candidate coordination error" in result["error"]


def test_reasoning_exact_active_reuse_registers_no_candidate(
    tmp_path: Path,
):
    project = tmp_path / "candidate-exact-reuse"
    store, _admissions = _active_reasoning_store(project)
    graph = FactGraph(project)
    fact_id = graph.add(
        problem_id="P",
        author="xhigh",
        statement="Already active exact fact",
        proof="A complete existing proof.",
    )

    def must_not_verify(*_args, **_kwargs):
        raise AssertionError("exact active reuse must not invoke verifier")

    original_verify = server._verify
    server._verify = must_not_verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement="Already active exact fact",
                proof="A complete existing proof.",
            )
    finally:
        server._verify = original_verify

    assert result["fact_id"] == fact_id
    assert result["verification_reuse"] == "active_exact_fact"
    assert result["verification_calls"] == 0
    assert "candidate_receipt_id" not in result
    assert store.project_status()["candidate"] is None
    assert store.list_candidates() == []


def test_reasoning_duplicate_concurrent_candidate_uses_one_receipt(
    tmp_path: Path,
):
    project = tmp_path / "candidate-concurrent"
    store, _admissions = _active_reasoning_store(project)
    both_registered = threading.Barrier(2)
    seen_receipts = []

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        active = store.project_status()["candidate"]
        assert active is not None and active["state"] == "active"
        seen_receipts.append(active["candidate_receipt_id"])
        both_registered.wait(timeout=5)
        return _verify_response(fact_context, candidate_proof=proof)

    original_verify = server._verify
    server._verify = verify
    results = []
    errors = []

    def submit() -> None:
        try:
            results.append(
                server.fact_submit(
                    statement="Concurrent exact candidate",
                    proof="One complete proof shared by both retries.",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            threads = [threading.Thread(target=submit) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            assert all(not thread.is_alive() for thread in threads)
    finally:
        server._verify = original_verify

    assert errors == []
    assert len(results) == 2
    assert len(set(seen_receipts)) == 1
    assert {result["candidate_receipt_id"] for result in results} == set(seen_receipts)
    assert all(result["candidate_outcome"] == "correct" for result in results)
    matching = [
        item
        for item in store.list_candidates()
        if item["candidate_id"] == seen_receipts[0]
    ]
    assert len(matching) == 1 and matching[0]["state"] == "terminal"


def test_reasoning_post_add_crash_reconciles_exact_candidate_on_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "candidate-post-add-crash"
    store, _admissions = _active_reasoning_store(project)
    original_terminalize = CoordinationStore.terminalize_candidate
    verify_calls = {"count": 0}

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        verify_calls["count"] += 1
        return _verify_response(fact_context, candidate_proof=proof)

    def crash_before_terminalize(*_args, **_kwargs):
        raise SystemExit("injected crash after durable fact add")

    original_verify = server._verify
    server._verify = verify
    monkeypatch.setattr(
        CoordinationStore, "terminalize_candidate", crash_before_terminalize
    )
    kwargs = {
        "statement": "Fact durable before candidate terminalization",
        "proof": "A complete proof surviving the injected crash.",
    }
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
            DANUS_VERIFY_CONTEXT_MAX_CHARS="200000",
            DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS="200000",
        ):
            with pytest.raises(SystemExit, match="injected crash"):
                server.fact_submit(**kwargs)
            graph = FactGraph(project)
            fact_id = graph.list()[0]
            fact_path = graph._path(fact_id)
            before = (fact_path.stat().st_mtime_ns, fact_path.read_bytes())
            active = store.project_status()["candidate"]
            assert active is not None and active["state"] == "active"
            os.environ["DANUS_VERIFY_CONTEXT_MAX_CHARS"] = "210000"
            os.environ["DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS"] = "210000"

            monkeypatch.setattr(
                CoordinationStore, "terminalize_candidate", original_terminalize
            )

            def must_not_verify(*_args, **_kwargs):
                raise AssertionError("crash retry must use active exact fact")

            server._verify = must_not_verify
            retry = server.fact_submit(**kwargs)
    finally:
        server._verify = original_verify

    assert verify_calls["count"] == 1
    assert retry["fact_id"] == fact_id
    assert retry["verification_calls"] == 0
    assert retry["verification_reuse"] == "active_exact_fact"
    assert retry["candidate_receipt_id"] == active["candidate_receipt_id"]
    assert retry["candidate_outcome"] == "correct"
    assert "candidate_terminalization_error" not in retry
    assert (fact_path.stat().st_mtime_ns, fact_path.read_bytes()) == before
    assert store.project_status()["candidate"] is None


def test_reasoning_exact_reuse_leaves_unrelated_active_candidate_unchanged(
    tmp_path: Path,
):
    project = tmp_path / "candidate-unrelated-reuse"
    store, admissions = _active_reasoning_store(project)
    unrelated_fact_id = "1" * 16
    unrelated_fact_identity = "3" * 64
    unrelated_context_digest = "2" * 64
    unrelated_receipt = candidate_receipt_id(
        slot_id=admissions[0].slot_id,
        candidate_fact_id=unrelated_fact_id,
        candidate_fact_identity=unrelated_fact_identity,
        source_id=None,
        context_digest=unrelated_context_digest,
    )
    before = store.register_candidate(
        "xhigh",
        unrelated_receipt,
        slot_id=admissions[0].slot_id,
        candidate_fact_id=unrelated_fact_id,
        candidate_fact_identity=unrelated_fact_identity,
        source_id=None,
        context_digest=unrelated_context_digest,
    )
    graph = FactGraph(project)
    reused_fact_id = graph.add(
        problem_id="P",
        author="xhigh",
        statement="A different already-active fact",
        proof="A complete proof for the different fact.",
    )

    def must_not_verify(*_args, **_kwargs):
        raise AssertionError("exact active reuse must not invoke verifier")

    original_verify = server._verify
    server._verify = must_not_verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement="A different already-active fact",
                proof="A complete proof for the different fact.",
            )
    finally:
        server._verify = original_verify

    assert result["fact_id"] == reused_fact_id
    assert result["verification_reuse"] == "active_exact_fact"
    assert "candidate_receipt_id" not in result
    assert store.project_status()["candidate"] == before


def test_reasoning_exact_reuse_does_not_release_same_short_different_full_identity(
    tmp_path: Path,
):
    project = tmp_path / "candidate-full-identity-collision"
    store, admissions = _active_reasoning_store(project)
    graph = FactGraph(project)
    statement = "An active fact with a colliding short candidate receipt"
    proof = "A complete proof for the active fact."
    fact_id = graph.add(
        problem_id="P",
        author="xhigh",
        statement=statement,
        proof=proof,
    )
    with graph.locked_active_fact_identity(fact_id) as active_identity:
        assert isinstance(active_identity, str)
    colliding_identity = ("0" if active_identity[0] != "0" else "1") + active_identity[
        1:
    ]
    receipt = candidate_receipt_id(
        slot_id=admissions[0].slot_id,
        candidate_fact_id=fact_id,
        candidate_fact_identity=colliding_identity,
        source_id=None,
        context_digest="2" * 64,
    )
    before = store.register_candidate(
        "xhigh",
        receipt,
        slot_id=admissions[0].slot_id,
        candidate_fact_id=fact_id,
        candidate_fact_identity=colliding_identity,
        source_id=None,
        context_digest="2" * 64,
    )

    def must_not_verify(*_args, **_kwargs):
        raise AssertionError("exact active reuse must not invoke verifier")

    original_verify = server._verify
    server._verify = must_not_verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(statement=statement, proof=proof)
    finally:
        server._verify = original_verify

    assert result["fact_id"] == fact_id
    assert "candidate_receipt_id" not in result
    assert store.project_status()["candidate"] == before


def test_fact_context_gateway_defaults_to_summary_only():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="tester"),
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="Base", proof="proof base")
        child = fg.add(
            problem_id="P",
            author="w",
            statement="Child",
            proof="proof child",
            predecessors=[base],
        )
        out = server.fact_context([child])
        assert out["facts"] == [
            {
                "fact_id": child,
                "statement": "Child",
                "predecessors": [base],
                "glossary_introduces": {},
            }
        ]
        selected = server.fact_context(
            [child], predecessor_depth=None, proof_mode="selected"
        )
        assert selected["facts"][0]["proof"] == "proof child"
        assert "proof" not in selected["facts"][1]


def test_fact_submit_accept_writes_fact_and_traces():
    captured = {}
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("correct", capture=captured),
    ):
        res = server.fact_submit(statement="S(n)=n^2", proof="induction; QED")
        assert res["accepted"] is True and res["fact_id"]
        assert res["promoted"] is True
        assert res["submission_status"] == "promoted"
        assert res["verification_verdict"] == "correct"
        # An empty predecessor list still sends an explicit complete empty context.
        assert captured["fact_context"]["facts"] == []
        assert captured["fact_context"]["complete"] is True
        assert captured["fact_context"]["truncated"] is False
        # the fact really landed in the graph
        fg = FactGraph(Path(d))
        assert fg.exists(res["fact_id"])
        # a verification trace was always written to global memory
        gm = GlobalMemory(Path(d))
        traces = gm.read("verification")
        assert traces and traces[-1]["verdict"] == "correct"
        assert traces[-1]["fact_id"] == res["fact_id"]
        assert traces[-1]["promoted"] is True
        assert traces[-1]["submission_status"] == "promoted"
        assert traces[-1]["verification_verdict"] == "correct"


def test_verify_preserves_bounded_fastapi_string_detail(
    monkeypatch: pytest.MonkeyPatch,
):
    body = json.dumps(
        {"detail": "candidate proof cites undeclared fact IDs: badcafe"}
    ).encode("utf-8")
    response_body = io.BytesIO(body)
    error = urllib.error.HTTPError(
        url="http://127.0.0.1:8092/verify",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=response_body,
    )

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/health"):
            return _health_response()
        raise error

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(
            RuntimeError,
            match="verify service HTTP 400: candidate proof cites undeclared fact IDs: badcafe",
        ):
            server._verify("statement", "proof")
    assert response_body.closed is True


def test_verify_omits_non_string_or_oversized_http_error_body(
    monkeypatch: pytest.MonkeyPatch,
):
    bodies = [
        json.dumps({"detail": {"input": "PRIVATE-PROOF"}}).encode("utf-8"),
        json.dumps({"detail": "PRIVATE-PROOF" * 1000}).encode("utf-8"),
        b"<html>PRIVATE-PROOF</html>",
    ]
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        for body in bodies:
            response_body = io.BytesIO(body)
            error = urllib.error.HTTPError(
                url="http://127.0.0.1:8092/verify",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=response_body,
            )
            monkeypatch.setattr(
                server.urllib.request,
                "urlopen",
                lambda request, _error=error, **_kwargs: (
                    _health_response()
                    if request.full_url.endswith("/health")
                    else (_ for _ in ()).throw(_error)
                ),
            )
            with pytest.raises(RuntimeError) as captured:
                server._verify("statement", "proof")
            assert str(captured.value) == "verify service HTTP 400"
            assert "PRIVATE-PROOF" not in str(captured.value)
            assert response_body.closed is True


def test_verify_closes_http_error_when_body_read_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    class FailingBody:
        closed = False

        def read(self, _limit: int) -> bytes:
            raise OSError("injected response read failure")

        def close(self) -> None:
            self.closed = True

    response_body = FailingBody()
    error = urllib.error.HTTPError(
        url="http://127.0.0.1:8092/verify",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=response_body,
    )
    monkeypatch.setattr(
        server.urllib.request,
        "urlopen",
        lambda request, **_kwargs: (
            _health_response()
            if request.full_url.endswith("/health")
            else (_ for _ in ()).throw(error)
        ),
    )
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(OSError, match="injected response read failure"):
            server._verify("statement", "proof")
    assert response_body.closed is True


def test_verify_bounds_and_closes_oversized_success_response(
    monkeypatch: pytest.MonkeyPatch,
):
    response_body = io.BytesIO(b"x" * (server._VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES + 1))

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/health"):
            return _health_response()
        return response_body

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(RuntimeError, match="success response is too large"):
            server._verify("statement", "proof")
    assert response_body.closed is True


def test_verify_closes_success_response_when_bounded_read_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    class FailingSuccess:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def read(self, limit: int) -> bytes:
            assert limit == server._VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES + 1
            raise OSError("injected success read failure")

        def close(self) -> None:
            self.closed = True

    response = FailingSuccess()

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/health"):
            return _health_response()
        return response

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(OSError, match="injected success read failure"):
            server._verify("statement", "proof")
    assert response.closed is True


def test_verify_normal_success_uses_bound_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
):
    class TrackingSuccess(io.BytesIO):
        requested_limit = None

        def read(self, limit: int = -1) -> bytes:
            self.requested_limit = limit
            return super().read(limit)

    response = TrackingSuccess(json.dumps({"ok": True}).encode("utf-8"))

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/health"):
            return _health_response()
        return response

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        assert server._verify("statement", "proof") == {"ok": True}
    assert response.requested_limit == server._VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES + 1
    assert response.closed is True


def test_new_gateway_rejects_old_health_before_post_or_model(
    monkeypatch: pytest.MonkeyPatch,
):
    requests = []

    def urlopen(request, **_kwargs):
        requests.append((request.method, request.full_url))
        if request.full_url.endswith("/health"):
            return _health_response(include_contract=False)
        raise AssertionError("paid verify POST must not be sent")

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(RuntimeError, match="output protocol mismatch"):
            server._verify("statement", "proof")
    assert requests == [("GET", "http://127.0.0.1:8092/health")]


@pytest.mark.parametrize(
    ("protocol", "digest", "message"),
    [
        (2, _TEST_VERIFIER_BUNDLE_DIGEST, "output protocol mismatch"),
        (3, "not-a-digest", "valid bundle digest"),
    ],
)
def test_gateway_health_contract_mismatch_sends_zero_verify_posts(
    monkeypatch: pytest.MonkeyPatch,
    protocol,
    digest,
    message,
):
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request.full_url)
        if request.full_url.endswith("/health"):
            return _health_response(protocol=protocol, digest=digest)
        raise AssertionError("paid verify POST must not be sent")

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(RuntimeError, match=message):
            server._verify("statement", "proof")
    assert requests == ["http://127.0.0.1:8092/health"]


@pytest.mark.parametrize(
    ("pid", "instance_nonce", "message"),
    [
        (True, "0" * 32, "positive pid"),
        (0, "0" * 32, "positive pid"),
        (1234, "A" * 32, "instance nonce"),
        (1234, "short", "instance nonce"),
    ],
)
def test_gateway_health_rejects_inexact_pid_or_nonce_before_post(
    monkeypatch: pytest.MonkeyPatch,
    pid,
    instance_nonce,
    message,
):
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request.full_url)
        if request.full_url.endswith("/health"):
            return _health_response(pid=pid, instance_nonce=instance_nonce)
        raise AssertionError("verify POST must not be sent")

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(RuntimeError, match=message):
            server._verify("statement", "proof")
    assert requests == ["http://127.0.0.1:8092/health"]


def test_verify_posts_the_same_preflight_nonce_and_restart_mismatch_is_clean(
    monkeypatch: pytest.MonkeyPatch,
):
    nonce = "1" * 32
    captured = {}

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/health"):
            return _health_response(instance_nonce=nonce)
        captured.update(json.loads(request.data.decode("utf-8")))
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=409,
            msg="Conflict",
            hdrs={},
            fp=io.BytesIO(
                json.dumps(
                    {
                        "detail": "verifier instance changed after caller health preflight"
                    }
                ).encode("utf-8")
            ),
        )

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with _env(DANUS_VERIFY_URL="http://127.0.0.1:8092/verify"):
        with pytest.raises(RuntimeError, match="HTTP 409: verifier instance changed"):
            server._verify("statement", "proof")
    assert captured["expected_verifier_instance_nonce"] == nonce


@pytest.mark.parametrize(
    ("source", "wait_ms", "outcome"),
    [
        ("launched", "23", "queued"),
        ("coalesced", "11", "coalesced"),
        ("cache_hit", "0", "cache"),
    ],
)
def test_fact_submit_projects_bounded_scheduler_headers_into_response_and_trace(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    wait_ms: str,
    outcome: str,
):
    request_key = "b" * 64

    class Response(io.BytesIO):
        def __init__(self, body: bytes, headers=None):
            super().__init__(body)
            self.headers = headers or {}

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/health"):
            body = _health_response(instance_nonce="2" * 32).getvalue()
            return Response(body)
        payload = json.loads(request.data.decode("utf-8"))
        result = _verify_response(
            payload["fact_context"],
            candidate_proof=payload["proof"],
        )
        return Response(
            json.dumps(result).encode("utf-8"),
            headers={
                "X-Danus-Verify-Scheduler": source,
                "X-Danus-Verify-Key": request_key,
                "X-Danus-Verify-Wait-Ms": wait_ms,
            },
        )

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://127.0.0.1:8092/verify",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        result = server.fact_submit(
            statement=f"Scheduler telemetry {outcome}",
            proof="A complete proof for bounded telemetry.",
        )
        expected = {
            "outcome": outcome,
            "source": source,
            "request_key_sha256": request_key,
            "wait_ms": int(wait_ms),
        }
        assert result["verification_scheduler"] == [expected]
        trace = GlobalMemory(Path(d)).read("verification")[-1]
        assert trace["verification_rounds"][0]["verification_scheduler"] == expected
        assert set(result["verification_scheduler"][0]) == {
            "outcome",
            "source",
            "request_key_sha256",
            "wait_ms",
        }


def test_scheduler_header_parser_accepts_only_correlated_rejection() -> None:
    parsed = server._parse_verify_scheduler_headers(
        {
            "X-Danus-Verify-Scheduler": "rejected",
            "X-Danus-Verify-Key": "a" * 64,
            "X-Danus-Verify-Wait-Ms": "0",
            "X-Danus-Verify-Rejection": "distinct_queue_full",
        },
        allow_rejected=True,
    )
    assert parsed == {
        "outcome": "rejected",
        "source": "rejected",
        "request_key_sha256": "a" * 64,
        "wait_ms": 0,
        "rejection": "distinct_queue_full",
    }


@pytest.mark.parametrize(
    ("headers", "allow_rejected"),
    [
        (
            {
                "X-Danus-Verify-Scheduler": "rejected",
                "X-Danus-Verify-Key": "a" * 64,
                "X-Danus-Verify-Wait-Ms": "0",
            },
            True,
        ),
        (
            {
                "X-Danus-Verify-Scheduler": "rejected",
                "X-Danus-Verify-Key": "a" * 64,
                "X-Danus-Verify-Wait-Ms": "1",
                "X-Danus-Verify-Rejection": "queue_wait_timeout",
            },
            True,
        ),
        (
            {
                "X-Danus-Verify-Scheduler": "launched",
                "X-Danus-Verify-Key": "a" * 64,
                "X-Danus-Verify-Wait-Ms": "0",
                "X-Danus-Verify-Rejection": "distinct_queue_full",
            },
            True,
        ),
        (
            {
                "X-Danus-Verify-Scheduler": "cache_hit",
                "X-Danus-Verify-Key": "a" * 64,
                "X-Danus-Verify-Wait-Ms": "1",
            },
            False,
        ),
        (
            {
                "X-Danus-Verify-Scheduler": "cache_hit",
                "X-Danus-Verify-Key": "a" * 64,
                "X-Danus-Verify-Wait-Ms": "0",
            },
            True,
        ),
        (
            {
                "X-Danus-Verify-Scheduler": "launched",
                "X-Danus-Verify-Key": "a" * 64,
            },
            False,
        ),
        (
            {
                "X-Danus-Verify-Key": "a" * 64,
                "X-Danus-Verify-Wait-Ms": "0",
            },
            False,
        ),
    ],
)
def test_scheduler_header_parser_rejects_uncorrelated_fields(
    headers: dict[str, str], allow_rejected: bool
) -> None:
    with pytest.raises(RuntimeError, match="scheduler"):
        server._parse_verify_scheduler_headers(
            headers,
            allow_rejected=allow_rejected,
        )


def test_fact_submit_audits_body_free_human_frontier_without_verifier_leak():
    captured = {}
    sentinel = "OWNER-DIRECTION-MUST-NOT-ENTER-VERIFIER-4b91"
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_ROLE="worker",
            DANUS_HOTJOIN_ENABLED="1",
            DANUS_HOTJOIN_TARGET="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("correct", capture=captured),
    ):
        store = HotJoinStore(Path(d))
        message = store.enqueue(target="worker_high", body=sentinel)
        assert (
            store.claim(target="worker_high", owner="test-broker", allow_queued=True)
            is not None
        )
        store.record(
            message["message_id"],
            "steer_accepted",
            thread_id="thread-1",
            turn_id="turn-1",
        )

        result = server.fact_submit(statement="S", proof="complete proof")
        assert result["accepted"] is True
        trace = GlobalMemory(Path(d)).read("verification")[-1]
        frontier = trace["conversation_frontier_at_action"]
        assert frontier["status"] == "available"
        assert frontier["accepted_message_ids"] == [message["message_id"]]
        assert frontier["event_count"] == 3
        assert sentinel not in json.dumps(captured, ensure_ascii=False)
        assert sentinel not in json.dumps(trace, ensure_ascii=False)


def test_fact_submit_hotjoin_audit_failure_is_honest_but_does_not_bypass_verifier():
    verifier_calls = []
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
        project = Path(d)
        (project / ".human-intervention").symlink_to(
            Path(outside), target_is_directory=True
        )
        with (
            _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="worker_high",
                DANUS_ROLE="worker",
                DANUS_HOTJOIN_ENABLED="1",
                DANUS_HOTJOIN_TARGET="worker_high",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ),
            _mock_verify("correct", capture={}) as _unused,
        ):
            # _mock_verify is the production-shape verifier response. Count the
            # call independently to prove provenance loss is not a write bypass.
            original = server._verify

            def counted(*args, **kwargs):
                verifier_calls.append(True)
                return original(*args, **kwargs)

            server._verify = counted
            try:
                result = server.fact_submit(statement="S", proof="complete proof")
            finally:
                server._verify = original
        assert result["accepted"] is True
        assert verifier_calls == [True]
        assert len(FactGraph(project).list()) == 1
        trace = GlobalMemory(project).read("verification")[-1]
        assert trace["conversation_frontier_at_action"] == {
            "schema_version": 1,
            "status": "unavailable",
            "target": "worker_high",
            "error_type": "HotJoinError",
        }


def test_fact_submit_reject_writes_nothing_but_traces():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("wrong", repair_hints="gap in step 2"),
    ):
        res = server.fact_submit(statement="bad", proof="hand-wave")
        assert res["accepted"] is False and res["repair_hints"] == "gap in step 2"
        assert res["promoted"] is False
        assert res["submission_status"] == "rejected"
        assert res["verification_verdict"] == "wrong"
        fg = FactGraph(Path(d))
        assert fg.list() == []  # nothing written
        gm = GlobalMemory(Path(d))
        trace = gm.read("verification")[-1]
        assert trace["verdict"] == "wrong"  # but traced
        assert trace["promoted"] is False
        assert trace["submission_status"] == "rejected"
        assert trace["verification_verdict"] == "wrong"


def test_fact_submit_returns_written_fact_when_trace_append_fails():
    original_append = GlobalMemory.append
    try:
        for injected_error in (
            OSError("injected verification trace failure"),
            OSError(),
            MemoryError(),
        ):

            def fail_trace(self, *args, **kwargs):
                raise injected_error

            GlobalMemory.append = fail_trace
            with (
                tempfile.TemporaryDirectory() as d,
                _env(
                    DANUS_PROJECT_DIR=d,
                    DANUS_AGENTS_ROOT=None,
                    DANUS_AUTHOR="worker_high",
                    DANUS_VERIFY_URL="http://mock",
                    DANUS_PROBLEM_ID="P",
                ),
                _mock_verify("correct"),
            ):
                result = server.fact_submit(
                    statement="A durable accepted statement",
                    proof="A complete durable proof for this accepted statement.",
                )
                expected_error = str(injected_error) or type(injected_error).__name__
                assert result["accepted"] is True and result["fact_id"]
                assert result["promoted"] is True
                assert result["submission_status"] == "promoted"
                assert result["trace_error"] == expected_error
                assert FactGraph(Path(d)).exists(result["fact_id"])
    finally:
        GlobalMemory.append = original_append


def test_fact_submit_verify_error_is_clean():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("correct", raise_exc=RuntimeError("service down")),
    ):
        res = server.fact_submit(statement="s", proof="p")
        assert res["accepted"] is False and res["verdict"] == "error"
        assert res["promoted"] is False
        assert res["submission_status"] == "error"
        assert res["verification_verdict"] is None
        assert "service down" in res["error"]


def test_fact_submit_sends_full_statement_closure_and_no_ancestor_proofs():
    captured = {}
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("correct", capture=captured),
    ):
        fg = FactGraph(Path(d))
        base = fg.add(
            problem_id="P",
            author="w",
            statement="A holds",
            proof="pf A",
            glossary_introduces={"A": "the base assertion"},
        )
        direct = fg.add(
            problem_id="P",
            author="w",
            statement="B from A",
            proof="pf B",
            predecessors=[base],
        )
        res = server.fact_submit(
            statement="C from B",
            proof=f"uses verified fact {direct}",
            predecessors=[direct],
            glossary_introduces={"C_result": "the downstream conclusion"},
        )
        assert res["accepted"] is True and res["fact_id"]
        facts = captured["fact_context"]["facts"]
        assert [item["fact_id"] for item in facts] == [direct, base]
        assert all("proof" not in item for item in facts)
        assert facts[-1]["statement"] == "A holds"
        assert facts[-1]["glossary_introduces"] == {"A": "the base assertion"}
        assert captured["fact_context"]["expanded_proofs"] == []
        assert captured["fact_context"]["scope"]["proof_mode"] == "adaptive"
        assert captured["fact_context"]["scope"]["expansion_round"] == 0
        assert captured["fact_context"]["scope"]["closure_fact_ids"] == [direct, base]
        assert captured["fact_context"]["complete"] is True
        assert captured["glossary_introduces"] == {
            "C_result": "the downstream conclusion"
        }


def test_fact_submit_adaptively_hydrates_only_requested_ancestor_proof():
    contexts = []
    candidate_proofs = []
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        fg = FactGraph(Path(d))
        base = fg.add(
            problem_id="P",
            author="w",
            statement="Base premise",
            proof="BASE PROOF SECRET BYTES",
        )
        left = fg.add(
            problem_id="P",
            author="w",
            statement="Left consequence",
            proof="LEFT PROOF MUST STAY OMITTED",
            predecessors=[base],
        )
        right = fg.add(
            problem_id="P",
            author="w",
            statement="Right consequence",
            proof="RIGHT PROOF MUST STAY OMITTED",
            predecessors=[base],
        )
        original = server._verify

        def adaptive(statement, proof, fact_context=None, glossary_introduces=None):
            contexts.append(fact_context)
            candidate_proofs.append(proof)
            if len(contexts) == 1:
                return _verify_response(
                    fact_context,
                    status="needs_context",
                    verdict="wrong",
                    requests=[{"id": base, "reason": "inspect the shared lemma"}],
                )
            return _verify_response(fact_context)

        server._verify = adaptive
        try:
            result = server.fact_submit(
                statement="Combined consequence",
                proof=f"Apply {left} and {right}.",
                predecessors=[left, right],
            )
        finally:
            server._verify = original

        assert result["accepted"] is True and result["fact_id"]
        assert result["adaptive_rounds"] == 1
        assert result["verification_calls"] == 2
        assert result["expanded_proof_ids"] == [base]
        assert len(contexts) == 2
        assert candidate_proofs == [
            f"Apply {left} and {right}.",
            f"Apply {left} and {right}.",
        ]
        first, second = contexts
        assert first["expanded_proofs"] == []
        assert all("proof" not in record for record in first["facts"])
        first_serialized = json.dumps(first)
        assert "BASE PROOF SECRET BYTES" not in first_serialized
        assert "LEFT PROOF MUST STAY OMITTED" not in first_serialized
        assert "RIGHT PROOF MUST STAY OMITTED" not in first_serialized
        assert second["expanded_proofs"] == [
            {"fact_id": base, "proof": "BASE PROOF SECRET BYTES"}
        ]
        assert second["scope"]["expansion_round"] == 1
        assert second["digest"] != first["digest"]
        trace = GlobalMemory(Path(d)).read("verification")[-1]
        assert [entry["round"] for entry in trace["verification_rounds"]] == [0, 1]
        assert trace["verification_rounds"][0]["needs_expanded_proofs"] == [
            {"id": base, "reason": "inspect the shared lemma"}
        ]


def test_adaptive_second_round_error_and_request_reason_redact_all_secrets():
    canaries = (
        "CANARY_BEARER_ADAPTIVE",
        "CANARY_BASIC_ADAPTIVE",
        "CANARY_API_ADAPTIVE",
        "sk-CANARYADAPTIVE123",
    )
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        project = Path(d)
        ancestor = FactGraph(project).add(
            problem_id="P", author="w", statement="Ancestor", proof="proof"
        )
        calls = 0
        original = server._verify

        def adaptive(statement, proof, fact_context=None, glossary_introduces=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _verify_response(
                    fact_context,
                    status="needs_context",
                    verdict="wrong",
                    requests=[
                        {
                            "id": ancestor,
                            "reason": (
                                "Authorization: Bearer CANARY_BEARER_ADAPTIVE "
                                "api_key=CANARY_API_ADAPTIVE"
                            ),
                        }
                    ],
                )
            raise RuntimeError(
                "Authorization: Bearer CANARY_BEARER_ADAPTIVE\n"
                "Basic CANARY_BASIC_ADAPTIVE api_key=CANARY_API_ADAPTIVE "
                "sk-CANARYADAPTIVE123"
            )

        server._verify = adaptive
        try:
            result = server.fact_submit(
                statement="Candidate",
                proof="Use ancestor",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original

        assert result["submission_status"] == "error"
        assert result["verification_calls"] == 2
        assert "<redacted>" in json.dumps(result)
        trace = GlobalMemory(project).read("verification")[-1]
        assert len(trace["verification_rounds"]) == 2
        assert trace["verification_rounds"][-1]["verification_status"] == "error"
        assert "verdict" not in trace["verification_rounds"][-1]
        combined = json.dumps({"result": result, "trace": trace})
        for canary in canaries:
            assert canary not in combined
        for path in project.rglob("*"):
            if path.is_file():
                payload = path.read_bytes()
                for canary in canaries:
                    assert canary.encode() not in payload


def test_fact_submit_rejects_unknown_nonancestor_current_and_duplicate_requests():
    cases = ("unknown", "non-ancestor", "current", "duplicate")
    for case in cases:
        with (
            tempfile.TemporaryDirectory() as d,
            _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="w",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ),
        ):
            fg = FactGraph(Path(d))
            ancestor = fg.add(
                problem_id="P", author="w", statement="Ancestor", proof="proof A"
            )
            nonancestor = fg.add(
                problem_id="P", author="w", statement="Unrelated", proof="proof U"
            )
            statement = "Candidate"
            proof = f"Use {ancestor}."
            candidate = compute_fact_id(
                problem_id="P",
                predecessors=[ancestor],
                glossary_introduces={},
                statement=statement,
                proof=proof,
            )
            request_id = {
                "unknown": "0000000000000000",
                "non-ancestor": nonancestor,
                "current": candidate,
                "duplicate": ancestor,
            }[case]
            requests = [{"id": request_id, "reason": "need it"}]
            if case == "duplicate":
                requests.append({"id": request_id, "reason": "need it twice"})
            original = server._verify
            server._verify = (
                lambda statement,
                proof,
                fact_context=None,
                glossary_introduces=None,
                req=requests: _verify_response(
                    fact_context,
                    status="needs_context",
                    verdict="wrong",
                    requests=req,
                )
            )
            try:
                result = server.fact_submit(
                    statement=statement, proof=proof, predecessors=[ancestor]
                )
            finally:
                server._verify = original
            assert result["accepted"] is False and result["verdict"] == "error"
            assert not fg.exists(candidate)
            if case == "duplicate":
                assert "duplicate expansion request" in result["error"]
            else:
                assert case in result["error"]


def test_fact_submit_rejects_needs_context_plus_correct_and_repeated_request():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        fg = FactGraph(Path(d))
        ancestor = fg.add(
            problem_id="P", author="w", statement="Ancestor", proof="proof A"
        )
        original = server._verify
        server._verify = (
            lambda statement,
            proof,
            fact_context=None,
            glossary_introduces=None: _verify_response(
                fact_context,
                status="needs_context",
                verdict="correct",
                requests=[{"id": ancestor, "reason": "need it"}],
            )
        )
        try:
            invalid = server.fact_submit(
                statement="Candidate one",
                proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert invalid["accepted"] is False and invalid["verdict"] == "error"
        assert "needs_context" in invalid["error"]

        calls = 0

        def repeat(statement, proof, fact_context=None, glossary_introduces=None):
            nonlocal calls
            calls += 1
            return _verify_response(
                fact_context,
                status="needs_context",
                verdict="wrong",
                requests=[{"id": ancestor, "reason": "still need it"}],
            )

        server._verify = repeat
        try:
            repeated = server.fact_submit(
                statement="Candidate two",
                proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert calls == 2
        assert repeated["accepted"] is False and repeated["verdict"] == "error"
        assert "already expanded" in repeated["error"]


def test_fact_submit_final_wrong_after_expansion_never_writes():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        fg = FactGraph(Path(d))
        ancestor = fg.add(
            problem_id="P", author="w", statement="Ancestor", proof="flawed proof"
        )
        calls = 0
        original = server._verify

        def reject_after_expansion(
            statement, proof, fact_context=None, glossary_introduces=None
        ):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _verify_response(
                    fact_context,
                    status="needs_context",
                    verdict="wrong",
                    requests=[{"id": ancestor, "reason": "audit dependency"}],
                )
            return _verify_response(
                fact_context,
                status="final",
                verdict="wrong",
                repair_hints="replace the flawed ancestor dependency",
                candidate_proof=proof,
            )

        server._verify = reject_after_expansion
        try:
            result = server.fact_submit(
                statement="Candidate",
                proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert calls == 2
        assert result["accepted"] is False and result["verdict"] == "wrong"
        assert result["expanded_proof_ids"] == [ancestor]
        assert fg.list() == [ancestor]


def test_fact_submit_expansion_proof_and_round_budgets_fail_closed():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
            DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS="1",
        ),
    ):
        fg = FactGraph(Path(d))
        ancestor = fg.add(
            problem_id="P",
            author="w",
            statement="Ancestor",
            proof="whole proof record exceeds one character",
        )
        calls = 0
        original = server._verify

        def request(statement, proof, fact_context=None, glossary_introduces=None):
            nonlocal calls
            calls += 1
            return _verify_response(
                fact_context,
                status="needs_context",
                verdict="wrong",
                requests=[{"id": ancestor, "reason": "inspect proof"}],
            )

        server._verify = request
        try:
            result = server.fact_submit(
                statement="Candidate",
                proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert calls == 1
        assert result["accepted"] is False and result["verdict"] == "error"
        assert "omitted expanded proof" in result["error"]

    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
            DANUS_VERIFY_MAX_EXPANSION_ROUNDS="0",
        ),
    ):
        fg = FactGraph(Path(d))
        ancestor = fg.add(
            problem_id="P", author="w", statement="Ancestor", proof="proof A"
        )
        original = server._verify
        server._verify = (
            lambda statement,
            proof,
            fact_context=None,
            glossary_introduces=None: _verify_response(
                fact_context,
                status="needs_context",
                verdict="wrong",
                requests=[{"id": ancestor, "reason": "inspect proof"}],
            )
        )
        try:
            result = server.fact_submit(
                statement="Candidate",
                proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert result["accepted"] is False and result["verdict"] == "error"
        assert "maximum expansion rounds (0) exceeded" in result["error"]


def test_fact_submit_expanded_proof_count_budget_fails_before_hydration():
    contexts = []
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
            DANUS_VERIFY_MAX_EXPANDED_PROOFS="1",
        ),
    ):
        fg = FactGraph(Path(d))
        left = fg.add(
            problem_id="P",
            author="w",
            statement="Left ancestor",
            proof="LEFT PROOF MUST NOT BE HYDRATED",
        )
        right = fg.add(
            problem_id="P",
            author="w",
            statement="Right ancestor",
            proof="RIGHT PROOF MUST NOT BE HYDRATED",
        )
        statement = "Candidate"
        proof = f"Combine {left} and {right}."
        candidate = compute_fact_id(
            problem_id="P",
            predecessors=[left, right],
            glossary_introduces={},
            statement=statement,
            proof=proof,
        )
        original = server._verify

        def request_both(statement, proof, fact_context=None, glossary_introduces=None):
            contexts.append(fact_context)
            return _verify_response(
                fact_context,
                status="needs_context",
                verdict="wrong",
                requests=[
                    {"id": left, "reason": "inspect the left proof"},
                    {"id": right, "reason": "inspect the right proof"},
                ],
            )

        server._verify = request_both
        try:
            result = server.fact_submit(
                statement=statement,
                proof=proof,
                predecessors=[left, right],
            )
        finally:
            server._verify = original

        assert len(contexts) == 1
        assert contexts[0]["expanded_proofs"] == []
        assert result["accepted"] is False and result["verdict"] == "error"
        assert "maximum expanded proofs (1) exceeded" in result["error"]
        assert result["verification_calls"] == 1
        assert result["adaptive_rounds"] == 0
        assert result["expanded_proof_ids"] == []
        assert not fg.exists(candidate)


def test_fact_submit_lazily_snapshots_project_glossary():
    captured = {}
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("correct", capture=captured),
    ):
        fg = FactGraph(Path(d))
        source = fg.add(
            problem_id="P",
            author="w",
            statement="Definition source",
            proof="definition proof",
            glossary_introduces={"Q_X": "a distinguished project object"},
        )
        result = server.fact_submit(
            statement="Q_X has the required property",
            proof="By the defining property of Q_X, the conclusion follows.",
            predecessors=[source],
        )
        assert result["accepted"] is True
        context = captured["fact_context"]
        assert [fact["fact_id"] for fact in context["facts"]] == [source]
        assert context["facts"][0]["glossary_introduces"] == {
            "Q_X": "a distinguished project object"
        }
        assert "Q_X" not in context["glossary"]
        assert context["omitted_glossary_terms"] == []


def test_fact_submit_never_sends_implicit_project_glossary_to_verifier():
    captured = {}
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("correct", capture=captured),
    ):
        FactGraph(Path(d)).add(
            problem_id="P",
            author="w",
            statement="Definition source",
            proof="definition proof",
            glossary_introduces={"Q_Y": "a project object"},
        )
        result = server.fact_submit(
            statement="Q_Y has the required property",
            proof="Use the definition of Q_Y.",
        )
        assert result["accepted"] is True
        assert captured["fact_context"]["facts"] == []
        assert "Q_Y" not in captured["fact_context"]["glossary"]
        assert captured["fact_context"]["scope"]["include_project_glossary"] is False


def test_fact_submit_rechecks_project_glossary_after_verification():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        original = server._verify

        def change_glossary_during_verify(
            statement, proof, fact_context=None, glossary_introduces=None
        ):
            FactGraph(Path(d)).add(
                problem_id="P",
                author="other",
                statement="New definition",
                proof="proof",
                glossary_introduces={"Q_X": "a newly available project object"},
            )
            return {
                "output_schema_version": 3,
                "verification_status": "final",
                "verdict": "correct",
                "needs_expanded_proofs": [],
                "repair_hints": "",
                "verification_context_digest": fact_context["digest"],
                "verification_report": {
                    "summary": "mock",
                    "critical_errors": [],
                    "gaps": [],
                },
            }

        server._verify = change_glossary_during_verify
        try:
            result = server.fact_submit(
                statement="Q_X has the required property",
                proof="Use the defining property of Q_X.",
            )
        finally:
            server._verify = original
        assert result["accepted"] is True and result["fact_id"] is not None
        assert "write_error" not in result


@pytest.mark.parametrize("conflict_scope", ["project", "global"])
def test_fact_submit_known_glossary_conflict_blocks_before_candidate_and_verify(
    tmp_path: Path, conflict_scope: str
):
    """A known semantic conflict must consume neither paid work nor graph state."""
    project = tmp_path / "known-glossary-conflict"
    store, _admissions = _active_reasoning_store(project)
    fg = FactGraph(project)
    if conflict_scope == "project":
        term = "Q_X"
        established_definition = "the existing project object"
        existing = fg.add(
            problem_id="P",
            author="other",
            statement="Q_X is fixed",
            proof="definition proof",
            glossary_introduces={term: established_definition},
        )
    else:
        term, established_definition = next(iter(_glossary.global_glossary().items()))
        existing = fg.add(
            problem_id="P",
            author="other",
            statement="A harmless seed fact",
            proof="A harmless seed proof.",
        )
    conflicting_definition = established_definition + " (conflicting meaning)"

    def graph_bytes() -> dict[str, bytes]:
        return {
            str(path.relative_to(fg.dir)): path.read_bytes()
            for path in sorted(fg.dir.rglob("*"))
            if path.is_file()
        }

    before_graph = graph_bytes()
    verification_path = project / "global_memory" / "verification.jsonl"
    before_verification = (
        verification_path.read_bytes() if verification_path.exists() else None
    )
    calls = {"count": 0}
    allow_verify = {"value": False}

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        calls["count"] += 1
        if not allow_verify["value"]:
            raise AssertionError("known glossary conflict must block the verifier")
        return _verify_response(fact_context, candidate_proof=proof)

    original_verify = server._verify
    server._verify = verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement=f"{term} has another property",
                proof=f"A complete proof for the proposed meaning of {term}.",
                glossary_introduces={term: conflicting_definition},
            )

            assert calls["count"] == 0
            assert result["accepted"] is False
            assert result["verification_verdict"] is None
            assert result["promoted"] is False
            assert result["submission_status"] == "error"
            assert result["verification_calls"] == 0
            assert "glossary_conflict" in result["error"]
            assert term in result["repair_hints"]
            assert "candidate_receipt_id" not in result
            assert store.project_status()["candidate"] is None
            assert store.list_candidates() == []
            assert fg.list() == [existing]
            assert graph_bytes() == before_graph
            assert (
                verification_path.read_bytes() if verification_path.exists() else None
            ) == before_verification

            allow_verify["value"] = True
            repaired = server.fact_submit(
                statement=f"{term} has another property",
                proof=f"A complete proof for the proposed meaning of {term}.",
                glossary_introduces={term: established_definition},
            )
    finally:
        server._verify = original_verify

    assert calls["count"] == 1
    assert repaired["verification_calls"] == 1
    assert repaired["promoted"] is True
    assert repaired["fact_id"] is not None
    assert store.project_status()["candidate"] is None
    assert len(store.list_candidates()) == 1


def test_fact_submit_glossary_integrity_failure_blocks_before_paid_work(
    tmp_path: Path,
):
    project = tmp_path / "glossary-integrity-preflight"
    store, _admissions = _active_reasoning_store(project)
    fg = FactGraph(project)
    exact_statement = "Existing fact must not bypass glossary integrity"
    exact_proof = "An existing proof whose active identity is already stored."
    existing = fg.add(
        problem_id="P",
        author="other",
        statement=exact_statement,
        proof=exact_proof,
    )
    fg.glossary_path.write_text("{not valid json", encoding="utf-8")
    before_graph = {
        str(path.relative_to(fg.dir)): path.read_bytes()
        for path in sorted(fg.dir.rglob("*"))
        if path.is_file()
    }
    verification_path = project / "global_memory" / "verification.jsonl"
    calls = {"count": 0}

    def must_not_verify(*_args, **_kwargs):
        calls["count"] += 1
        raise AssertionError("integrity failure must block the verifier")

    original_verify = server._verify
    server._verify = must_not_verify
    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            result = server.fact_submit(
                statement=exact_statement,
                proof=exact_proof,
            )
    finally:
        server._verify = original_verify

    assert calls["count"] == 0
    assert result["verification_calls"] == 0
    assert result["verification_verdict"] is None
    assert result.get("verification_reuse") is None
    assert "glossary_integrity_error" in result["error"]
    assert "candidate_receipt_id" not in result
    assert store.project_status()["candidate"] is None
    assert store.list_candidates() == []
    assert fg.list() == [existing]
    assert {
        str(path.relative_to(fg.dir)): path.read_bytes()
        for path in sorted(fg.dir.rglob("*"))
        if path.is_file()
    } == before_graph
    assert not verification_path.exists()


def test_fact_submit_concurrent_glossary_conflict_keeps_promotion_cas_and_reverifies(
    tmp_path: Path,
):
    """A post-preflight conflict fails promotion; changed identity gets no reuse."""
    project = tmp_path / "concurrent-glossary-conflict"
    store, _admissions = _active_reasoning_store(project)
    entered_verify = threading.Event()
    release_verify = threading.Event()
    calls = {"count": 0}
    observed_candidates = []

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        calls["count"] += 1
        active = store.project_status()["candidate"]
        assert active is not None and active["state"] == "active"
        observed_candidates.append(dict(active))
        if calls["count"] == 1:
            entered_verify.set()
            assert release_verify.wait(timeout=5)
        return _verify_response(fact_context, candidate_proof=proof)

    original_verify = server._verify
    server._verify = verify
    raced_results = []
    raced_errors = []

    def submit_raced_candidate() -> None:
        try:
            raced_results.append(
                server.fact_submit(
                    statement="Q_RACE has the candidate property",
                    proof="A complete proof for the candidate interpretation.",
                    glossary_introduces={"Q_RACE": "the candidate project object"},
                )
            )
        except BaseException as exc:
            raced_errors.append(exc)

    try:
        with _env(
            DANUS_PROJECT_DIR=str(project),
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="xhigh",
            DANUS_ROLE="worker",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ):
            thread = threading.Thread(target=submit_raced_candidate)
            thread.start()
            assert entered_verify.wait(timeout=5)
            competing_fact = FactGraph(project).add(
                problem_id="P",
                author="other",
                statement="Q_RACE is established concurrently",
                proof="A complete competing definition proof.",
                glossary_introduces={
                    "Q_RACE": "the concurrently established project object"
                },
            )
            release_verify.set()
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert raced_errors == []
            assert len(raced_results) == 1
            raced = raced_results[0]
            assert raced["verification_calls"] == 1
            assert raced["verification_verdict"] == "correct"
            assert raced["promoted"] is False
            assert raced["submission_status"] == "verified_not_promoted"
            assert "glossary_conflict" in raced["write_error"]

            repaired = server.fact_submit(
                statement="Q_RACE has the candidate property",
                proof="A complete proof for the candidate interpretation.",
                glossary_introduces={
                    "Q_RACE": "the concurrently established project object"
                },
            )
    finally:
        release_verify.set()
        server._verify = original_verify

    assert repaired["verification_calls"] == 1
    assert repaired["promoted"] is True
    assert repaired["fact_id"] is not None
    assert calls["count"] == 2
    assert len(observed_candidates) == 2
    assert (
        observed_candidates[0]["candidate_fact_identity"]
        != observed_candidates[1]["candidate_fact_identity"]
    )
    assert (
        observed_candidates[0]["candidate_receipt_id"]
        != observed_candidates[1]["candidate_receipt_id"]
    )
    assert (
        repaired["candidate_receipt_id"]
        == observed_candidates[1]["candidate_receipt_id"]
    )
    assert set(FactGraph(project).list()) == {competing_fact, repaired["fact_id"]}
    candidates = store.list_candidates()
    assert len(candidates) == 2
    assert all(candidate["state"] == "terminal" for candidate in candidates)


def test_fact_submit_empty_write_exceptions_are_not_promoted_or_written():
    """A falsey diagnostic must never turn a failed graph write into success."""
    original_add = FactGraph.add_if_context_unchanged
    try:
        for injected_error in (OSError(), MemoryError()):

            def fail_write(self, **kwargs):
                raise injected_error

            FactGraph.add_if_context_unchanged = fail_write
            with (
                tempfile.TemporaryDirectory() as d,
                _env(
                    DANUS_PROJECT_DIR=d,
                    DANUS_AGENTS_ROOT=None,
                    DANUS_AUTHOR="worker_high",
                    DANUS_VERIFY_URL="http://mock",
                    DANUS_PROBLEM_ID="P",
                ),
                _mock_verify("correct"),
            ):
                result = server.fact_submit(
                    statement="A verifier-accepted candidate",
                    proof="A complete proof whose graph write is injected to fail.",
                )

                expected_error = type(injected_error).__name__
                assert result["accepted"] is True
                assert result["verification_verdict"] == "correct"
                assert result["promoted"] is False
                assert result["submission_status"] == "verified_not_promoted"
                assert result["fact_id"] is None
                assert result["write_error"] == expected_error
                assert FactGraph(Path(d)).list() == []

                trace = GlobalMemory(Path(d)).read("verification")[-1]
                assert trace["verification_verdict"] == "correct"
                assert trace["promoted"] is False
                assert trace["submission_status"] == "verified_not_promoted"
                assert trace["fact_id"] is None
                assert trace["write_error"] == expected_error
    finally:
        FactGraph.add_if_context_unchanged = original_add


def test_fact_submit_transaction_fsync_outcomes_match_response_and_trace():
    """Promotion follows the durable commit point, including cleanup failures."""
    original_fsync_directory = FactGraph._fsync_directory
    injected = False

    def fail_after_fact_directory_fsync(directory):
        nonlocal injected
        original_fsync_directory(directory)
        if directory.name == "facts" and not injected:
            injected = True
            raise OSError("injected post-replace fact fsync failure")

    FactGraph._fsync_directory = staticmethod(fail_after_fact_directory_fsync)
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="worker_high",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ),
            _mock_verify("correct"),
        ):
            result = server.fact_submit(
                statement="A candidate whose data fsync is rejected",
                proof="A complete proof for the injected pre-commit failure.",
            )
            assert result["accepted"] is True
            assert result["promoted"] is False
            assert result["submission_status"] == "verified_not_promoted"
            assert result["fact_id"] is None
            assert "post-replace fact fsync failure" in result["write_error"]

            trace = GlobalMemory(Path(d)).read("verification")[-1]
            assert trace["promoted"] is False
            assert trace["submission_status"] == "verified_not_promoted"
            assert trace["fact_id"] is None
            assert "post-replace fact fsync failure" in trace["write_error"]
            graph = FactGraph(Path(d))
            assert graph.list() == []
            assert not graph.pending_add_path.exists()
            assert not graph.pending_add_commit_path.exists()
    finally:
        FactGraph._fsync_directory = staticmethod(original_fsync_directory)

    original_unlink = FactGraph._unlink_durable

    def unlink_committed_marker_then_fail(self, path):
        original_unlink(self, path)
        if path == self.pending_add_commit_path:
            raise OSError("injected committed-marker unlink fsync failure")

    FactGraph._unlink_durable = unlink_committed_marker_then_fail
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="worker_high",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ),
            _mock_verify("correct"),
        ):
            result = server.fact_submit(
                statement="A candidate committed before cleanup",
                proof="A complete proof for the injected cleanup failure.",
                glossary_introduces={
                    "COMMITTED_GATEWAY_X_481": "the durable gateway test object"
                },
            )
            assert result["accepted"] is True
            assert result["promoted"] is True
            assert result["submission_status"] == "promoted"
            assert isinstance(result["fact_id"], str)
            assert "write_error" not in result

            trace = GlobalMemory(Path(d)).read("verification")[-1]
            assert trace["promoted"] is True
            assert trace["submission_status"] == "promoted"
            assert trace["fact_id"] == result["fact_id"]
            graph = FactGraph(Path(d))
            assert graph.list() == [result["fact_id"]]
            assert (
                graph.glossary()["COMMITTED_GATEWAY_X_481"]
                == "the durable gateway test object"
            )
    finally:
        FactGraph._unlink_durable = original_unlink

    original_cleanup = FactGraph._cleanup_committed_add_unlocked
    try:
        for index, injected_error in enumerate((OSError(), MemoryError())):

            def fail_whole_cleanup(self, _error=injected_error):
                raise _error

            FactGraph._cleanup_committed_add_unlocked = fail_whole_cleanup
            with (
                tempfile.TemporaryDirectory() as d,
                _env(
                    DANUS_PROJECT_DIR=d,
                    DANUS_AGENTS_ROOT=None,
                    DANUS_AUTHOR="worker_high",
                    DANUS_VERIFY_URL="http://mock",
                    DANUS_PROBLEM_ID="P",
                ),
                _mock_verify("correct"),
            ):
                result = server.fact_submit(
                    statement=f"Committed before whole cleanup failure {index}",
                    proof="A complete proof for the durable commit regression.",
                )
                assert result["accepted"] is True
                assert result["promoted"] is True
                assert result["submission_status"] == "promoted"
                assert isinstance(result["fact_id"], str)
                assert "write_error" not in result

                trace = GlobalMemory(Path(d)).read("verification")[-1]
                assert trace["promoted"] is True
                assert trace["submission_status"] == "promoted"
                assert trace["fact_id"] == result["fact_id"]
                graph = FactGraph(Path(d))
                assert graph.pending_add_path.exists()
                assert graph.pending_add_commit_path.exists()
                assert graph.list() == [result["fact_id"]]
    finally:
        FactGraph._cleanup_committed_add_unlocked = original_cleanup

    original_atomic_write = FactGraph._atomic_write_text
    original_fsync_directory = FactGraph._fsync_directory

    def inject_ambiguous_markers(self, path, text):
        if path == self.pending_add_commit_path:
            original_atomic_write(self, path, text)
            raise MemoryError("injected error after durable commit marker")
        if path == self.pending_add_abort_path:
            path.write_text(text, encoding="utf-8")
            raise OSError("injected error before rollback-marker durability")
        original_atomic_write(self, path, text)

    def fail_abort_directory_fsync(directory):
        if (directory / ".pending_add.rollback_required.json").exists():
            raise OSError("injected rollback-marker fsync failure")
        original_fsync_directory(directory)

    FactGraph._atomic_write_text = inject_ambiguous_markers
    FactGraph._fsync_directory = staticmethod(fail_abort_directory_fsync)
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="worker_high",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ),
            _mock_verify("correct"),
        ):
            result = server.fact_submit(
                statement="A candidate with an unknowable storage outcome",
                proof="A complete proof for the durability ambiguity regression.",
            )
            assert result["accepted"] is True
            assert result["promoted"] is None
            assert result["submission_status"] == "promotion_unknown"
            assert result["fact_id"] is None
            assert "fact_graph_promotion_unknown" in result["write_error"]

            trace = GlobalMemory(Path(d)).read("verification")[-1]
            assert trace["promoted"] is None
            assert trace["submission_status"] == "promotion_unknown"
            assert trace["fact_id"] is None
            assert "fact_graph_promotion_unknown" in trace["write_error"]

            graph = FactGraph(Path(d))
            assert graph.pending_add_path.exists()
            assert graph.pending_add_commit_path.exists()
            assert graph.pending_add_abort_path.exists()
            with pytest.raises(ValueError, match="fact_graph_recovery_required"):
                graph.list()

            # Power loss may discard the abort entry whose fsync failed.  The
            # restart then preserves the durable commit; the response above was
            # explicitly unknown, never a definitive false promotion.
            graph.pending_add_abort_path.unlink()
            assert len(FactGraph(Path(d)).list()) == 1
    finally:
        FactGraph._atomic_write_text = original_atomic_write
        FactGraph._fsync_directory = staticmethod(original_fsync_directory)


def test_fact_submit_exact_retry_remains_promoted_without_rewrite():
    """A lost success response can be retried without a false failed promotion."""
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
        _mock_verify("correct"),
    ):
        submit_kwargs = {
            "statement": "An exactly retried verified candidate",
            "proof": "A complete proof for the idempotent retry regression.",
            "glossary_introduces": {
                "IDEMPOTENT_GATEWAY_X_327": "the idempotent gateway test object"
            },
        }
        first = server.fact_submit(**submit_kwargs)
        assert first["promoted"] is True
        assert isinstance(first["fact_id"], str)

        original_fsync_directory = FactGraph._fsync_directory
        fact_fsync_attempted = False

        def reject_redundant_fact_fsync(directory):
            nonlocal fact_fsync_attempted
            if directory.name == "facts":
                fact_fsync_attempted = True
                raise OSError("redundant fact rewrite must not run")
            original_fsync_directory(directory)

        FactGraph._fsync_directory = staticmethod(reject_redundant_fact_fsync)
        try:
            retry = server.fact_submit(**submit_kwargs)
        finally:
            FactGraph._fsync_directory = staticmethod(original_fsync_directory)

        assert fact_fsync_attempted is False
        assert retry["accepted"] is True
        assert retry["promoted"] is True
        assert retry["submission_status"] == "promoted"
        assert retry["fact_id"] == first["fact_id"]
        assert "write_error" not in retry
        graph = FactGraph(Path(d))
        assert graph.list() == [first["fact_id"]]
        assert not graph.pending_add_path.exists()
        assert not graph.pending_add_commit_path.exists()

        traces = GlobalMemory(Path(d)).read("verification")
        assert [trace["promoted"] for trace in traces[-2:]] == [True, True]
        assert [trace["fact_id"] for trace in traces[-2:]] == [
            first["fact_id"],
            first["fact_id"],
        ]


def test_fact_submit_active_exact_reuse_calls_no_verifier_or_fact_writer(
    monkeypatch: pytest.MonkeyPatch,
):
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://127.0.0.1:9/verify",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        kwargs = {
            "statement": "A fact whose exact retry is already active",
            "proof": "A complete proof of the active exact fact.",
            "glossary_introduces": {
                "ACTIVE_EXACT_X_912": "the active exact test object"
            },
        }
        with _mock_verify("correct"):
            first = server.fact_submit(**kwargs)
        graph = FactGraph(Path(d))
        fact_path = graph._path(first["fact_id"])
        before = (fact_path.stat().st_mtime_ns, fact_path.read_bytes())

        def must_not_open(*_args, **_kwargs):
            raise AssertionError("exact active reuse must not health-check or POST")

        monkeypatch.setattr(server.urllib.request, "urlopen", must_not_open)
        reused = server.fact_submit(
            **kwargs,
            intuition="new mutable intuition must not rewrite the active fact",
            external_refs=[{"key": "NEW", "title": "must not replace metadata"}],
        )

        assert reused["accepted"] is True and reused["promoted"] is True
        assert reused["verification_verdict"] == reused["verdict"] == "correct"
        assert reused["verification_calls"] == 0
        assert reused["verification_reuse"] == "active_exact_fact"
        assert reused["fact_id"] == first["fact_id"]
        assert (fact_path.stat().st_mtime_ns, fact_path.read_bytes()) == before
        trace = GlobalMemory(Path(d)).read("verification")[-1]
        assert trace["verification_calls"] == 0
        assert trace["verification_reuse"] == "active_exact_fact"
        assert trace["verification_rounds"] == []
        assert trace["evidence"] == "active exact fact reused; verifier calls: 0"


def test_fact_submit_revoked_identity_is_not_reused():
    calls = {"count": 0}

    def verify(statement, proof, fact_context=None, glossary_introduces=None):
        calls["count"] += 1
        return _verify_response(fact_context, candidate_proof=proof)

    original_verify = server._verify
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        graph = FactGraph(Path(d))
        fact_id = graph.add(
            problem_id="P",
            author="worker_high",
            statement="A revoked exact candidate",
            proof="A once-complete proof.",
        )
        graph.revoke(fact_id, reason="test revocation")
        server._verify = verify
        try:
            result = server.fact_submit(
                statement="A revoked exact candidate",
                proof="A once-complete proof.",
            )
        finally:
            server._verify = original_verify

        assert calls["count"] == 1
        assert result["accepted"] is True
        assert result["promoted"] is False
        assert result["verification_calls"] == 1
        assert "fact_revoked" in result["write_error"]
        assert "verification_reuse" not in result


def test_fact_submit_blocks_missing_and_revoked_before_verify():
    calls = {"count": 0}

    def must_not_verify(statement, proof, fact_context=None, glossary_introduces=None):
        calls["count"] += 1
        raise AssertionError("verifier must not be called")

    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        fg = FactGraph(Path(d))
        revoked = fg.add(problem_id="P", author="w", statement="A", proof="pf A")
        fg.revoke(revoked, reason="wrong")
        removed = fg.add(problem_id="P", author="w", statement="Removed", proof="pf")
        dangling = fg.add(
            problem_id="P",
            author="w",
            statement="Dangling",
            proof="uses missing",
            predecessors=[removed],
        )
        (fg.facts_dir / f"{removed}.md").unlink()
        original = server._verify
        server._verify = must_not_verify
        try:
            missing = server.fact_submit("B", "proof B", predecessors=[dangling])
            blocked = server.fact_submit("C", "proof C", predecessors=[revoked])
        finally:
            server._verify = original
        assert missing["accepted"] is False and missing["verdict"] == "error"
        assert f"missing predecessor fact_ids: {removed}" in missing["error"]
        assert blocked["accepted"] is False and blocked["verdict"] == "error"
        assert f"revoked predecessor fact_ids: {revoked}" in blocked["error"]
        assert calls["count"] == 0
        assert fg.list() == [dangling]
        assert GlobalMemory(Path(d)).read("verification") == []


def test_fact_submit_blocks_budget_omission_before_verify():
    calls = {"count": 0}

    def must_not_verify(statement, proof, fact_context=None, glossary_introduces=None):
        calls["count"] += 1
        raise AssertionError("verifier must not be called")

    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
            DANUS_VERIFY_CONTEXT_MAX_CHARS="1",
        ),
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A", proof="full proof A")
        original = server._verify
        server._verify = must_not_verify
        try:
            res = server.fact_submit("B", "proof B", predecessors=[base])
        finally:
            server._verify = original
        assert res["accepted"] is False and res["verdict"] == "error"
        assert "exceeds character budget 1" in res["error"] and base in res["error"]
        assert calls["count"] == 0 and fg.list() == [base]


def test_fact_submit_glossary_check_never_blocks():
    # a raising undefined_symbols must not block submission (advisory heuristic)
    orig = FactGraph.undefined_symbols

    def boom(self, **kw):
        raise RuntimeError("glossary heuristic bug")

    FactGraph.undefined_symbols = boom
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="w",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ),
            _mock_verify("correct"),
        ):
            res = server.fact_submit(statement="X thing", proof="because")
            assert res["accepted"] is True and res["undefined_symbols"] == []
    finally:
        FactGraph.undefined_symbols = orig


def test_fact_submit_nondict_verify_body_is_clean():
    # a valid-JSON but non-dict verify response must not crash the gate
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        orig = server._verify
        server._verify = (
            lambda statement, proof, fact_context=None, glossary_introduces=None: [
                "not",
                "a",
                "dict",
            ]
        )
        try:
            res = server.fact_submit(statement="s", proof="p")
            assert res["accepted"] is False and res["verdict"] == "error"
            assert "non-dict" in res["error"]
            assert FactGraph(Path(d)).list() == []  # nothing written
        finally:
            server._verify = orig


def test_fact_submit_invalid_or_inconsistent_verdict_never_writes():
    invalid_payloads = [
        {
            "output_schema_version": 2,  # legacy contract must fail closed
            "verification_status": "final",
            "verdict": "correct",
            "needs_expanded_proofs": [],
            "verification_report": {
                "summary": "legacy v2 acceptance",
                "critical_errors": [],
                "gaps": [],
            },
            "repair_hints": "",
        },
        {
            "output_schema_version": 3,
            "verification_status": "final",
            "verdict": "correct",
            "needs_expanded_proofs": [],
            "verification_report": {
                "summary": "has gap",
                "critical_errors": [],
                "gaps": [
                    {
                        "location": "proof",
                        "issue": "missing step",
                        "candidate_evidence": {
                            "source": "proof",
                            "line": 1,
                            "exact_line": "a complete proof",
                        },
                    }
                ],
            },
            "repair_hints": "",
        },
    ]
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        original = server._verify
        try:
            for payload in invalid_payloads:
                server._verify = (
                    lambda statement,
                    proof,
                    fact_context=None,
                    glossary_introduces=None,
                    p=payload: {
                        **p,
                        "verification_context_digest": fact_context["digest"],
                    }
                )
                result = server.fact_submit(statement="s", proof="a complete proof")
                assert result["accepted"] is False and result["verdict"] == "error"
                assert "invalid verdict payload" in result["error"]
        finally:
            server._verify = original
        assert FactGraph(Path(d)).list() == []
        traces = GlobalMemory(Path(d)).read("verification")
        assert len(traces) == 2
        assert all(trace["verdict"] == "error" for trace in traces)
        assert all(
            trace["verification_rounds"][-1]["verification_status"] == "error"
            for trace in traces
        )


def test_fact_submit_misquoted_finding_evidence_fails_closed_without_trace_or_fact():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        original = server._verify

        def misquote(statement, proof, fact_context=None, glossary_introduces=None):
            return {
                "output_schema_version": 3,
                "verification_status": "final",
                "verdict": "wrong",
                "needs_expanded_proofs": [],
                "repair_hints": "change the alleged strict bound",
                "verification_context_digest": fact_context["digest"],
                "verification_report": {
                    "summary": "strictness mismatch",
                    "critical_errors": [],
                    "gaps": [
                        {
                            "location": "proof line 1",
                            "issue": "The candidate allegedly used d < h.",
                            "candidate_evidence": {
                                "source": "proof",
                                "line": 1,
                                "exact_line": "The candidate proves d < h.",
                            },
                        }
                    ],
                },
            }

        server._verify = misquote
        try:
            result = server.fact_submit(
                statement="The non-strict bound holds.",
                proof="The candidate proves d <= h.",
            )
        finally:
            server._verify = original

        assert result["accepted"] is False
        assert result["submission_status"] == "error"
        assert result["verification_verdict"] is None
        assert result["verdict"] == "error"
        assert "not the verbatim candidate proof line 1" in result["error"]
        assert FactGraph(Path(d)).list() == []
        traces = GlobalMemory(Path(d)).read("verification")
        assert len(traces) == 1
        assert traces[0]["verdict"] == "error"
        assert traces[0]["verification_rounds"][-1]["verification_status"] == "error"


def test_fact_submit_rejects_old_service_without_context_attestation():
    """A rolling upgrade must not let an old service silently drop context."""
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        original = server._verify
        server._verify = (
            lambda statement, proof, fact_context=None, glossary_introduces=None: {
                "output_schema_version": 2,  # old unattested service
                "verification_status": "final",
                "verdict": "correct",
                "needs_expanded_proofs": [],
                "repair_hints": "",
                "verification_report": {
                    "summary": "legacy response",
                    "critical_errors": [],
                    "gaps": [],
                },
            }
        )
        try:
            result = server.fact_submit(statement="s", proof="a complete proof")
        finally:
            server._verify = original
        assert result["accepted"] is False and result["verdict"] == "error"
        assert "did not attest" in result["error"]
        assert FactGraph(Path(d)).list() == []
        traces = GlobalMemory(Path(d)).read("verification")
        assert len(traces) == 1
        assert traces[0]["verdict"] == "error"
        assert traces[0]["verification_rounds"][-1]["verification_status"] == "error"


def test_fact_submit_rechecks_context_after_verification_before_write():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ),
    ):
        fg = FactGraph(Path(d))
        predecessor = fg.add(
            problem_id="P", author="w", statement="A holds", proof="proof A"
        )
        original = server._verify

        def revoke_during_verify(
            statement, proof, fact_context=None, glossary_introduces=None
        ):
            FactGraph(Path(d)).revoke(predecessor, reason="race test")
            return {
                "output_schema_version": 3,
                "verification_status": "final",
                "verdict": "correct",
                "needs_expanded_proofs": [],
                "repair_hints": "",
                "verification_context_digest": fact_context["digest"],
                "verification_report": {
                    "summary": "mock",
                    "critical_errors": [],
                    "gaps": [],
                },
            }

        server._verify = revoke_during_verify
        try:
            result = server.fact_submit(
                statement="B follows",
                proof=f"use verified fact {predecessor}",
                predecessors=[predecessor],
            )
        finally:
            server._verify = original

        assert result["accepted"] is True and result["fact_id"] is None
        assert result["verification_verdict"] == "correct"
        assert result["promoted"] is False
        assert result["submission_status"] == "verified_not_promoted"
        assert "verification_context_changed" in result["write_error"]
        assert FactGraph(Path(d)).list() == []
        trace = GlobalMemory(Path(d)).read("verification")[-1]
        assert trace["verdict"] == "correct" and trace["write_error"]
        assert trace["promoted"] is False
        assert trace["submission_status"] == "verified_not_promoted"
        assert trace["verification_context_digest"].startswith("sha256:")


def test_role_env_default_and_build_app():
    # build_app(None) reads DANUS_ROLE (server._role) — exercises the env branch
    with _env(DANUS_ROLE="worker"):
        assert server._role() == "worker"
        app = build_app()  # role=None -> defaults to _role() (env)
        assert app is not None
    with _env(DANUS_ROLE=None):
        assert server._role() == "verifier"  # unset falls back read-only (fail-closed)


def test_project_by_name_without_agents_root_raises():
    # a project name is given but DANUS_AGENTS_ROOT is unset -> RuntimeError
    with _env(
        DANUS_AGENTS_ROOT=None,
        DANUS_PROJECT_DIR="/tmp/whatever",
        DANUS_ROLE="main",
    ):
        try:
            server._project("proj_a")
            assert False, "should require DANUS_AGENTS_ROOT to resolve by name"
        except RuntimeError as e:
            assert "DANUS_AGENTS_ROOT" in str(e)


def test_verify_http_roundtrip_and_errors():
    # exercise the REAL _verify (local HTTP, offline-safe on 127.0.0.1)
    import http.server
    import threading

    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_GET(self):
            assert self.path == "/health"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "status": "ok",
                        "pid": 1234,
                        "instance_nonce": "0" * 32,
                        "output_protocol_version": (
                            server.VERIFICATION_OUTPUT_PROTOCOL_VERSION
                        ),
                        "verifier_bundle_digest": _TEST_VERIFIER_BUNDLE_DIGEST,
                    }
                ).encode("utf-8")
            )

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            captured["body"] = self.rfile.read(n).decode("utf-8")
            captured["ctype"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"verdict": "correct", "verification_report": {"ok": true}}'
            )

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/verify"
    try:
        # not set -> RuntimeError
        with _env(DANUS_VERIFY_URL=None):
            try:
                server._verify("s", "p")
                assert False, "should raise when DANUS_VERIFY_URL unset"
            except RuntimeError as e:
                assert "DANUS_VERIFY_URL" in str(e)
        # a real POST round-trip; the body is the JSON we sent
        with _env(DANUS_VERIFY_URL=url, DANUS_VERIFY_TIMEOUT="5"):
            fact_context = {
                "facts": [],
                "complete": True,
                "truncated": False,
                "missing_fact_ids": [],
                "revoked_fact_ids": [],
                "omitted_fact_ids": [],
                "characters_used": 0,
                "character_budget": 200000,
            }
            out = server._verify("S(n)=n^2", "induction", fact_context=fact_context)
            assert out["verdict"] == "correct"
        sent = json.loads(captured["body"])
        assert sent["expected_verifier_instance_nonce"] == "0" * 32
        assert sent["expected_output_protocol_version"] == 3
        assert sent["expected_verifier_bundle_digest"] == _TEST_VERIFIER_BUNDLE_DIGEST
        assert sent["statement"] == "S(n)=n^2"
        assert sent["fact_context"] == fact_context
        assert captured["ctype"] == "application/json"
        # a garbage timeout falls back to the default (no crash)
        with _env(DANUS_VERIFY_URL=url, DANUS_VERIFY_TIMEOUT="not-an-int"):
            assert server._verify("s", "p")["verdict"] == "correct"
    finally:
        srv.shutdown()


def test_fact_revoke_cascades():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="main_agent",
        ),
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        child = fg.add(
            problem_id="P",
            author="w",
            statement="B from A",
            proof="uses A",
            predecessors=[base],
        )
        out = server.fact_revoke(base, reason="A was wrong")
        assert set(out["revoked"]) == {base, child}
        assert not fg.exists(base) and not fg.exists(child)


def test_search_arxiv_theorems_delegates(monkeypatch=None):
    # the tool is a thin wrapper over danus.integrations.search; stub it (offline)
    orig = server._arxiv_search
    server._arxiv_search = lambda query, num_results=10: {
        "query": query,
        "num_results": num_results,
        "results": [{"title": "T"}],
    }
    try:
        out = server.search_arxiv_theorems("Beatty sequence", num_results=3)
        assert out["query"] == "Beatty sequence" and out["num_results"] == 3
        assert out["results"] == [{"title": "T"}]
    finally:
        server._arxiv_search = orig


def test_project_resolution_by_name_and_validation():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "proj_a").mkdir()
        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=None,
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="main",
        ):
            # main addresses a project by name
            out = server.gm_add(
                "master_guidance", claim="try route X", evidence="", project="proj_a"
            )
            assert out["id"]
            assert (
                server.gm_get(out["id"], project="proj_a")["kind"] == "master_guidance"
            )
            assert GlobalMemory(Path(root) / "proj_a").read("master_guidance")
            # path-escape / bad names are rejected
            for bad in ("../evil", "a/b", "", "/abs"):
                try:
                    server.gm_search("x", project=bad)
                    assert False, f"should reject project name {bad!r}"
                except RuntimeError:
                    pass
            # unknown project rejected
            try:
                server.gm_search("x", project="missing")
                assert False, "should reject unknown project"
            except RuntimeError:
                pass


def test_master_guidance_browser_provenance_requires_adopted_receipt():
    prompt = "Bounded mathematical checkpoint."
    raw_response = "The browser suggests a boundary lemma."
    strategy = "Investigate the boundary lemma under the verified hypotheses."
    with tempfile.TemporaryDirectory() as root:
        project = Path(root) / "proj_a"
        project.mkdir()
        other = Path(root) / "proj_b"
        other.mkdir()
        broker = BrowserAdvisorBroker(project)
        prepared = _prepare_browser_request(
            broker, prompt, elaboration_id="elaboration-1", context_id="cycle-1"
        )
        broker.authorize(
            prepared["request_id"],
            prompt_sha256=prepared["prompt_sha256"],
            authorization_scope="Owner permits this exact external transmission.",
            acknowledge_external_transmission=True,
        )
        broker.dispatch_started(prepared["request_id"])
        broker.submitted(
            prepared["request_id"],
            observed_prompt_sha256=prepared["prompt_sha256"],
            ui_mode="Pro",
            full_prompt_observed=True,
            conversation_url="https://chatgpt.com/c/gateway-offline-test",
        )
        broker.complete(
            prepared["request_id"],
            response=raw_response,
            observed_prompt_sha256=prepared["prompt_sha256"],
            ui_mode="Pro",
            conversation_url="https://chatgpt.com/c/gateway-offline-test",
            stable_snapshots=2,
            completion_actions_observed=True,
            composer_available=True,
            working_indicator_absent=True,
        )
        broker.import_result(prepared["request_id"], response=raw_response)
        adopted = broker.adopt(
            prepared["request_id"],
            strategy=strategy,
            acknowledge_untrusted_review=True,
        )
        provenance = adopted["consult_provenance"]
        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=None,
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="main",
        ):
            result = server.gm_add(
                "master_guidance",
                claim="reviewed browser strategy",
                evidence=strategy,
                consult_provenance=provenance,
                project="proj_a",
            )
            stored = GlobalMemory(project).read("master_guidance")
            assert stored[0]["id"] == result["id"]
            assert stored[0]["consult_provenance"] == provenance

            counts_before = {
                kind: len(GlobalMemory(project).read(kind))
                for kind in ("master_guidance", "elaboration", "direction")
            }
            for kind, claim, raw_evidence, glossary, links in (
                (
                    "master_guidance",
                    "unreviewed browser reply",
                    raw_response,
                    None,
                    None,
                ),
                ("elaboration", raw_response, "benign", None, None),
                (
                    "direction",
                    "nested raw payload",
                    "benign",
                    {"route": raw_response},
                    {"notes": [raw_response]},
                ),
            ):
                with pytest.raises(BrowserAdvisorConflict, match="raw browser output"):
                    server.gm_add(
                        kind,
                        claim=claim,
                        evidence=raw_evidence,
                        glossary=glossary,
                        links=links,
                        project="proj_a",
                    )
            assert counts_before == {
                kind: len(GlobalMemory(project).read(kind)) for kind in counts_before
            }

            with _env(
                DANUS_AGENTS_ROOT=root,
                DANUS_PROJECT_DIR=str(project),
                DANUS_AUTHOR="main_agent",
                DANUS_ROLE="worker",
            ):
                with pytest.raises(RuntimeError, match="only the main role"):
                    server.gm_add("elaboration", claim=raw_response, evidence="benign")
                with pytest.raises(BrowserAdvisorConflict, match="raw browser output"):
                    server.gm_add(
                        "obstacle", claim="worker raw leak", evidence=raw_response
                    )

            api_result = server.gm_add(
                "master_guidance",
                claim="normal API strategy",
                evidence="Try the second route.",
                input_tokens=12,
                output_tokens=34,
                cost_usd=0.25,
                project="proj_a",
            )
            api_entry = next(
                item
                for item in GlobalMemory(project).read("master_guidance")
                if item["id"] == api_result["id"]
            )
            assert api_entry["input_tokens"] == 12
            assert api_entry["output_tokens"] == 34
            assert api_entry["cost_usd"] == 0.25

            for unsafe in (
                {**provenance, "trust": "untrusted_strategy"},
                {**provenance, "input_tokens": 1},
                {**provenance, "cost_usd": 0},
                {
                    key: value
                    for key, value in provenance.items()
                    if key != "receipt_sha256"
                },
            ):
                with pytest.raises(ValueError):
                    server.gm_add(
                        "master_guidance",
                        claim="must fail closed",
                        evidence="raw imported browser text",
                        consult_provenance=unsafe,
                        project="proj_a",
                    )
            with pytest.raises(BrowserAdvisorConflict, match="evidence"):
                server.gm_add(
                    "master_guidance",
                    claim="hash mismatch",
                    evidence="Different synthesized strategy.",
                    consult_provenance=provenance,
                    project="proj_a",
                )
            for field in (
                "elaboration_id",
                "context_id",
                "binding_sha256",
                "receipt_sha256",
                "prompt_sha256",
                "reply_sha256",
                "adopted_strategy_sha256",
            ):
                changed = "b" * 64 if field.endswith("sha256") else "other-cycle"
                with pytest.raises(BrowserAdvisorConflict, match="exactly match"):
                    server.gm_add(
                        "master_guidance",
                        claim=f"tampered {field}",
                        evidence=strategy,
                        consult_provenance={**provenance, field: changed},
                        project="proj_a",
                    )
            with pytest.raises(BrowserAdvisorConflict, match="missing"):
                server.gm_add(
                    "master_guidance",
                    claim="missing same-project row",
                    evidence=strategy,
                    consult_provenance={**provenance, "request_id": "missing-row"},
                    project="proj_a",
                )
            with pytest.raises(BrowserAdvisorError, match="same-project"):
                server.gm_add(
                    "master_guidance",
                    claim="cross-project replay",
                    evidence=strategy,
                    consult_provenance=provenance,
                    project="proj_b",
                )
            assert not (other / ".advisor").exists()
            with pytest.raises(ValueError, match="only for master_guidance"):
                server.gm_add(
                    "direction",
                    claim="wrong kind",
                    evidence="",
                    consult_provenance=provenance,
                    project="proj_a",
                )


@pytest.mark.parametrize("target", ["completed", "needs_user_input"])
def test_raw_fence_gm_first_blocks_later_browser_digest_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
):
    """A GM append linearized first makes terminal raw registration fail."""

    project = tmp_path / "project"
    project.mkdir()
    broker, request = _submitted_browser_request(project)
    raw = f"RAW-GM-FIRST-{target}-CANARY"
    scan_finished = threading.Event()
    release_scan = threading.Event()
    gm_done = threading.Event()
    terminal_done = threading.Event()
    outcomes: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    original = BrowserAdvisorBroker.reject_raw_project_text_locked

    def pause_after_scan(cls, project_dir, *, fields):
        original(project_dir, fields=fields)
        scan_finished.set()
        if not release_scan.wait(5):
            raise AssertionError("test barrier timed out after raw-digest scan")

    monkeypatch.setattr(
        BrowserAdvisorBroker,
        "reject_raw_project_text_locked",
        classmethod(pause_after_scan),
    )

    def add_memory() -> None:
        try:
            outcomes["gm"] = server.gm_add(
                "direction", claim="race probe", evidence=raw
            )
        except BaseException as exc:  # captured for deterministic assertions
            errors["gm"] = exc
        finally:
            gm_done.set()

    def finish_browser() -> None:
        try:
            outcomes["terminal"] = _record_browser_terminal(
                broker, request, target=target, response=raw
            )
        except BaseException as exc:  # captured for deterministic assertions
            errors["terminal"] = exc
        finally:
            terminal_done.set()

    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="main_agent",
        DANUS_ROLE="main",
    ):
        gm_thread = threading.Thread(target=add_memory, daemon=True)
        terminal_thread = threading.Thread(target=finish_browser, daemon=True)
        gm_thread.start()
        assert scan_finished.wait(5)
        terminal_thread.start()
        assert not terminal_done.wait(0.2), "terminal bypassed the held GM fence"
        release_scan.set()
        gm_thread.join(5)
        terminal_thread.join(5)

    assert not gm_thread.is_alive() and not terminal_thread.is_alive()
    assert "gm" in outcomes and "gm" not in errors
    assert isinstance(errors.get("terminal"), BrowserAdvisorConflict)
    assert broker.get(request["request_id"])["state"] == "submitted"
    stored = GlobalMemory(project).read("direction")
    assert stored and stored[0]["evidence"] == raw
    assert broker.get(request["request_id"])["reply_sha256"] is None


@pytest.mark.parametrize("target", ["completed", "needs_user_input"])
def test_raw_fence_browser_digest_first_blocks_later_gm_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
):
    """A terminal digest linearized first makes the exact GM append fail."""

    project = tmp_path / "project"
    project.mkdir()
    broker, request = _submitted_browser_request(project)
    raw = f"RAW-BROWSER-FIRST-{target}-CANARY"
    scan_finished = threading.Event()
    release_scan = threading.Event()
    gm_done = threading.Event()
    terminal_done = threading.Event()
    outcomes: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    original_scan = browser_advisor_module._assert_digest_absent_from_global_memory

    def pause_browser_after_gm_scan(project_dir, *, digest):
        original_scan(project_dir, digest=digest)
        scan_finished.set()
        if not release_scan.wait(5):
            raise AssertionError("test barrier timed out after global-memory scan")

    monkeypatch.setattr(
        browser_advisor_module,
        "_assert_digest_absent_from_global_memory",
        pause_browser_after_gm_scan,
    )

    def finish_browser() -> None:
        try:
            outcomes["terminal"] = _record_browser_terminal(
                broker, request, target=target, response=raw
            )
        except BaseException as exc:
            errors["terminal"] = exc
        finally:
            terminal_done.set()

    def add_memory() -> None:
        try:
            outcomes["gm"] = server.gm_add(
                "direction", claim="race probe", evidence=raw
            )
        except BaseException as exc:
            errors["gm"] = exc
        finally:
            gm_done.set()

    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="main_agent",
        DANUS_ROLE="main",
    ):
        terminal_thread = threading.Thread(target=finish_browser, daemon=True)
        gm_thread = threading.Thread(target=add_memory, daemon=True)
        terminal_thread.start()
        assert scan_finished.wait(5)
        gm_thread.start()
        assert not gm_done.wait(0.2), "GM append bypassed the held browser fence"
        release_scan.set()
        terminal_thread.join(5)
        gm_thread.join(5)

    assert not gm_thread.is_alive() and not terminal_thread.is_alive()
    assert "terminal" in outcomes and "terminal" not in errors
    assert isinstance(errors.get("gm"), BrowserAdvisorConflict)
    assert broker.get(request["request_id"])["state"] == target
    assert GlobalMemory(project).read("direction") == []


def test_worker_alternate_environment_cannot_split_supervisor_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A same-UID worker process cannot redirect the authoritative lock root."""

    project = tmp_path / "project"
    project.mkdir()
    canonical_root = (
        Path(browser_advisor_module.__file__).resolve().parents[2]
        / "runtime"
        / "advisor-control"
    )
    alternate_root = tmp_path / "worker-selected-control"
    alternate_runtime = tmp_path / "worker-selected-runtime"
    monkeypatch.setattr(
        browser_advisor_module,
        "_canonical_control_root",
        lambda: canonical_root,
    )
    broker, request = _submitted_browser_request(project)
    raw = "RAW-CROSS-PROCESS-FENCE-CANARY"
    scan_finished = threading.Event()
    release_scan = threading.Event()
    terminal_done = threading.Event()
    terminal_outcome: dict[str, object] = {}
    original_scan = browser_advisor_module._assert_digest_absent_from_global_memory

    def pause_browser_after_gm_scan(project_dir, *, digest):
        original_scan(project_dir, digest=digest)
        scan_finished.set()
        if not release_scan.wait(10):
            raise AssertionError("cross-process test barrier timed out")

    monkeypatch.setattr(
        browser_advisor_module,
        "_assert_digest_absent_from_global_memory",
        pause_browser_after_gm_scan,
    )

    def finish_browser() -> None:
        try:
            terminal_outcome["result"] = _record_browser_terminal(
                broker, request, target="completed", response=raw
            )
        except BaseException as exc:
            terminal_outcome["error"] = exc
        finally:
            terminal_done.set()

    terminal_thread = threading.Thread(target=finish_browser, daemon=True)
    terminal_thread.start()
    assert scan_finished.wait(5)
    parent_lock = browser_advisor_module._project_fence_path(project)
    parent_stat = os.stat(parent_lock)

    child_code = r"""
import json
import os
import sys
from pathlib import Path

from danus.strategy import browser_advisor as advisor

project = Path(sys.argv[1])
raw = sys.argv[2]
from danus.gateway import server

print("READY", flush=True)
try:
    server.gm_add("obstacle", claim="worker race probe", evidence=raw)
except BaseException as exc:
    outcome = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
else:
    outcome = {"ok": True}
lock = advisor._project_fence_path(project)
item = os.stat(lock)
outcome.update(root=str(lock.parent), dev=item.st_dev, ino=item.st_ino)
print(json.dumps(outcome, sort_keys=True), flush=True)
"""
    child_env = os.environ.copy()
    child_env.update(
        DANUS_PROJECT_DIR=str(project),
        DANUS_ROLE="worker",
        DANUS_AUTHOR="spoofed-main-agent",
        DANUS_ADVISOR_CONTROL_ROOT=str(alternate_root),
        DANUS_RUNTIME=str(alternate_runtime),
    )
    child_env.pop("DANUS_AGENTS_ROOT", None)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(project),
            raw,
        ],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    try:
        assert child.stdout.readline().strip() == "READY"
        readable, _, _ = select.select([child.stdout], [], [], 0.3)
        assert readable == [], "worker acquired an alternate flock domain"
    finally:
        release_scan.set()

    terminal_thread.join(5)
    assert terminal_done.is_set() and not terminal_thread.is_alive()
    assert "error" not in terminal_outcome
    line = child.stdout.readline()
    stdout_tail, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    assert not stdout_tail.strip(), stdout_tail
    outcome = json.loads(line)
    assert outcome["ok"] is False
    assert outcome["error_type"] == "BrowserAdvisorConflict"
    assert "raw browser output" in outcome["error"]
    assert outcome["root"] == str(canonical_root)
    assert (outcome["dev"], outcome["ino"]) == (
        parent_stat.st_dev,
        parent_stat.st_ino,
    )
    assert not alternate_root.exists() and not alternate_runtime.exists()
    assert broker.get(request["request_id"])["state"] == "completed"
    assert GlobalMemory(project).read("obstacle") == []


@pytest.mark.parametrize(
    "links",
    [
        {"RAW-NESTED-KEY-CANARY": "first", "later": "benign"},
        {"first": "benign", "RAW-NESTED-KEY-CANARY": "middle", "last": "ok"},
        {"first": "benign", "RAW-NESTED-KEY-CANARY": "last"},
        {"outer": [{"safe": "ok", "RAW-NESTED-KEY-CANARY": "nested"}]},
        {"outer": [{"safe": "RAW-NESTED-KEY-CANARY"}, ["RAW-NESTED-KEY-CANARY"]]},
    ],
    ids=["key-first", "key-middle", "key-last", "nested-key", "value-and-list"],
)
def test_raw_digest_collector_rejects_every_nested_key_and_value(
    tmp_path: Path, links: dict
):
    project = tmp_path / "project"
    project.mkdir()
    raw = "RAW-NESTED-KEY-CANARY"
    broker, request = _submitted_browser_request(project)
    _record_browser_terminal(broker, request, target="completed", response=raw)
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="main_agent",
        DANUS_ROLE="main",
    ):
        with pytest.raises(BrowserAdvisorConflict, match="raw browser output"):
            server.gm_add(
                "direction",
                claim="nested collector probe",
                evidence="benign",
                links=links,
            )
    assert GlobalMemory(project).read("direction") == []


def test_raw_digest_collector_allows_benign_nested_payload(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    broker, request = _submitted_browser_request(project)
    _record_browser_terminal(
        broker,
        request,
        target="completed",
        response="RAW-BENIGN-CONTROL-CANARY",
    )
    links = {"first": "safe", "outer": [{"nested": ["also safe"]}]}
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="main_agent",
        DANUS_ROLE="main",
    ):
        result = server.gm_add(
            "direction", claim="benign nested payload", evidence="", links=links
        )
    stored = GlobalMemory(project).read("direction")
    assert stored[0]["id"] == result["id"] and stored[0]["links"] == links


@pytest.mark.parametrize("verify_outcome", ["final-reject", "protocol-error"])
def test_non_gm_add_verification_trace_cannot_persist_registered_raw_output(
    tmp_path: Path, verify_outcome: str
):
    """Both production verification-trace append sites share the raw fence."""

    project = tmp_path / "project"
    project.mkdir()
    raw = f"RAW-VERIFICATION-TRACE-{verify_outcome}-CANARY"
    broker, request = _submitted_browser_request(project)
    _record_browser_terminal(broker, request, target="completed", response=raw)
    with _env(
        DANUS_PROJECT_DIR=str(project),
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="worker_high",
        DANUS_ROLE="worker",
        DANUS_VERIFY_URL="http://unused.test/verify",
    ):
        if verify_outcome == "final-reject":
            verify_context = _mock_verify("wrong", repair_hints=raw)
        else:
            verify_context = _mock_verify("wrong", raise_exc=RuntimeError(raw))
        with verify_context:
            result = server.fact_submit(
                statement="A benign candidate statement.",
                proof="A benign candidate proof.",
            )
    assert result["promoted"] is False
    assert "raw browser output" in result["trace_error"]
    assert GlobalMemory(project).read("verification") == []


def test_browser_guidance_rejects_worker_author_spoof_and_unadopted_receipts():
    digest = "a" * 64
    fake = {
        "schema_version": 1,
        "transport": "chatgpt_pro_browser",
        "request_id": "missing",
        "elaboration_id": None,
        "context_id": "cycle",
        "recommendation_id": None,
        "binding_sha256": digest,
        "receipt_sha256": digest,
        "prompt_sha256": digest,
        "reply_sha256": digest,
        "adopted_strategy_sha256": digest,
        "trust": "adopted_strategy",
        "billing_basis": "subscription",
        "model": None,
        "ui_mode": "Pro",
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }
    with tempfile.TemporaryDirectory() as root:
        project = Path(root) / "proj_a"
        project.mkdir()
        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=str(project),
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="worker",
        ):
            for kind in ("master_guidance", "elaboration"):
                with pytest.raises(RuntimeError, match="only the main role"):
                    server.gm_add(
                        kind,
                        claim="spoofed privileged finding",
                        evidence="fake reviewed strategy",
                        consult_provenance=fake if kind == "master_guidance" else None,
                    )
        assert GlobalMemory(project).read("master_guidance") == []
        assert GlobalMemory(project).read("elaboration") == []

        broker = BrowserAdvisorBroker(project)
        request = _prepare_browser_request(broker, "Question", context_id="cycle")
        abandoned = broker.abandon(request["request_id"], reason="owner cancelled")
        assert abandoned["state"] == "abandoned"
        unadopted = {**fake, "request_id": request["request_id"]}
        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=None,
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="main",
        ):
            with pytest.raises(BrowserAdvisorStateError, match="adopted"):
                server.gm_add(
                    "master_guidance",
                    claim="abandoned receipt replay",
                    evidence="fake reviewed strategy",
                    consult_provenance=unadopted,
                    project="proj_a",
                )


def test_reasoning_advisor_checkpoint_without_current_recommendation_writes_nothing(
    tmp_path: Path,
):
    project = tmp_path / "checkpoint-no-recommendation"
    store, _admissions = _active_reasoning_store(project, workers=2)
    fact_id = FactGraph(project).add(
        problem_id="P",
        author="xhigh",
        statement="A verified premise for the checkpoint.",
        proof="Direct verification.",
    )
    evidence = (
        "## Verified facts\n- Linked below.\n\n"
        "## Failed routes and evidence\n- The direct route failed.\n\n"
        "## Unresolved bottleneck\nA uniform bound is missing.\n\n"
        "## Candidate decision question\nWhich route should be prioritized?"
    )
    assert store.project_status()["phase"] == "root_critic_reasoning"
    with _env(
        DANUS_AGENTS_ROOT=str(tmp_path),
        DANUS_PROJECT_DIR=None,
        DANUS_AUTHOR="main_agent",
        DANUS_ROLE="main",
    ):
        with pytest.raises(RuntimeError, match="exact current recommendation id"):
            server.gm_add(
                "advisor_checkpoint",
                claim="checkpoint without coordinator authority",
                evidence=evidence,
                links={"fact_ids": [fact_id]},
                project=project.name,
            )
        with pytest.raises(RuntimeError, match="exact current ready recommendation"):
            server.gm_add(
                "advisor_checkpoint",
                claim="checkpoint with a fabricated recommendation",
                evidence=evidence,
                links={
                    "fact_ids": [fact_id],
                    "recommendation_id": "recommendation_wrong",
                },
                project=project.name,
            )
    assert GlobalMemory(project).read("advisor_checkpoint") == []


def test_reasoning_advisor_checkpoint_binds_only_exact_ready_recommendation(
    tmp_path: Path,
):
    project = tmp_path / "checkpoint-ready-recommendation"
    store, recommendation_id, review = _reasoning_recommendation(
        project,
        complete_review=False,
    )
    fact_id = FactGraph(project).add(
        problem_id="P",
        author="xhigh",
        statement="A verified premise for the ready checkpoint.",
        proof="Direct verification.",
    )
    evidence = (
        "## Verified facts\n- Linked below.\n\n"
        "## Failed routes and evidence\n- The direct route failed.\n\n"
        "## Unresolved bottleneck\nA uniform bound is missing.\n\n"
        "## Candidate decision question\nWhich route should be prioritized?"
    )

    def checkpoint(bound_recommendation_id: str):
        return server.gm_add(
            "advisor_checkpoint",
            claim="checkpoint bound to reviewed obstruction",
            evidence=evidence,
            links={
                "fact_ids": [fact_id],
                "recommendation_id": bound_recommendation_id,
            },
            project=project.name,
        )

    with _env(
        DANUS_AGENTS_ROOT=str(tmp_path),
        DANUS_PROJECT_DIR=None,
        DANUS_AUTHOR="main_agent",
        DANUS_ROLE="main",
    ):
        with pytest.raises(RuntimeError, match="exact current ready recommendation"):
            checkpoint(recommendation_id)
        assert GlobalMemory(project).read("advisor_checkpoint") == []

        store.complete(review.slot_id, outcome="terminal_rc_0")
        _stage_next_gateway_generation(store)
        with pytest.raises(RuntimeError, match="exact current ready recommendation"):
            checkpoint("recommendation_wrong")
        assert GlobalMemory(project).read("advisor_checkpoint") == []

        result = checkpoint(recommendation_id)
        stored = GlobalMemory(project).read("advisor_checkpoint")
        assert stored[0]["id"] == result["id"]
        assert stored[0]["links"] == {
            "fact_ids": [fact_id],
            "recommendation_id": recommendation_id,
        }
        recommendation = store.project_status()["recommendation"]
        assert recommendation["browser_dispatch_authorized"] is False
        assert recommendation["advisor_request_id"] is None
        assert (
            BrowserAdvisorBroker.recommendation_request(
                project,
                recommendation_id=recommendation_id,
            )
            is None
        )

        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
        FactGraph(project).revoke(fact_id, reason="checkpoint response was lost")
        assert checkpoint(recommendation_id) == result
        assert len(GlobalMemory(project).read("advisor_checkpoint")) == 1
        with pytest.raises(RuntimeError, match="retry conflicts"):
            server.gm_add(
                "advisor_checkpoint",
                claim="different checkpoint after resolution",
                evidence=evidence,
                links={
                    "fact_ids": [fact_id],
                    "recommendation_id": recommendation_id,
                },
                project=project.name,
            )
        assert len(GlobalMemory(project).read("advisor_checkpoint")) == 1
        with pytest.raises(BrowserAdvisorStateError, match="exact current open"):
            BrowserAdvisorBroker(project).prepare(
                evidence,
                context_id="stale-recommendation",
                recommendation_id=recommendation_id,
                checkpoint_id=result["checkpoint_id"],
                checkpoint_sha256=result["checkpoint_sha256"],
                checkpoint_bytes=result["checkpoint_bytes"],
            )


def test_reasoning_advisor_checkpoint_serializes_with_owner_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "checkpoint-resolution-race"
    store, recommendation_id, _review = _reasoning_recommendation(
        project,
        complete_review=True,
    )
    fact_id = FactGraph(project).add(
        problem_id="P",
        author="xhigh",
        statement="A verified premise for the serialized checkpoint.",
        proof="Direct verification.",
    )
    evidence = (
        "## Verified facts\n- Linked below.\n\n"
        "## Failed routes and evidence\n- The direct route failed.\n\n"
        "## Unresolved bottleneck\nA uniform bound is missing.\n\n"
        "## Candidate decision question\nWhich route should be prioritized?"
    )
    original_append = GlobalMemory.append
    resolution_started = threading.Event()
    resolution_finished = threading.Event()
    resolver_errors: list[BaseException] = []
    resolver_threads: list[threading.Thread] = []
    observed_block = {"value": False}

    def append_with_resolution_race(memory, *args, **kwargs):
        def resolve() -> None:
            resolution_started.set()
            try:
                with BrowserAdvisorBroker.project_memory_fence(project):
                    store.resolve_recommendation(
                        recommendation_id,
                        resolution="continue_without_advisor",
                        owner_acknowledgement=recommendation_id,
                    )
            except BaseException as exc:  # pragma: no cover - asserted below
                resolver_errors.append(exc)
            finally:
                resolution_finished.set()

        thread = threading.Thread(target=resolve)
        resolver_threads.append(thread)
        thread.start()
        assert resolution_started.wait(timeout=2)
        observed_block["value"] = not resolution_finished.wait(timeout=0.2)
        return original_append(memory, *args, **kwargs)

    monkeypatch.setattr(GlobalMemory, "append", append_with_resolution_race)
    with _env(
        DANUS_AGENTS_ROOT=str(tmp_path),
        DANUS_PROJECT_DIR=None,
        DANUS_AUTHOR="main_agent",
        DANUS_ROLE="main",
    ):
        result = server.gm_add(
            "advisor_checkpoint",
            claim="checkpoint serialized against owner resolution",
            evidence=evidence,
            links={
                "fact_ids": [fact_id],
                "recommendation_id": recommendation_id,
            },
            project=project.name,
        )

    resolver_threads[0].join(timeout=2)
    assert observed_block["value"] is True
    assert resolution_finished.is_set()
    assert resolver_errors == []
    stored = GlobalMemory(project).read("advisor_checkpoint")
    assert stored[0]["id"] == result["id"]
    assert stored[0]["links"]["recommendation_id"] == recommendation_id
    status = store.project_status()
    assert status["generation"] == 2
    assert status["resolution"]["recommendation_id"] == recommendation_id


def test_advisor_checkpoint_is_main_only_and_binds_active_verified_facts():
    evidence = (
        "## Verified facts\n"
        "- See the fact ids in links.\n\n"
        "## Failed routes and evidence\n"
        "- The direct route is recorded as a dead end.\n\n"
        "## Unresolved bottleneck\n"
        "A uniform estimate is missing.\n\n"
        "## Candidate decision question\n"
        "Which remaining route should be prioritized?"
    )
    with tempfile.TemporaryDirectory() as root:
        project = Path(root) / "proj_a"
        project.mkdir()
        graph = FactGraph(project)
        active = graph.add(
            problem_id="P",
            author="worker_high",
            statement="The reduction is valid.",
            proof="Direct verification.",
        )
        revoked = graph.add(
            problem_id="P",
            author="worker_high",
            statement="A route-specific lemma.",
            proof="Temporary proof.",
        )
        graph.revoke(revoked, reason="test revoked checkpoint source")

        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=str(project),
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="worker",
        ):
            with pytest.raises(RuntimeError, match="only the main role"):
                server.gm_add(
                    "advisor_checkpoint",
                    claim="spoofed worker checkpoint",
                    evidence=evidence,
                    links={"fact_ids": [active]},
                )

        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=None,
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="main",
        ):
            with pytest.raises(RuntimeError, match="cannot claim"):
                server.gm_add(
                    "advisor_checkpoint",
                    claim="legacy checkpoint with explicit null recommendation",
                    evidence=evidence,
                    links={"fact_ids": [active], "recommendation_id": None},
                    project="proj_a",
                )
            assert GlobalMemory(project).read("advisor_checkpoint") == []
            result = server.gm_add(
                "advisor_checkpoint",
                claim="late checkpoint for the uniform estimate",
                evidence=evidence,
                links={"fact_ids": [active]},
                project="proj_a",
            )
            stored = GlobalMemory(project).read("advisor_checkpoint")
            assert stored[0]["id"] == result["id"]
            assert stored[0]["links"]["fact_ids"] == [active]
            assert "recommendation_id" not in stored[0]["links"]
            assert (
                server.gm_add(
                    "advisor_checkpoint",
                    claim="late checkpoint for the uniform estimate",
                    evidence=evidence,
                    links={"fact_ids": [active]},
                    project="proj_a",
                )
                == result
            )
            assert len(GlobalMemory(project).read("advisor_checkpoint")) == 1

            for invalid_id, error in (
                ("0" * 16, "missing"),
                (revoked, "revoked"),
            ):
                with pytest.raises(ValueError, match=error):
                    server.gm_add(
                        "advisor_checkpoint",
                        claim="invalid fact checkpoint",
                        evidence=evidence,
                        links={"fact_ids": [invalid_id]},
                        project="proj_a",
                    )

        with _env(
            DANUS_AGENTS_ROOT=None,
            DANUS_PROJECT_DIR=None,
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="main",
        ):
            with pytest.raises(RuntimeError, match="DANUS_PROJECT_DIR"):
                server.gm_add(
                    "advisor_checkpoint",
                    claim="unscoped checkpoint",
                    evidence=evidence,
                    links={"fact_ids": []},
                )


def test_advisor_checkpoint_holds_fact_snapshot_through_memory_append(
    monkeypatch: pytest.MonkeyPatch,
):
    import threading

    evidence = (
        "## Verified facts\n- Linked below.\n\n"
        "## Failed routes and evidence\n- Direct route failed.\n\n"
        "## Unresolved bottleneck\nA bound is missing.\n\n"
        "## Candidate decision question\nWhich route should be prioritized?"
    )
    with tempfile.TemporaryDirectory() as root:
        project = Path(root) / "proj_a"
        project.mkdir()
        graph = FactGraph(project)
        fact_id = graph.add(
            problem_id="P",
            author="worker_high",
            statement="A verified checkpoint premise.",
            proof="Direct verification.",
        )
        original_append = GlobalMemory.append
        revoke_started = threading.Event()
        revoke_finished = threading.Event()
        revoke_thread: list[threading.Thread] = []
        observed_block = {"value": False}

        def append_with_interleaving(memory, *args, **kwargs):
            def revoke() -> None:
                revoke_started.set()
                graph.revoke(fact_id, reason="deterministic interleave")
                revoke_finished.set()

            thread = threading.Thread(target=revoke)
            revoke_thread.append(thread)
            thread.start()
            assert revoke_started.wait(timeout=2)
            observed_block["value"] = not revoke_finished.wait(timeout=0.2)
            return original_append(memory, *args, **kwargs)

        monkeypatch.setattr(GlobalMemory, "append", append_with_interleaving)
        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=None,
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="main",
        ):
            result = server.gm_add(
                "advisor_checkpoint",
                claim="transactional checkpoint",
                evidence=evidence,
                links={"fact_ids": [fact_id]},
                project="proj_a",
            )
        assert result["id"]
        assert observed_block["value"] is True
        revoke_thread[0].join(timeout=2)
        assert revoke_finished.is_set()
        assert GlobalMemory(project).read("advisor_checkpoint")[0]["links"] == {
            "fact_ids": [fact_id]
        }
        assert not graph.exists(fact_id)


def test_worker_cannot_select_or_poison_another_project():
    with tempfile.TemporaryDirectory() as root:
        own = Path(root) / "own"
        other = Path(root) / "other"
        own.mkdir()
        other.mkdir()
        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=str(own),
            DANUS_AUTHOR="worker_high",
            DANUS_ROLE="worker",
        ):
            for operation in (
                lambda: server.gm_add(
                    "master_guidance", claim="poison", evidence="", project="other"
                ),
                lambda: server.gm_search("x", project="other"),
                lambda: server.gm_get("0" * 16, project="other"),
                lambda: server.fact_search("x", project="other"),
                lambda: server.fact_context([], project="other"),
            ):
                with pytest.raises(RuntimeError, match="only the main role"):
                    operation()
        assert GlobalMemory(other).read("master_guidance") == []


def test_project_resolution_rejects_symlinked_selector_and_pinned_project():
    with (
        tempfile.TemporaryDirectory() as root,
        tempfile.TemporaryDirectory() as outside,
    ):
        linked = Path(root) / "linked"
        linked.symlink_to(Path(outside), target_is_directory=True)
        with _env(
            DANUS_AGENTS_ROOT=root,
            DANUS_PROJECT_DIR=None,
            DANUS_AUTHOR="main_agent",
            DANUS_ROLE="main",
        ):
            with pytest.raises(RuntimeError, match="unsafe project path"):
                server._project("linked")
        with _env(
            DANUS_AGENTS_ROOT=None,
            DANUS_PROJECT_DIR=str(linked),
            DANUS_AUTHOR="worker_high",
            DANUS_ROLE="worker",
        ):
            with pytest.raises(RuntimeError, match="real directory"):
                server._project()


def test_main_module_builds_and_runs():
    # `python -m danus.gateway` builds an app from DANUS_ROLE and calls .run();
    # stub FastMCP.run so no stdio server actually starts.
    import runpy
    from danus._mcp import FastMCP

    orig_run = FastMCP.run
    calls = {"n": 0}
    FastMCP.run = lambda self, *a, **k: calls.__setitem__("n", calls["n"] + 1)
    try:
        with _env(DANUS_ROLE="verifier"):
            runpy.run_module("danus.gateway", run_name="__main__")
        assert calls["n"] == 1
    finally:
        FastMCP.run = orig_run


def main() -> None:
    test_role_table()
    print("  [ok] role table (main no fact_submit; verifier read-only; worker submits)")
    test_role_env_default_and_build_app()
    print("  [ok] build_app reads DANUS_ROLE; _role default")
    test_project_by_name_without_agents_root_raises()
    print("  [ok] project-by-name without DANUS_AGENTS_ROOT -> RuntimeError")
    test_verify_http_roundtrip_and_errors()
    print("  [ok] _verify HTTP round-trip + unset-URL + bad-timeout fallback")
    test_fact_revoke_cascades()
    print("  [ok] fact_revoke cascades to descendants")
    test_search_arxiv_theorems_delegates()
    print("  [ok] search_arxiv_theorems delegates to integrations.search")
    test_main_module_builds_and_runs()
    print("  [ok] python -m danus.gateway builds app + calls run()")
    test_gm_and_fact_search_over_temp_project()
    print("  [ok] gm_add / gm_search / fact_search over a temp project")
    test_fact_context_gateway_defaults_to_summary_only()
    print("  [ok] fact_context default summary + explicit selected proof")
    test_fact_submit_accept_writes_fact_and_traces()
    print("  [ok] fact_submit accept -> writes fact + verification trace")
    test_fact_submit_reject_writes_nothing_but_traces()
    print("  [ok] fact_submit reject -> writes nothing, still traces")
    test_fact_submit_verify_error_is_clean()
    print("  [ok] fact_submit verify-error -> clean error, no verdict")
    test_fact_submit_sends_full_statement_closure_and_no_ancestor_proofs()
    print("  [ok] fact_submit sends statement/definition-only predecessor closure")
    test_fact_submit_blocks_missing_and_revoked_before_verify()
    print("  [ok] fact_submit blocks missing/revoked context before verifier")
    test_fact_submit_blocks_budget_omission_before_verify()
    print("  [ok] fact_submit blocks omitted context before verifier")
    test_fact_submit_glossary_check_never_blocks()
    print("  [ok] fact_submit glossary heuristic never blocks submission")
    test_fact_submit_nondict_verify_body_is_clean()
    print("  [ok] fact_submit non-dict verify body -> clean error, nothing written")
    test_project_resolution_by_name_and_validation()
    print("  [ok] project resolution by name + path-escape validation")
    print("ALL GATEWAY TESTS PASSED")


if __name__ == "__main__":
    main()
