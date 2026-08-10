from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from danus.coordination import (
    CRITIC_REVIEW_PHASE,
    DEFAULT_COORDINATION,
    CoordinationError,
    CoordinationStore,
    candidate_outcome_releases,
    candidate_receipt_id,
    coordination_config,
)
from danus.coordination.store import PREPARED_DEADLINE_OUTCOME


@pytest.mark.parametrize(
    ("outcome", "releases"),
    [
        ("correct", True),
        ("wrong", True),
        ("needs_context", True),
        ("error", True),
        ("promotion_unknown", True),
        ("outcome_unknown", False),
    ],
)
def test_candidate_outcome_release_policy(outcome: str, releases: bool) -> None:
    assert candidate_outcome_releases(outcome) is releases


def test_candidate_receipt_canonical_json_binds_explicit_null_source() -> None:
    assert (
        candidate_receipt_id(
            slot_id="slot_abc",
            candidate_fact_id="a" * 16,
            candidate_fact_identity="d" * 64,
            source_id=None,
            context_digest="b" * 64,
        )
        == "9271213630f887f110e8c2117092ea022f5c61950f04d16a5de47b0345e06d0e"
    )
    assert (
        candidate_receipt_id(
            slot_id="slot_abc",
            candidate_fact_id="a" * 16,
            candidate_fact_identity="d" * 64,
            source_id="c" * 16,
            context_digest="b" * 64,
        )
        == "e8257eb0d8d215df7bc6f9e4abc902f1c43cafae9131a564e73448164396c411"
    )


def test_full_identity_separates_forced_short_id_collision_and_fences_overlay(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    common = {
        "slot_id": root.slot_id,
        "candidate_fact_id": "a" * 16,
        "source_id": None,
        "context_digest": "b" * 64,
    }
    first_receipt = candidate_receipt_id(
        **common,
        candidate_fact_identity="c" * 64,
    )
    collision_receipt = candidate_receipt_id(
        **common,
        candidate_fact_identity="d" * 64,
    )
    assert first_receipt != collision_receipt
    first = store.register_candidate(
        "xhigh",
        first_receipt,
        **common,
        candidate_fact_identity="c" * 64,
    )

    with pytest.raises(CoordinationError, match="another candidate overlay"):
        store.register_candidate(
            "xhigh",
            collision_receipt,
            **common,
            candidate_fact_identity="d" * 64,
        )
    with pytest.raises(CoordinationError, match="not terminalizable"):
        store.terminalize_candidate(
            "xhigh",
            collision_receipt,
            slot_id=root.slot_id,
            outcome="wrong",
        )
    with pytest.raises(CoordinationError, match="does not bind"):
        store.register_candidate(
            "xhigh",
            first_receipt,
            **common,
            candidate_fact_identity="d" * 64,
        )
    assert store.project_status()["candidate"] == first


def _project(
    tmp_path: Path,
    *,
    roles: str = "high:3,xhigh:4",
    workers: list[str] | None = None,
) -> tuple[Path, dict[str, object]]:
    roster = workers or [
        "high",
        "high2",
        "high3",
        "xhigh",
        "xhigh2",
        "xhigh3",
        "xhigh4",
    ]
    project = tmp_path / "project"
    project.mkdir()
    metadata: dict[str, object] = {
        "name": "project",
        "model": "model",
        "roles": roles,
        "workers": roster,
        "coordination": dict(DEFAULT_COORDINATION),
    }
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    bootstrap = CoordinationStore(project, metadata)
    status = bootstrap.project_status()
    for worker in (status["root_worker"], status["critic_worker"]):
        if worker is not None:
            bootstrap.stage_task_assignment(
                str(worker),
                f"# Generation 1 assignment for {worker}\n",
            )
    return project, metadata


def _bound_prompt(admission, body: str | None = None) -> str:
    content = admission.directive if body is None else body
    return (
        f"{content}\n\n"
        f"coordination_slot_id={admission.slot_id}\n"
        f"generation={admission.generation}\n"
        f"task_sha256={admission.task_sha256}\n"
    )


def _stage_next_generation(store: CoordinationStore) -> dict[str, object]:
    status = store.project_status()
    assert status["phase"] == "owner_action_required"
    target = int(status["generation"]) + 1
    for worker in (status["root_worker"], status["critic_worker"]):
        if worker is not None:
            store.stage_task_assignment(
                str(worker),
                f"# Generation {target} assignment for {worker}\n",
            )
    return store.staged_task_assignments()


def test_new_store_starts_task_empty_and_admission_fails_closed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "empty-task-project"
    project.mkdir()
    metadata: dict[str, object] = {
        "name": "empty-task-project",
        "model": "model",
        "roles": "xhigh:2",
        "workers": ["xhigh", "xhigh2"],
        "coordination": dict(DEFAULT_COORDINATION),
    }
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    store = CoordinationStore(project, metadata)

    coverage = store.staged_task_assignments()
    assert coverage == {
        "generation": 1,
        "required_workers": ["xhigh", "xhigh2"],
        "assignments": [],
        "missing_workers": ["xhigh", "xhigh2"],
        "ready": False,
    }
    assert store.project_status()["fail_stop_reason"] == (
        "durable_task_assignment_required"
    )
    with pytest.raises(CoordinationError, match="no durable task assignment"):
        store.admit("xhigh")

    root_task = "# Exact root task\n"
    staged = store.stage_task_assignment("xhigh", root_task, now=10.0)
    assert staged["task_sha256"] == hashlib.sha256(root_task.encode()).hexdigest()
    assert staged["task_bytes"] == len(root_task.encode())
    assert staged["generation"] == 1
    assert staged["frozen"] is False
    assert root_task not in json.dumps(store.project_status())
    with pytest.raises(CoordinationError, match="exceeds its hard limit"):
        store.stage_task_assignment("xhigh2", "x" * 131_073)


def test_task_stage_replace_replay_and_post_slot_freeze(
    tmp_path: Path,
) -> None:
    project, metadata = _project(
        tmp_path,
        roles="xhigh:2",
        workers=["xhigh", "xhigh2"],
    )
    store = CoordinationStore(project, metadata)
    replacement = "# Replacement root task\n"
    first = store.stage_task_assignment("xhigh", replacement)
    replay = store.stage_task_assignment("xhigh", replacement)
    assert first["replaced"] is True
    assert replay["replayed"] is True
    assert replay["task_sha256"] == first["task_sha256"]

    root = store.admit("xhigh")
    assert root is not None
    assert root.task == replacement
    assert root.task_sha256 == first["task_sha256"]
    assert root.task_bytes == len(replacement.encode())
    with pytest.raises(CoordinationError, match="frozen for this generation"):
        store.stage_task_assignment("xhigh", "# Too late\n")
    assert (
        store.stage_task_assignment(
            "xhigh",
            replacement,
        )["replayed"]
        is True
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "generation_task",
        "slot_task",
        "prompt_task_digest",
        "prompt_markers",
        "legacy_live_slot",
    ],
)
def test_generation_slot_and_prompt_task_tamper_fail_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    with sqlite3.connect(store.path) as connection:
        if mutation == "generation_task":
            connection.execute(
                """
                UPDATE generation_tasks SET task='# Forged assignment\n'
                WHERE worker='xhigh' AND generation=1
                """
            )
        elif mutation == "slot_task":
            connection.execute(
                "UPDATE round_slots SET task='# Forged slot task\n' WHERE slot_id=?",
                (root.slot_id,),
            )
        elif mutation == "prompt_task_digest":
            connection.execute(
                "UPDATE round_slots SET prompt_task_sha256=? WHERE slot_id=?",
                ("0" * 64, root.slot_id),
            )
        elif mutation == "prompt_markers":
            forged = "prompt without any durable binding markers"
            connection.execute(
                """
                UPDATE round_slots SET prompt=?, prompt_sha256=?
                WHERE slot_id=?
                """,
                (
                    forged,
                    hashlib.sha256(forged.encode()).hexdigest(),
                    root.slot_id,
                ),
            )
        else:
            connection.execute(
                "UPDATE round_slots SET legacy_task_binding=1 WHERE slot_id=?",
                (root.slot_id,),
            )

    with pytest.raises(CoordinationError, match="task|prompt|legacy"):
        CoordinationStore(project, metadata, create=False)


def test_concurrent_admission_selects_one_root_and_one_critic(tmp_path: Path) -> None:
    project, metadata = _project(tmp_path)
    store = CoordinationStore(project, metadata)
    workers = list(metadata["workers"])

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        results = list(executor.map(lambda worker: store.admit(worker), workers))

    admitted = [result for result in results if result is not None]
    assert {(result.worker, result.lane) for result in admitted} == {
        ("xhigh", "root"),
        ("xhigh2", "critic"),
    }
    assert len({result.slot_id for result in admitted}) == 2
    critic = next(result for result in admitted if result.lane == "critic")
    assert "Independent-first" in critic.directive
    assert "problem" not in critic.directive.lower()
    status = store.project_status()
    assert status["reserved_admission"] == 2
    assert status["paid_active"] == 0
    assert status["waiting_admission"] == len(workers) - 2

    for result in admitted:
        pinned = store.pin_prompt(
            result.slot_id,
            _bound_prompt(result, f"kickoff {result.lane}"),
        )
        active = store.activate(pinned.slot_id)
        assert active.prompt == _bound_prompt(result, f"kickoff {result.lane}")
    assert store.project_status()["paid_active"] == 2


def test_crash_reopen_preserves_slot_prompt_and_ambiguous_identity(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    first_store = CoordinationStore(project, metadata)
    first = first_store.admit("xhigh")
    assert first is not None and first.prompt is None
    pinned = first_store.pin_prompt(
        first.slot_id,
        _bound_prompt(first, "original pinned kickoff"),
    )
    first_store.activate(pinned.slot_id)
    first_store.mark_ambiguous(pinned.slot_id)

    reopened = CoordinationStore(project, metadata, create=False)
    resumed = reopened.admit("xhigh")
    assert resumed is not None
    assert resumed.resumed is True
    assert resumed.slot_id == first.slot_id
    assert resumed.prompt == _bound_prompt(first, "original pinned kickoff")
    immutable = reopened.pin_prompt(
        resumed.slot_id,
        _bound_prompt(resumed, "changed kickoff"),
    )
    assert immutable.prompt == _bound_prompt(first, "original pinned kickoff")
    assert immutable.prompt_sha256 == pinned.prompt_sha256


def test_generation_advances_only_after_both_lanes_terminal(tmp_path: Path) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    critic = store.admit("xhigh2")
    assert root is not None and critic is not None
    for admission in (root, critic):
        store.pin_prompt(admission.slot_id, _bound_prompt(admission))
        store.activate(admission.slot_id)

    after_root = store.complete(root.slot_id, outcome="terminal_rc_0")
    assert after_root["generation"] == 1
    assert store.admit("xhigh") is None
    after_critic = store.complete(critic.slot_id, outcome="terminal_rc_0")
    assert after_critic["generation"] == 2
    carried = store.staged_task_assignments()
    assert carried["generation"] == 2
    assert carried["ready"] is True
    assert {item["task_sha256"] for item in carried["assignments"]} == {
        root.task_sha256,
        critic.task_sha256,
    }
    assert all(item["frozen"] is False for item in carried["assignments"])
    next_root = store.admit("xhigh")
    assert next_root is not None and next_root.generation == 2
    assert next_root.task == root.task
    assert next_root.task_sha256 == root.task_sha256
    before_replay = store.project_status("xhigh")
    replay = store.complete(root.slot_id, outcome="terminal_rc_0")
    assert replay["generation"] == 2
    assert store.project_status("xhigh") == before_replay
    with pytest.raises(CoordinationError, match="terminal outcome conflicts"):
        store.complete(root.slot_id, outcome="terminal_rc_126")


def test_recommendation_requires_exact_same_generation_root_critic_pair(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    root_slot = store.admit("xhigh")
    critic_slot = store.admit("xhigh2")
    assert root_slot is not None and critic_slot is not None
    for admission in (root_slot, critic_slot):
        store.pin_prompt(admission.slot_id, _bound_prompt(admission))
        store.activate(admission.slot_id)
    root = store.record_root_evidence(
        "xhigh",
        "dead_end",
        entry_id="root_dead_end",
        slot_id=root_slot.slot_id,
    )
    assert store.project_status()["recommendation"] is None

    with pytest.raises(CoordinationError, match="exact designated review slot"):
        store.confirm_root_evidence(
            "xhigh2",
            "missing_entry",
            entry_id="bad_confirmation",
            slot_id=critic_slot.slot_id,
        )
    with pytest.raises(CoordinationError, match="only root"):
        store.record_root_evidence(
            "xhigh",
            "timer",
            entry_id="timer_entry",
            slot_id=root_slot.slot_id,
        )

    with pytest.raises(CoordinationError, match="different obstacle review"):
        store.record_root_evidence(
            "xhigh",
            "obstacle",
            entry_id="different_root_obstacle",
            slot_id=root_slot.slot_id,
        )
    assert store.evidence_entry("different_root_obstacle") is None

    store.complete(critic_slot.slot_id, outcome="terminal_rc_0")
    review_status = store.complete(root_slot.slot_id, outcome="terminal_rc_0")
    assert review_status["generation"] == 1
    assert review_status["phase"] == "critic_obstacle_review"
    assert review_status["review"]["root_entry_id"] == root["entry_id"]
    assert store.admit("xhigh") is None
    review_slot = store.admit("xhigh2")
    assert review_slot is not None
    assert review_slot.slot_id != critic_slot.slot_id
    assert review_slot.review_id == root["review_id"]
    assert review_slot.designated_root_entry_id == root["entry_id"]
    assert root["entry_id"] in review_slot.directive
    assert f"gm_get(entry_id={root['entry_id']})" in review_slot.directive
    assert "never substitute gm_search/BM25" in review_slot.directive
    store.pin_prompt(review_slot.slot_id, _bound_prompt(review_slot))
    store.activate(review_slot.slot_id)

    with pytest.raises(CoordinationError, match="exact designated review slot"):
        store.confirm_root_evidence(
            "xhigh2",
            "missing_entry",
            entry_id="wrong_review_confirmation",
            slot_id=review_slot.slot_id,
        )
    confirmation = store.confirm_root_evidence(
        "xhigh2",
        root["entry_id"],
        entry_id="critic_confirmation",
        slot_id=review_slot.slot_id,
    )
    assert confirmation["state"] == "owner_action_required"
    assert confirmation["browser_dispatch_authorized"] is False
    assert confirmation["advisor_request_id"] is None
    recommendation = store.project_status()["recommendation"]
    assert recommendation is not None
    assert recommendation["root_entry_id"] == "root_dead_end"
    assert recommendation["critic_entry_id"] == "critic_confirmation"
    assert recommendation["browser_dispatch_authorized"] is False
    assert recommendation["advisor_request_id"] is None
    # The confirmed review remains paid-owned until its exact terminal receipt.
    resumed = store.admit("xhigh2")
    assert resumed is not None and resumed.slot_id == review_slot.slot_id
    store.complete(review_slot.slot_id, outcome="terminal_rc_0")
    assert store.admit("xhigh") is None


def _memory_entry(
    admission,
    *,
    entry_id: str,
    author: str,
    kind: str = "dead_end",
    confirms_entry_id: str | None = None,
) -> dict[str, object]:
    links: dict[str, object] = {
        "coordination": {
            "slot_id": admission.slot_id,
            "generation": admission.generation,
            "lane": admission.lane,
        }
    }
    if confirms_entry_id is not None:
        links["confirms_entry_id"] = confirms_entry_id
    return {
        "id": entry_id,
        "author": author,
        "kind": kind,
        "links": links,
        "claim": "ignored mathematical content",
    }


def test_terminal_reconciliation_rejects_generic_pre_name_then_reviews_exact_slot(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    critic = store.admit("xhigh2")
    assert root is not None and critic is not None
    for admission in (root, critic):
        store.pin_prompt(admission.slot_id, _bound_prompt(admission))
        store.activate(admission.slot_id)

    critic_entry = _memory_entry(
        critic,
        entry_id="critic_gm_entry",
        author="xhigh2",
        confirms_entry_id="root_gm_entry",
    )
    with pytest.raises(CoordinationError, match="generic critic confirmation"):
        store.reconcile_terminal_memory_entries(
            critic.slot_id,
            "xhigh2",
            [critic_entry],
        )
    assert store.evidence_entry("critic_gm_entry") is None
    store.complete(critic.slot_id, outcome="terminal_rc_0")

    root_entry = _memory_entry(
        root,
        entry_id="root_gm_entry",
        author="xhigh",
    )
    second = store.reconcile_terminal_memory_entries(
        root.slot_id,
        "xhigh",
        [root_entry],
    )
    assert second["accepted_entry_ids"] == ["root_gm_entry"]
    assert second["review_id"] is not None
    assert second["recommendation_id"] is None
    status = store.complete(root.slot_id, outcome="terminal_rc_0")
    assert status["phase"] == "critic_obstacle_review"

    review = store.admit("xhigh2")
    assert review is not None
    store.pin_prompt(review.slot_id, _bound_prompt(review))
    store.activate(review.slot_id)
    designated = _memory_entry(
        review,
        entry_id="designated_critic_gm_entry",
        author="xhigh2",
        confirms_entry_id="root_gm_entry",
    )
    third = store.reconcile_terminal_memory_entries(
        review.slot_id,
        "xhigh2",
        [designated],
    )
    assert third["accepted_entry_ids"] == ["designated_critic_gm_entry"]
    assert third["recommendation_id"] is not None
    recommendation = store.project_status()["recommendation"]
    assert recommendation is not None
    assert recommendation["root_entry_id"] == "root_gm_entry"
    assert recommendation["critic_entry_id"] == "designated_critic_gm_entry"
    assert recommendation["browser_dispatch_authorized"] is False
    assert recommendation["advisor_request_id"] is None


def test_terminal_reconciliation_ignores_self_reported_or_wrong_slot_provenance(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    correct = _memory_entry(
        root,
        entry_id="exact_entry",
        author="xhigh",
        kind="obstacle",
    )
    wrong_generation = _memory_entry(
        root,
        entry_id="wrong_generation",
        author="xhigh",
    )
    wrong_generation["links"]["coordination"]["generation"] = 99
    boolean_generation = _memory_entry(
        root,
        entry_id="boolean_generation",
        author="xhigh",
    )
    boolean_generation["links"]["coordination"]["generation"] = True
    wrong_slot = _memory_entry(
        root,
        entry_id="wrong_slot",
        author="xhigh",
    )
    wrong_slot["links"]["coordination"]["slot_id"] = "slot_self_reported"
    wrong_author = _memory_entry(
        root,
        entry_id="wrong_author",
        author="xhigh2",
    )

    result = store.reconcile_terminal_memory_entries(
        root.slot_id,
        "xhigh",
        [wrong_generation, boolean_generation, wrong_slot, wrong_author, correct],
    )
    assert result["accepted_entry_ids"] == ["exact_entry"]
    assert store.evidence_entry("wrong_generation") is None
    evidence = store.evidence_entry("exact_entry")
    assert evidence is not None
    assert evidence["slot_id"] == root.slot_id
    assert evidence["generation"] == 1


def _create_review_recommendation(
    store: CoordinationStore,
    *,
    complete_review: bool,
) -> tuple[object, object, object, dict[str, object]]:
    root = store.admit("xhigh")
    critic = store.admit("xhigh2")
    assert root is not None and critic is not None
    for admission in (root, critic):
        store.pin_prompt(admission.slot_id, _bound_prompt(admission))
        store.activate(admission.slot_id)
    evidence = store.record_root_evidence(
        "xhigh",
        "obstacle",
        entry_id="root_review_obstacle",
        slot_id=root.slot_id,
    )
    store.complete(root.slot_id, outcome="terminal_rc_0")
    status = store.complete(critic.slot_id, outcome="terminal_rc_0")
    assert status["phase"] == CRITIC_REVIEW_PHASE
    review = store.admit("xhigh2")
    assert review is not None
    store.pin_prompt(review.slot_id, _bound_prompt(review))
    store.activate(review.slot_id)
    confirmation = store.confirm_root_evidence(
        "xhigh2",
        str(evidence["entry_id"]),
        entry_id="designated_review_confirmation",
        slot_id=review.slot_id,
    )
    if complete_review:
        store.complete(review.slot_id, outcome="terminal_rc_0")
    return root, critic, review, confirmation


def test_unconfirmed_review_terminal_advances_to_fresh_generation(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    critic = store.admit("xhigh2")
    assert root is not None and critic is not None
    for admission in (root, critic):
        store.pin_prompt(admission.slot_id, _bound_prompt(admission))
        store.activate(admission.slot_id)
    evidence = store.record_root_evidence(
        "xhigh",
        "dead_end",
        entry_id="unconfirmed_root",
        slot_id=root.slot_id,
    )
    store.complete(critic.slot_id, outcome="terminal_rc_0")
    store.complete(root.slot_id, outcome="terminal_rc_0")
    review = store.admit("xhigh2")
    assert review is not None
    assert review.designated_root_entry_id == evidence["entry_id"]
    store.pin_prompt(review.slot_id, _bound_prompt(review))
    store.activate(review.slot_id)

    status = store.complete(review.slot_id, outcome="terminal_rc_0")
    assert status["generation"] == 2
    assert status["phase"] == "root_critic_reasoning"
    assert status["review"] is None
    assert status["recommendation"] is None
    with sqlite3.connect(store.path) as connection:
        review_state = connection.execute(
            "SELECT state FROM obstacle_reviews WHERE review_id=?",
            (review.review_id,),
        ).fetchone()[0]
    assert review_state == "not_confirmed"
    replay = store.complete(review.slot_id, outcome="terminal_rc_0")
    assert replay["generation"] == 2
    with pytest.raises(CoordinationError, match="terminal outcome conflicts"):
        store.complete(review.slot_id, outcome="terminal_rc_1")


def test_owner_resolution_is_terminal_exact_cas_and_replay_idempotent(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    _root, _critic, review, confirmation = _create_review_recommendation(
        store,
        complete_review=False,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    before = store.project_status()
    assert before["recommendation"]["browser_dispatch_authorized"] is False
    assert before["recommendation"]["advisor_request_id"] is None
    assert before["advisor_recommendation_present"] is True
    assert before["advisor_recommendation_ready"] is False
    with pytest.raises(CoordinationError, match="nonterminal paid slots"):
        store.validate_open_recommendation(recommendation_id)
    with pytest.raises(CoordinationError, match="must be terminal"):
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
    store.complete(review.slot_id, outcome="terminal_rc_0")
    ready_status = store.project_status()
    assert ready_status["advisor_recommendation_present"] is True
    assert ready_status["advisor_recommendation_ready"] is True
    open_recommendation = store.validate_open_recommendation(recommendation_id)
    assert open_recommendation == {
        "recommendation_id": recommendation_id,
        "generation": 1,
        "state": "owner_action_required",
        "review_id": review.review_id,
        "root_entry_id": "root_review_obstacle",
        "critic_entry_id": "designated_review_confirmation",
        "browser_dispatch_authorized": False,
        "advisor_request_id": None,
        "ready": True,
    }
    with pytest.raises(CoordinationError, match="does not exist"):
        store.validate_open_recommendation("recommendation_unknown")
    with pytest.raises(CoordinationError, match="exactly equal"):
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement="different_recommendation",
        )

    _stage_next_generation(store)
    resolved = store.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    status = store.project_status()
    assert resolved["recommendation_id"] == recommendation_id
    assert resolved["resolution"] == "continue_without_advisor"
    assert status["generation"] == 2
    assert status["phase"] == "root_critic_reasoning"
    assert status["recommendation"] is None
    assert status["resolution"] == resolved
    assert status["review"] is None
    with pytest.raises(CoordinationError, match="already resolved"):
        store.validate_open_recommendation(recommendation_id)
    assert (
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
        == resolved
    )
    with pytest.raises(CoordinationError, match="conflicting owner resolution"):
        store.resolve_recommendation(
            recommendation_id,
            resolution="adopted_master_guidance",
            owner_acknowledgement=recommendation_id,
            master_guidance_entry_id="a" * 16,
            master_guidance_record_sha256="b" * 64,
        )


def test_concurrent_exact_owner_resolution_has_one_durable_result(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    _root, _critic, _review, confirmation = _create_review_recommendation(
        store,
        complete_review=True,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    _stage_next_generation(store)
    barrier = threading.Barrier(2)

    def resolve() -> dict[str, object]:
        barrier.wait()
        return store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: resolve(), range(2)))
    assert results[0] == results[1]
    with sqlite3.connect(store.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM recommendation_resolutions"
        ).fetchone()[0]
    assert count == 1
    assert store.project_status()["generation"] == 2


def test_owner_resolution_requires_complete_next_generation_tasks_and_freezes_exactly(
    tmp_path: Path,
) -> None:
    project, metadata = _project(
        tmp_path,
        roles="xhigh:2",
        workers=["xhigh", "xhigh2"],
    )
    store = CoordinationStore(project, metadata)
    _root, _critic, _review, confirmation = _create_review_recommendation(
        store,
        complete_review=True,
    )
    recommendation_id = str(confirmation["recommendation_id"])

    assert store.staged_task_assignments()["missing_workers"] == [
        "xhigh",
        "xhigh2",
    ]
    with pytest.raises(CoordinationError, match="incomplete: xhigh, xhigh2"):
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
    root_task = "# Next root task\n"
    critic_task = "# Next critic task\n"
    store.stage_task_assignment("xhigh", root_task)
    with pytest.raises(CoordinationError, match="incomplete: xhigh2"):
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
    assert store.staged_task_assignments()["assignments"][0]["frozen"] is False
    store.stage_task_assignment("xhigh2", critic_task)
    resolved = store.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    assert resolved["generation"] == 1
    coverage = store.staged_task_assignments()
    assert coverage["generation"] == 2
    assert coverage["ready"] is True
    assert len(coverage["assignments"]) == 2
    assert all(item["frozen"] is True for item in coverage["assignments"])
    with sqlite3.connect(store.path) as connection:
        count, frozen_count = connection.execute(
            """
            SELECT COUNT(*), SUM(frozen_at IS NOT NULL)
            FROM generation_tasks WHERE generation=2
            """
        ).fetchone()
    assert (count, frozen_count) == (2, 2)
    with pytest.raises(CoordinationError, match="frozen for this generation"):
        store.stage_task_assignment("xhigh", "# Post-resolve mutation\n")
    assert store.stage_task_assignment("xhigh", root_task)["replayed"] is True
    assert (
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
        == resolved
    )


def test_stage_last_task_vs_owner_resolution_serializes_and_retry_is_exact(
    tmp_path: Path,
) -> None:
    project, metadata = _project(
        tmp_path,
        roles="xhigh:2",
        workers=["xhigh", "xhigh2"],
    )
    store = CoordinationStore(project, metadata)
    _root, _critic, _review, confirmation = _create_review_recommendation(
        store,
        complete_review=True,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    store.stage_task_assignment("xhigh", "# Concurrent next root\n")
    barrier = threading.Barrier(2)

    def stage_last() -> str:
        barrier.wait()
        store.stage_task_assignment("xhigh2", "# Concurrent next critic\n")
        return "staged"

    def resolve() -> str:
        barrier.wait()
        try:
            store.resolve_recommendation(
                recommendation_id,
                resolution="continue_without_advisor",
                owner_acknowledgement=recommendation_id,
            )
        except CoordinationError as exc:
            assert "task staging is incomplete" in str(exc)
            return "retry_required"
        return "resolved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        staged_future = executor.submit(stage_last)
        resolve_future = executor.submit(resolve)
        outcomes = {staged_future.result(), resolve_future.result()}
    assert "staged" in outcomes
    resolved = store.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    assert resolved["recommendation_id"] == recommendation_id
    assert store.project_status()["generation"] == 2


def test_owner_resolution_rejects_live_candidate_overlay(tmp_path: Path) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    _root, _critic, review, confirmation = _create_review_recommendation(
        store,
        complete_review=False,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    receipt = candidate_receipt_id(
        slot_id=review.slot_id,
        candidate_fact_id="a" * 16,
        candidate_fact_identity="b" * 64,
        source_id=None,
        context_digest="c" * 64,
    )
    store.register_candidate(
        "xhigh2",
        receipt,
        slot_id=review.slot_id,
        candidate_fact_id="a" * 16,
        candidate_fact_identity="b" * 64,
        source_id=None,
        context_digest="c" * 64,
    )
    store.complete(review.slot_id, outcome="terminal_rc_0")
    with pytest.raises(CoordinationError, match="active candidate overlay"):
        store.validate_open_recommendation(recommendation_id)
    with pytest.raises(CoordinationError, match="candidate overlay"):
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
    store.terminalize_candidate(
        "xhigh2",
        receipt,
        slot_id=review.slot_id,
        outcome="wrong",
    )
    assert store.validate_open_recommendation(recommendation_id)["ready"] is True
    _stage_next_generation(store)
    resolved = store.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    assert resolved["recommendation_id"] == recommendation_id
    assert store.project_status()["generation"] == 2


def test_prepared_slot_deadline_is_known_unspent_and_replay_safe(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    deadline = store.project_status()["phase_deadline_at"]
    root = store.admit("xhigh", now=deadline - 1)
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))

    with pytest.raises(CoordinationError, match="deadline exceeded"):
        store.activate(root.slot_id, now=deadline)
    status = store.project_status()
    assert status["generation"] == 2
    with sqlite3.connect(store.path) as connection:
        state, outcome = connection.execute(
            "SELECT state, outcome FROM round_slots WHERE slot_id=?",
            (root.slot_id,),
        ).fetchone()
    assert (state, outcome) == ("terminal", PREPARED_DEADLINE_OUTCOME)
    replay = store.complete(root.slot_id, outcome=PREPARED_DEADLINE_OUTCOME)
    assert replay["generation"] == 2
    with pytest.raises(CoordinationError, match="terminal outcome conflicts"):
        store.complete(root.slot_id, outcome="terminal_rc_0")


def test_expiry_vs_activate_serializes_without_ambiguous_slot(tmp_path: Path) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    deadline = store.project_status()["phase_deadline_at"]
    root = store.admit("xhigh", now=deadline - 2)
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    barrier = threading.Barrier(2)

    def activate() -> str:
        barrier.wait()
        try:
            return store.activate(root.slot_id, now=deadline - 1).state
        except CoordinationError:
            return "terminal"

    def expire() -> str:
        barrier.wait()
        admission = store.admit("xhigh", now=deadline + 1)
        return "none" if admission is None else admission.state

    with ThreadPoolExecutor(max_workers=2) as executor:
        active_result = executor.submit(activate)
        expiry_result = executor.submit(expire)
        outcomes = {active_result.result(), expiry_result.result()}
    with sqlite3.connect(store.path) as connection:
        state = connection.execute(
            "SELECT state FROM round_slots WHERE slot_id=?", (root.slot_id,)
        ).fetchone()[0]
    assert state in {"active", "terminal"}
    assert state != "ambiguous"
    assert outcomes <= {"active", "terminal", "none"}


def test_expired_prepared_baseline_completes_review_phase_decision(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    deadline = store.project_status()["phase_deadline_at"]
    root = store.admit("xhigh", now=deadline - 3)
    critic = store.admit("xhigh2", now=deadline - 3)
    assert root is not None and critic is not None
    for admission in (root, critic):
        store.pin_prompt(admission.slot_id, _bound_prompt(admission))
    store.activate(root.slot_id, now=deadline - 2)
    evidence = store.record_root_evidence(
        "xhigh",
        "obstacle",
        entry_id="deadline_root_obstacle",
        slot_id=root.slot_id,
        now=deadline - 1,
    )
    store.complete(root.slot_id, outcome="terminal_rc_0", now=deadline - 1)

    assert store.admit("xhigh2", now=deadline) is None
    status = store.project_status()
    assert status["generation"] == 1
    assert status["phase"] == CRITIC_REVIEW_PHASE
    assert status["review"]["root_entry_id"] == evidence["entry_id"]
    with sqlite3.connect(store.path) as connection:
        outcome = connection.execute(
            "SELECT outcome FROM round_slots WHERE slot_id=?", (critic.slot_id,)
        ).fetchone()[0]
    assert outcome == PREPARED_DEADLINE_OUTCOME


def test_phase_expiry_blocks_new_paid_slot_and_exposes_fail_stop(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE project_state SET phase_deadline_at=? WHERE singleton=1",
            (0.0,),
        )
    assert store.admit("xhigh") is None
    status = store.project_status()
    assert status["phase_deadline_exceeded"] is True
    assert status["advisor_reachable"] is True
    assert status["advisor_recommendation_present"] is False
    assert status["advisor_recommendation_ready"] is False
    assert status["fail_stop_reason"] == "phase_deadline_exceeded_no_new_paid_admission"
    assert status["paid_active"] == 0


def test_no_critic_root_obstacle_does_not_create_unreachable_review(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    evidence = store.record_root_evidence(
        "xhigh",
        "obstacle",
        entry_id="single_lane_obstacle",
        slot_id=root.slot_id,
    )
    assert evidence["review_id"] is None
    status = store.complete(root.slot_id, outcome="terminal_rc_0")
    assert status["generation"] == 2
    assert status["review"] is None
    assert status["advisor_reachable"] is False
    assert status["advisor_recommendation_present"] is False
    assert status["advisor_recommendation_ready"] is False


def test_expired_owner_action_keeps_structural_advisor_resolution_reachable(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    _root, _critic, _review, confirmation = _create_review_recommendation(
        store,
        complete_review=True,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE project_state SET phase_deadline_at=0 WHERE singleton=1"
        )
    status = store.project_status()
    assert status["phase"] == "owner_action_required"
    assert status["phase_deadline_exceeded"] is True
    assert status["advisor_reachable"] is True
    assert status["advisor_recommendation_present"] is True
    assert status["advisor_recommendation_ready"] is True
    assert status["fail_stop_reason"] == "durable_task_assignment_required"
    _stage_next_generation(store)
    assert store.project_status()["fail_stop_reason"] == (
        "owner_recommendation_resolution_required"
    )
    resolved = store.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    assert resolved["recommendation_id"] == recommendation_id
    assert store.project_status()["generation"] == 2


def test_content_free_memory_cursor_and_candidate_api_stubs(tmp_path: Path) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    cursor = store.set_memory_cursor("xhigh", "global_memory", "cursor_17")
    candidate = store.record_candidate("xhigh", "candidate_1", state="promising")

    assert cursor == {
        "worker": "xhigh",
        "stream": "global_memory",
        "cursor": "cursor_17",
        "generation": 1,
    }
    assert candidate["lane"] == "root"
    assert store.list_candidates() == [candidate]
    with pytest.raises(CoordinationError, match="bounded identifier"):
        store.record_candidate("xhigh", "candidate with mathematical prose")


def test_candidate_overlay_preserves_open_lanes_and_freezes_advance_until_terminal(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    critic = store.admit("xhigh2")
    assert root is not None and critic is not None
    for admission in (root, critic):
        store.pin_prompt(admission.slot_id, _bound_prompt(admission))
        store.activate(admission.slot_id)
    receipt = candidate_receipt_id(
        slot_id=root.slot_id,
        candidate_fact_id="a" * 16,
        candidate_fact_identity="c" * 64,
        source_id=None,
        context_digest="b" * 64,
    )
    candidate = store.register_candidate(
        "xhigh",
        receipt,
        slot_id=root.slot_id,
        candidate_fact_id="a" * 16,
        candidate_fact_identity="c" * 64,
        source_id=None,
        context_digest="b" * 64,
    )
    assert candidate["state"] == "active" and candidate["source_id"] is None
    assert store.project_status()["candidate"] == candidate
    assert store.admit("xhigh2").slot_id == critic.slot_id

    store.complete(root.slot_id, outcome="terminal_rc_0")
    store.complete(critic.slot_id, outcome="terminal_rc_0")
    assert store.project_status()["generation"] == 1
    assert store.admit("xhigh") is None
    assert (
        store.register_candidate(
            "xhigh",
            receipt,
            slot_id=root.slot_id,
            candidate_fact_id="a" * 16,
            candidate_fact_identity="c" * 64,
            source_id=None,
            context_digest="b" * 64,
        )["state"]
        == "active"
    )

    terminal = store.terminalize_candidate(
        "xhigh",
        receipt,
        slot_id=root.slot_id,
        outcome="correct",
    )
    assert terminal["state"] == "terminal" and terminal["outcome"] == "correct"
    assert store.project_status()["candidate"] is None
    assert store.project_status()["generation"] == 2
    assert store.admit("xhigh") is not None


def test_candidate_outcome_unknown_survives_reopen_without_ttl_release(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    receipt = candidate_receipt_id(
        slot_id=root.slot_id,
        candidate_fact_id="1" * 16,
        candidate_fact_identity="4" * 64,
        source_id="2" * 16,
        context_digest="3" * 64,
    )
    with pytest.raises(CoordinationError, match="does not bind"):
        store.register_candidate(
            "xhigh",
            "0" * 64,
            slot_id=root.slot_id,
            candidate_fact_id="1" * 16,
            candidate_fact_identity="4" * 64,
            source_id="2" * 16,
            context_digest="3" * 64,
        )
    store.register_candidate(
        "xhigh",
        receipt,
        slot_id=root.slot_id,
        candidate_fact_id="1" * 16,
        candidate_fact_identity="4" * 64,
        source_id="2" * 16,
        context_digest="3" * 64,
    )
    store.terminalize_candidate(
        "xhigh",
        receipt,
        slot_id=root.slot_id,
        outcome="outcome_unknown",
    )
    with pytest.raises(CoordinationError, match="source slot is not terminal"):
        store.resolve_candidate_outcome_unknown(
            receipt,
            resolution="known_no_promotion",
            acknowledge_paid_outcome_unknown=True,
            candidate_fact_active=False,
        )
    store.mark_ambiguous(root.slot_id)
    with pytest.raises(CoordinationError, match="source slot is not terminal"):
        store.resolve_candidate_outcome_unknown(
            receipt,
            resolution="known_no_promotion",
            acknowledge_paid_outcome_unknown=True,
            candidate_fact_active=False,
        )
    store.complete(root.slot_id, outcome="terminal_rc_126")

    reopened = CoordinationStore(project, metadata, create=False)
    candidate = reopened.project_status()["candidate"]
    assert candidate is not None and candidate["state"] == "outcome_unknown"
    assert reopened.admit("xhigh", now=10**12) is None
    with pytest.raises(CoordinationError, match="explicit owner resolution"):
        reopened.terminalize_candidate(
            "xhigh",
            receipt,
            slot_id=root.slot_id,
            outcome="error",
            now=10**12,
        )
    with pytest.raises(CoordinationError, match="active candidate fact"):
        reopened.resolve_candidate_outcome_unknown(
            receipt,
            resolution="known_no_promotion",
            acknowledge_paid_outcome_unknown=True,
            candidate_fact_active=True,
        )
    resolved = reopened.resolve_candidate_outcome_unknown(
        receipt,
        resolution="known_no_promotion",
        acknowledge_paid_outcome_unknown=True,
        candidate_fact_active=False,
    )
    assert resolved["state"] == "terminal"
    assert resolved["outcome"] == "outcome_unknown"
    assert resolved["owner_resolution"] == "known_no_promotion"
    assert resolved["owner_acknowledged_unknown"] is True
    assert resolved["candidate_fact_active_at_resolution"] is False
    assert reopened.project_status()["candidate"] is None
    assert reopened.project_status()["generation"] == 2
    assert (
        reopened.register_candidate(
            "xhigh",
            receipt,
            slot_id=root.slot_id,
            candidate_fact_id="1" * 16,
            candidate_fact_identity="4" * 64,
            source_id="2" * 16,
            context_digest="3" * 64,
        )
        == resolved
    )


def test_owner_can_acknowledge_and_abandon_active_candidate_after_process_crash(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    receipt = candidate_receipt_id(
        slot_id=root.slot_id,
        candidate_fact_id="4" * 16,
        candidate_fact_identity="6" * 64,
        source_id=None,
        context_digest="5" * 64,
    )
    store.register_candidate(
        "xhigh",
        receipt,
        slot_id=root.slot_id,
        candidate_fact_id="4" * 16,
        candidate_fact_identity="6" * 64,
        source_id=None,
        context_digest="5" * 64,
    )
    with pytest.raises(CoordinationError, match="must acknowledge"):
        store.resolve_candidate_outcome_unknown(
            receipt,
            resolution="abandon_unknown",
            acknowledge_paid_outcome_unknown=False,
            candidate_fact_active=True,
        )
    with pytest.raises(CoordinationError, match="source slot is not terminal"):
        store.resolve_candidate_outcome_unknown(
            receipt,
            resolution="abandon_unknown",
            acknowledge_paid_outcome_unknown=True,
            candidate_fact_active=True,
        )
    store.complete(root.slot_id, outcome="terminal_rc_126")
    resolved = CoordinationStore(
        project, metadata, create=False
    ).resolve_candidate_outcome_unknown(
        receipt,
        resolution="abandon_unknown",
        acknowledge_paid_outcome_unknown=True,
        candidate_fact_active=True,
    )
    assert resolved["state"] == "terminal"
    assert resolved["owner_resolution"] == "abandon_unknown"
    assert resolved["candidate_fact_active_at_resolution"] is True


def _remove_v7_task_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE generation_tasks")
    for column in (
        "task",
        "task_sha256",
        "task_bytes",
        "prompt_task_sha256",
        "legacy_task_binding",
    ):
        connection.execute(f"ALTER TABLE round_slots DROP COLUMN {column}")


def test_schema_v6_owner_gate_migrates_legacy_terminal_slots_but_no_future_tasks(
    tmp_path: Path,
) -> None:
    project, metadata = _project(
        tmp_path,
        roles="xhigh:2",
        workers=["xhigh", "xhigh2"],
    )
    store = CoordinationStore(project, metadata)
    _root, _critic, _review, confirmation = _create_review_recommendation(
        store,
        complete_review=True,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    with sqlite3.connect(store.path) as connection:
        _remove_v7_task_schema(connection)
        connection.execute(
            "UPDATE project_state SET schema_version=6 WHERE singleton=1"
        )

    migrated = CoordinationStore(project, metadata, create=False)
    status = migrated.project_status()
    assert status["generation"] == 1
    assert status["phase"] == "owner_action_required"
    assert status["fail_stop_reason"] == "durable_task_assignment_required"
    assert status["task_staging"]["missing_workers"] == ["xhigh", "xhigh2"]
    with sqlite3.connect(migrated.path) as connection:
        version = connection.execute(
            "SELECT schema_version FROM project_state WHERE singleton=1"
        ).fetchone()[0]
        legacy_slots = connection.execute(
            """
            SELECT COUNT(*), SUM(legacy_task_binding)
            FROM round_slots WHERE state='terminal'
            """
        ).fetchone()
    assert version == 7
    assert legacy_slots[0] == legacy_slots[1]

    with pytest.raises(CoordinationError, match="incomplete: xhigh, xhigh2"):
        migrated.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
    migrated.stage_task_assignment("xhigh", "# Migrated next root\n")
    with pytest.raises(CoordinationError, match="incomplete: xhigh2"):
        migrated.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
    migrated.stage_task_assignment("xhigh2", "# Migrated next critic\n")
    resolved = migrated.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    reopened = CoordinationStore(project, metadata, create=False)
    assert reopened.project_status()["generation"] == 2
    assert (
        reopened.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
        == resolved
    )


def test_schema_v6_nonterminal_slot_task_identity_fails_atomically(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None and root.state == "prepared"
    with sqlite3.connect(store.path) as connection:
        _remove_v7_task_schema(connection)
        connection.execute(
            "UPDATE project_state SET schema_version=6 WHERE singleton=1"
        )

    with pytest.raises(CoordinationError, match="nonterminal paid slot"):
        CoordinationStore(project, metadata, create=False)
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM project_state WHERE singleton=1"
            ).fetchone()[0]
            == 6
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='generation_tasks'"
            ).fetchone()
            is None
        )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(round_slots)")
        }
    assert "task_sha256" not in columns
    assert "legacy_task_binding" not in columns


def test_schema_v6_partial_terminal_generation_fails_atomically_on_reopen(
    tmp_path: Path,
) -> None:
    project, metadata = _project(
        tmp_path,
        roles="xhigh:2",
        workers=["xhigh", "xhigh2"],
    )
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    status = store.complete(root.slot_id, outcome="terminal_rc_0")
    assert status["generation"] == 1
    assert status["phase"] == "root_critic_reasoning"
    assert store.admit("xhigh") is None

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            """
            SELECT lane, state FROM round_slots
            WHERE generation=1 ORDER BY lane
            """
        ).fetchall() == [("root", "terminal")]
        _remove_v7_task_schema(connection)
        connection.execute(
            "UPDATE project_state SET schema_version=6 WHERE singleton=1"
        )

    for _attempt in range(2):
        with pytest.raises(
            CoordinationError,
            match="current-generation paid slot history.*cannot be safely migrated",
        ):
            CoordinationStore(project, metadata, create=False)
        with sqlite3.connect(store.path) as connection:
            assert (
                connection.execute(
                    "SELECT schema_version FROM project_state WHERE singleton=1"
                ).fetchone()[0]
                == 6
            )
            assert connection.execute(
                """
                SELECT lane, state FROM round_slots
                WHERE generation=1 ORDER BY lane
                """
            ).fetchall() == [("root", "terminal")]
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='generation_tasks'"
                ).fetchone()
                is None
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(round_slots)")
            }
        assert "task_sha256" not in columns
        assert "legacy_task_binding" not in columns


def test_schema_v4_without_overlay_migrates_full_identity_column(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    CoordinationStore(project, metadata)
    database = project / ".coordination" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        _remove_v7_task_schema(connection)
        connection.execute("ALTER TABLE candidates DROP COLUMN candidate_fact_identity")
        connection.execute(
            "UPDATE project_state SET schema_version=4 WHERE singleton=1"
        )

    CoordinationStore(project, metadata, create=False)
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT schema_version FROM project_state WHERE singleton=1"
        ).fetchone()[0]
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(candidates)").fetchall()
        }
    assert version == 7
    assert "candidate_fact_identity" in columns


def test_schema_v4_active_overlay_without_full_identity_fails_closed(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    receipt = candidate_receipt_id(
        slot_id=root.slot_id,
        candidate_fact_id="a" * 16,
        candidate_fact_identity="b" * 64,
        source_id=None,
        context_digest="c" * 64,
    )
    store.register_candidate(
        "xhigh",
        receipt,
        slot_id=root.slot_id,
        candidate_fact_id="a" * 16,
        candidate_fact_identity="b" * 64,
        source_id=None,
        context_digest="c" * 64,
    )
    database = project / ".coordination" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        _remove_v7_task_schema(connection)
        connection.execute("ALTER TABLE candidates DROP COLUMN candidate_fact_identity")
        connection.execute(
            "UPDATE project_state SET schema_version=4 WHERE singleton=1"
        )

    with pytest.raises(CoordinationError, match="cannot be safely migrated"):
        CoordinationStore(project, metadata, create=False)


def test_schema_v5_nonterminal_slot_fails_closed_before_task_migration(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    critic = store.admit("xhigh2")
    assert root is not None and critic is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    evidence = store.record_root_evidence(
        "xhigh",
        "obstacle",
        entry_id="legacy_v5_obstacle",
        slot_id=root.slot_id,
    )
    with sqlite3.connect(store.path) as connection:
        _remove_v7_task_schema(connection)
        connection.execute(
            "UPDATE project_state SET schema_version=5, active_review_id=NULL "
            "WHERE singleton=1"
        )
        connection.execute("DELETE FROM obstacle_reviews")

    with pytest.raises(CoordinationError, match="nonterminal paid slot"):
        CoordinationStore(project, metadata, create=False)
    with sqlite3.connect(store.path) as connection:
        version = connection.execute(
            "SELECT schema_version FROM project_state WHERE singleton=1"
        ).fetchone()[0]
        task_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_tasks'"
        ).fetchone()
    assert evidence["entry_id"] == "legacy_v5_obstacle"
    assert version == 5
    assert task_table is None


def test_schema_v5_multiple_root_obstacles_fail_closed_as_ambiguous(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:2", workers=["xhigh", "xhigh2"])
    store = CoordinationStore(project, metadata)
    root = store.admit("xhigh")
    assert root is not None
    store.pin_prompt(root.slot_id, _bound_prompt(root))
    store.activate(root.slot_id)
    store.record_root_evidence(
        "xhigh",
        "obstacle",
        entry_id="legacy_first_obstacle",
        slot_id=root.slot_id,
    )
    with sqlite3.connect(store.path) as connection:
        _remove_v7_task_schema(connection)
        connection.execute(
            "UPDATE project_state SET schema_version=5, active_review_id=NULL "
            "WHERE singleton=1"
        )
        connection.execute("DELETE FROM obstacle_reviews")
        connection.execute(
            """
            INSERT INTO evidence_entries(
                entry_id, generation, slot_id, worker, lane, kind,
                confirms_entry_id, created_at
            ) VALUES('legacy_second_obstacle', 1, ?, 'xhigh', 'root',
                     'dead_end', NULL, 2.0)
            """,
            (root.slot_id,),
        )

    with pytest.raises(CoordinationError, match="multiple root obstacles"):
        CoordinationStore(project, metadata, create=False)


def test_missing_coordination_field_is_legacy_and_creates_no_store(
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    metadata = {
        "name": "legacy",
        "model": "model",
        "roles": "high:1",
        "workers": ["high"],
    }
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")

    assert coordination_config(metadata).mode == "legacy"
    with pytest.raises(CoordinationError, match="legacy project"):
        CoordinationStore(project, metadata)
    assert not (project / ".coordination").exists()


def test_existing_coordination_store_cannot_be_downgraded_by_metadata_edit(
    tmp_path: Path,
) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    CoordinationStore(project, metadata)
    metadata.pop("coordination")
    (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(CoordinationError, match="cannot downgrade"):
        CoordinationStore.open_existing(project)


def test_disappeared_database_fails_closed_without_recreation(tmp_path: Path) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    store.path.unlink()

    with pytest.raises(CoordinationError, match="database disappeared"):
        store.project_status()
    assert not store.path.exists()


def test_database_symlink_alias_is_rejected_before_sqlite_open(tmp_path: Path) -> None:
    project, metadata = _project(tmp_path, roles="xhigh:1", workers=["xhigh"])
    store = CoordinationStore(project, metadata)
    original = store.directory / "original.sqlite3"
    store.path.rename(original)
    store.path.symlink_to(original.name)

    with pytest.raises(CoordinationError, match="database is unsafe"):
        store.project_status()


def test_owner_resolution_mid_transaction_failure_rolls_back_and_exact_retry_replays(
    tmp_path: Path,
) -> None:
    project, metadata = _project(
        tmp_path,
        roles="xhigh:2",
        workers=["xhigh", "xhigh2"],
    )
    store = CoordinationStore(project, metadata)
    _root, _critic, review, confirmation = _create_review_recommendation(
        store,
        complete_review=True,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    before = store.project_status()
    assert before["advisor_recommendation_ready"] is True
    _stage_next_generation(store)

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_owner_resolution_project_cas
            BEFORE UPDATE OF generation ON project_state
            WHEN NEW.generation = OLD.generation + 1
            BEGIN
                SELECT RAISE(ABORT, 'injected owner-resolution crash cut');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected owner-resolution"):
        store.resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )

    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM recommendation_resolutions"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT state FROM obstacle_reviews WHERE review_id=?",
                (review.review_id,),
            ).fetchone()[0]
            == "confirmed"
        )
        project_row = connection.execute(
            "SELECT generation, phase, recommendation_id, active_review_id "
            "FROM project_state WHERE singleton=1"
        ).fetchone()
        assert project_row == (
            before["generation"],
            before["phase"],
            recommendation_id,
            review.review_id,
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM generation_tasks
                WHERE generation=2 AND frozen_at IS NOT NULL
                """
            ).fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER fail_owner_resolution_project_cas")

    resolved = store.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    assert resolved["resolution"] == "continue_without_advisor"
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM generation_tasks
                WHERE generation=2 AND frozen_at IS NOT NULL
                """
            ).fetchone()[0]
            == 2
        )
    assert (
        CoordinationStore(project, metadata, create=False).resolve_recommendation(
            recommendation_id,
            resolution="continue_without_advisor",
            owner_acknowledgement=recommendation_id,
        )
        == resolved
    )
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM recommendation_resolutions"
            ).fetchone()[0]
            == 1
        )


def _downgrade_ready_recommendation_database_to_real_v5(database: Path) -> None:
    """Rebuild v7-owned tables with the actual pre-review v5 column shape."""

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            DROP TABLE generation_tasks;
            ALTER TABLE project_state RENAME TO project_state_v6;
            CREATE TABLE project_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                mode TEXT NOT NULL,
                config_digest TEXT NOT NULL,
                root_worker TEXT NOT NULL,
                critic_worker TEXT,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                phase TEXT NOT NULL,
                phase_started_at REAL NOT NULL,
                phase_deadline_at REAL NOT NULL,
                recommendation_id TEXT,
                updated_at REAL NOT NULL
            );
            INSERT INTO project_state
            SELECT singleton, 5, mode, config_digest, root_worker, critic_worker,
                   generation, phase, phase_started_at, phase_deadline_at,
                   recommendation_id, updated_at
            FROM project_state_v6;
            DROP TABLE project_state_v6;

            ALTER TABLE round_slots RENAME TO round_slots_v6;
            CREATE TABLE round_slots (
                slot_id TEXT PRIMARY KEY,
                worker TEXT NOT NULL,
                lane TEXT NOT NULL CHECK (lane IN ('root', 'critic')),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                phase TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('prepared', 'active', 'ambiguous', 'terminal')
                ),
                directive TEXT NOT NULL,
                prompt TEXT,
                prompt_sha256 TEXT,
                created_at REAL NOT NULL,
                activated_at REAL,
                terminal_at REAL,
                outcome TEXT,
                UNIQUE (worker, generation, phase)
            );
            INSERT INTO round_slots
            SELECT slot_id, worker, lane, generation, phase, state, directive,
                   prompt, prompt_sha256, created_at, activated_at, terminal_at,
                   outcome
            FROM round_slots_v6;
            DROP TABLE round_slots_v6;
            CREATE UNIQUE INDEX one_open_slot_per_lane
                ON round_slots(lane)
                WHERE state IN ('prepared', 'active', 'ambiguous');

            DROP TABLE recommendation_resolutions;
            ALTER TABLE advisor_recommendations RENAME TO advisor_recommendations_v6;
            CREATE TABLE advisor_recommendations (
                recommendation_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state = 'owner_action_required'),
                root_entry_id TEXT NOT NULL,
                critic_entry_id TEXT NOT NULL,
                browser_dispatch_authorized INTEGER NOT NULL CHECK (
                    browser_dispatch_authorized = 0
                ),
                advisor_request_id TEXT CHECK (advisor_request_id IS NULL),
                created_at REAL NOT NULL,
                UNIQUE (root_entry_id, critic_entry_id)
            );
            INSERT INTO advisor_recommendations
            SELECT recommendation_id, generation, state, root_entry_id,
                   critic_entry_id, browser_dispatch_authorized,
                   advisor_request_id, created_at
            FROM advisor_recommendations_v6;
            DROP TABLE advisor_recommendations_v6;
            CREATE TABLE recommendation_resolutions (
                resolution_id TEXT PRIMARY KEY,
                recommendation_id TEXT NOT NULL UNIQUE
                    REFERENCES advisor_recommendations(recommendation_id),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                resolution TEXT NOT NULL CHECK (
                    resolution IN (
                        'adopted_master_guidance','continue_without_advisor'
                    )
                ),
                owner_acknowledgement TEXT NOT NULL,
                master_guidance_entry_id TEXT,
                master_guidance_record_sha256 TEXT,
                browser_request_id TEXT,
                browser_receipt_sha256 TEXT,
                created_at REAL NOT NULL
            );
            DROP TABLE obstacle_reviews;
            """
        )


def _real_v5_open_recommendation(
    tmp_path: Path,
) -> tuple[Path, dict[str, object], str, str, str, str]:
    project, metadata = _project(
        tmp_path,
        roles="xhigh:2",
        workers=["xhigh", "xhigh2"],
    )
    store = CoordinationStore(project, metadata)
    root, critic, review, confirmation = _create_review_recommendation(
        store,
        complete_review=True,
    )
    recommendation_id = str(confirmation["recommendation_id"])
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE evidence_entries SET slot_id=? WHERE entry_id=?",
            (critic.slot_id, confirmation["entry_id"]),
        )
        connection.execute(
            "DELETE FROM round_slots WHERE slot_id=?",
            (review.slot_id,),
        )
    _downgrade_ready_recommendation_database_to_real_v5(store.path)
    return (
        project,
        metadata,
        recommendation_id,
        root.slot_id,
        critic.slot_id,
        str(confirmation["entry_id"]),
    )


def test_real_v5_open_recommendation_migrates_to_resolvable_v7_review(
    tmp_path: Path,
) -> None:
    (
        project,
        metadata,
        recommendation_id,
        _root_slot_id,
        _critic_slot_id,
        _critic_entry_id,
    ) = _real_v5_open_recommendation(tmp_path)

    migrated = CoordinationStore(project, metadata, create=False)
    with sqlite3.connect(migrated.path) as connection:
        version = connection.execute(
            "SELECT schema_version FROM project_state WHERE singleton=1"
        ).fetchone()[0]
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(advisor_recommendations)"
            ).fetchall()
        }
    assert version == 7
    assert "review_id" in columns
    ready = migrated.validate_open_recommendation(recommendation_id)
    assert ready["ready"] is True
    assert ready["review_id"] is not None

    second_open = CoordinationStore(project, metadata, create=False)
    assert second_open.validate_open_recommendation(recommendation_id) == ready
    with sqlite3.connect(second_open.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM obstacle_reviews").fetchone()[0]
            == 1
        )

    _stage_next_generation(second_open)
    resolved = second_open.resolve_recommendation(
        recommendation_id,
        resolution="continue_without_advisor",
        owner_acknowledgement=recommendation_id,
    )
    assert resolved["recommendation_id"] == recommendation_id
    assert second_open.project_status()["generation"] == 2

    reopened = CoordinationStore(project, metadata, create=False)
    assert reopened.recommendation_resolution(recommendation_id) == resolved


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("missing_recommendation", "one unique open recommendation"),
        ("multiple_recommendations", "one unique open recommendation"),
        ("missing_root", "one unique root obstacle"),
        ("multiple_roots", "one unique root obstacle"),
        ("multiple_critics", "one unique critic confirmation"),
        ("nonterminal_critic", "exact terminal evidence"),
        ("mismatched_confirmation", "critic evidence is not exact"),
        ("mismatched_recommendation", "root evidence is not exact"),
    ],
)
def test_real_v5_open_recommendation_ambiguous_or_mismatched_fails_atomically(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    (
        project,
        metadata,
        recommendation_id,
        _root_slot_id,
        critic_slot_id,
        critic_entry_id,
    ) = _real_v5_open_recommendation(tmp_path)
    database = project / ".coordination" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        root_entry_id, root_slot_id = connection.execute(
            "SELECT root_entry_id, "
            "(SELECT slot_id FROM evidence_entries "
            " WHERE entry_id=advisor_recommendations.root_entry_id) "
            "FROM advisor_recommendations WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        if mutation == "missing_recommendation":
            connection.execute(
                "DELETE FROM advisor_recommendations WHERE recommendation_id=?",
                (recommendation_id,),
            )
        elif mutation == "multiple_recommendations":
            connection.execute(
                """
                INSERT INTO advisor_recommendations(
                    recommendation_id, generation, state, root_entry_id,
                    critic_entry_id, browser_dispatch_authorized,
                    advisor_request_id, created_at
                ) VALUES('recommendation_ambiguous_v5', 1,
                         'owner_action_required', 'other_root', 'other_critic',
                         0, NULL, 100.0)
                """
            )
        elif mutation == "missing_root":
            connection.execute(
                "DELETE FROM evidence_entries WHERE entry_id=?",
                (root_entry_id,),
            )
        elif mutation == "multiple_roots":
            connection.execute(
                """
                INSERT INTO evidence_entries(
                    entry_id, generation, slot_id, worker, lane, kind,
                    confirms_entry_id, created_at
                ) VALUES('ambiguous_v5_root', 1, ?, 'xhigh', 'root',
                         'dead_end', NULL, 99.0)
                """,
                (root_slot_id,),
            )
        elif mutation == "multiple_critics":
            connection.execute(
                """
                INSERT INTO evidence_entries(
                    entry_id, generation, slot_id, worker, lane, kind,
                    confirms_entry_id, created_at
                ) VALUES('ambiguous_v5_critic', 1, ?, 'xhigh2', 'critic',
                         'critic_confirmation', ?, 100.0)
                """,
                (critic_slot_id, root_entry_id),
            )
        elif mutation == "nonterminal_critic":
            connection.execute(
                "UPDATE round_slots SET state='active', terminal_at=NULL, outcome=NULL "
                "WHERE slot_id=?",
                (critic_slot_id,),
            )
        elif mutation == "mismatched_confirmation":
            connection.execute(
                "UPDATE evidence_entries SET confirms_entry_id='different_root' "
                "WHERE entry_id=?",
                (critic_entry_id,),
            )
        else:
            connection.execute(
                "UPDATE advisor_recommendations SET root_entry_id='different_root' "
                "WHERE recommendation_id=?",
                (recommendation_id,),
            )

    with pytest.raises(CoordinationError, match=error):
        CoordinationStore(project, metadata, create=False)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM project_state WHERE singleton=1"
            ).fetchone()[0]
            == 5
        )
        assert "active_review_id" not in {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(project_state)").fetchall()
        }
        assert "review_id" not in {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(advisor_recommendations)"
            ).fetchall()
        }
        assert (
            connection.execute("SELECT COUNT(*) FROM obstacle_reviews").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("lane", ["root", "critic"])
@pytest.mark.parametrize(
    ("mutation", "assignment"),
    [
        ("activated_null", "activated_at=NULL"),
        ("prompt_null", "prompt=NULL"),
        ("prompt_empty", "prompt=''"),
        ("sha_null", "prompt_sha256=NULL"),
        ("sha_malformed", "prompt_sha256='NOT-LOWERCASE-SHA256'"),
        ("sha_mismatch", f"prompt_sha256='{'0' * 64}'"),
    ],
)
def test_real_v5_paid_slot_incomplete_dispatch_provenance_fails_atomically(
    tmp_path: Path,
    lane: str,
    mutation: str,
    assignment: str,
) -> None:
    del mutation
    (
        project,
        metadata,
        _recommendation_id,
        root_slot_id,
        critic_slot_id,
        _critic_entry_id,
    ) = _real_v5_open_recommendation(tmp_path)
    database = project / ".coordination" / "state.sqlite3"
    slot_id = root_slot_id if lane == "root" else critic_slot_id
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE round_slots SET {assignment} WHERE slot_id=?",
            (slot_id,),
        )

    with pytest.raises(
        CoordinationError,
        match=f"v5 {lane} paid slot lacks canonical dispatch provenance",
    ):
        CoordinationStore(project, metadata, create=False)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM project_state WHERE singleton=1"
            ).fetchone()[0]
            == 5
        )
        assert "active_review_id" not in {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(project_state)").fetchall()
        }
        assert "review_id" not in {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(advisor_recommendations)"
            ).fetchall()
        }
        assert (
            connection.execute("SELECT COUNT(*) FROM obstacle_reviews").fetchone()[0]
            == 0
        )
