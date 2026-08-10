"""Offline HTTP-contract tests for danus.verify.service + __main__ entry.

Exercises the FastAPI app via TestClient with the launcher's codex-run
MONKEYPATCHED to a fake (no subprocess, no codex, no API spend), asserting the
POST /verify + GET /health contract and every error status mapping. The
``python -m danus.verify`` entrypoint is exercised via runpy with uvicorn.run
mocked so no server ever binds.

HTTP contract under test:
  POST /verify {statement, proof, glossary_introduces?, fact_context?}
    -> 200 {verification_report, verdict, repair_hints}
  * 400 on a vacuous / precheck-failing input (before any codex run)
  * 422 on a schema-invalid body (missing/empty field — pydantic)
  * 504 on codex timeout, 500 on exit / missing-output / bad-json (launcher raises)
  GET /health -> 200 {status: "ok"}

Runs standalone (``python -m danus.verify.tests.test_service``) and under pytest.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from danus.core import (
    VERIFICATION_CONTEXT_PROJECTION,
    VERIFICATION_CONTEXT_SCHEMA_VERSION,
    verification_context_digest,
)
from danus.verify import launcher as verify_launcher
from danus.verify import service
from danus.verify.scheduler import SchedulerLimits, VerificationScheduler

_STMT = "For every integer n, n + 0 equals n."
_PROOF = (
    "Zero is the additive identity of the integers, so adding zero to any integer n "
    "leaves the value unchanged. Hence n + 0 = n for every integer n, as required."
)

_CANNED_OK = {
    "output_schema_version": 3,
    "verification_status": "final",
    "verification_report": {"summary": "fake accept", "critical_errors": [], "gaps": []},
    "verdict": "correct",
    "needs_expanded_proofs": [],
    "repair_hints": "",
}

def _make_context(
    facts=None,
    glossary=None,
    *,
    requested=None,
    expanded_proofs=None,
    expansion_round=0,
    candidate_fact_id="cccccccccccccccc",
):
    facts = list(facts or [])
    expanded_proofs = list(expanded_proofs or [])
    glossary = dict(glossary or {})
    requested = list(
        requested
        if requested is not None
        else [record["fact_id"] for record in facts]
    )
    closure_ids = [record["fact_id"] for record in facts]
    expanded_ids = [record["fact_id"] for record in expanded_proofs]
    scope = {
        "candidate_fact_id": candidate_fact_id,
        "requested_fact_ids": requested,
        "predecessor_depth": None,
        "proof_mode": "adaptive",
        "include_project_glossary": False,
        "projection": VERIFICATION_CONTEXT_PROJECTION,
        "expansion_round": expansion_round,
        "closure_fact_ids": closure_ids,
        "expanded_proof_ids": expanded_ids,
        "glossary_terms": list(glossary),
    }
    characters_used = sum(
        len(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for record in facts + expanded_proofs
    )
    characters_used += sum(
        len(json.dumps(
            {"term": term, "definition": definition},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
        for term, definition in glossary.items()
    )
    context = {
        "schema_version": VERIFICATION_CONTEXT_SCHEMA_VERSION,
        "scope": scope,
        "facts": facts,
        "expanded_proofs": expanded_proofs,
        "glossary": glossary,
        "complete": True,
        "truncated": False,
        "missing_fact_ids": [],
        "revoked_fact_ids": [],
        "omitted_fact_ids": [],
        "omitted_glossary_terms": [],
        "omitted_expanded_proof_ids": [],
        "characters_used": characters_used,
        "character_budget": 200000,
        "expanded_proof_characters": sum(
            len(json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ))
            for record in expanded_proofs
        ),
        "expanded_proof_character_budget": 200000,
    }
    context["digest"] = verification_context_digest(context=context)
    return context


_FACT_CONTEXT = _make_context()
_SCOPE = _FACT_CONTEXT["scope"]


@pytest.fixture(autouse=True)
def _isolated_scheduler(monkeypatch):
    monkeypatch.setattr(
        service,
        "_SCHEDULER",
        VerificationScheduler(
            instance_nonce=service.VERIFY_INSTANCE_NONCE,
            limits=SchedulerLimits(),
        ),
    )


@contextmanager
def _fake_run(fn):
    """Replace the launcher's codex-run (imported into service) with a fake."""
    orig_run = service.run_codex_verification
    orig_alloc = service._allocate_run_id
    orig_preflight = service.require_gateway_runtime
    orig_scheduler = service._SCHEDULER

    def adapted(*args, **kwargs):
        kwargs.pop("execution_profile", None)
        return fn(*args, **kwargs)

    service.run_codex_verification = adapted
    service._allocate_run_id = lambda statement: "RID-fake"
    service.require_gateway_runtime = lambda: None
    service._SCHEDULER = VerificationScheduler(
        instance_nonce=service.VERIFY_INSTANCE_NONCE,
        limits=SchedulerLimits(),
    )
    try:
        yield
    finally:
        service.run_codex_verification = orig_run
        service._allocate_run_id = orig_alloc
        service.require_gateway_runtime = orig_preflight
        service._SCHEDULER = orig_scheduler


def _client():
    return TestClient(service.app)


def _verify_json(**payload):
    return {
        "expected_verifier_instance_nonce": service.VERIFY_INSTANCE_NONCE,
        "expected_output_protocol_version": (
            service.VERIFICATION_OUTPUT_PROTOCOL_VERSION
        ),
        "expected_verifier_bundle_digest": service.VERIFIER_BUNDLE_DIGEST,
        **payload,
    }


# --------------------------------------------------------------------------- #
# /health                                                                     #
# --------------------------------------------------------------------------- #

def test_health_ok():
    resp = _client().get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "status",
        "pid",
        "instance_nonce",
        "output_protocol_version",
        "verifier_bundle_digest",
    }
    assert body["status"] == "ok"
    # /health self-identifies with the serving process pid (callers match it
    # against runtime/run/verify.pid to distinguish OUR verify from a foreign
    # deployment holding the same port on a shared host).
    assert isinstance(body["pid"], int) and body["pid"] > 0
    assert body["instance_nonce"] == service.VERIFY_INSTANCE_NONCE
    assert (
        body["output_protocol_version"]
        == service.VERIFICATION_OUTPUT_PROTOCOL_VERSION
        == 3
    )
    assert body["verifier_bundle_digest"] == service.VERIFIER_BUNDLE_DIGEST


def test_scheduler_wait_env_rejects_platform_timeout_overflow(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "DANUS_VERIFY_QUEUE_WAIT_SECONDS",
        str(int(threading.TIMEOUT_MAX) + 1),
    )
    with pytest.raises(RuntimeError, match="threading.TIMEOUT_MAX"):
        service._scheduler_limits_from_env()


def test_old_gateway_request_fails_before_allocation_or_codex(monkeypatch):
    calls = {"allocate": 0, "codex": 0}

    def allocate(_statement):
        calls["allocate"] += 1
        return "must-not-exist"

    def codex(*_args, **_kwargs):
        calls["codex"] += 1
        raise AssertionError("codex must not run")

    monkeypatch.setattr(service, "_allocate_run_id", allocate)
    monkeypatch.setattr(service, "run_codex_verification", codex)
    response = _client().post(
        "/verify", json={"statement": _STMT, "proof": _PROOF}
    )

    assert response.status_code == 422
    assert calls == {"allocate": 0, "codex": 0}


def test_protocol_or_health_digest_mismatch_fails_before_paid_work(monkeypatch):
    calls = {"allocate": 0, "codex": 0}
    monkeypatch.setattr(
        service,
        "_allocate_run_id",
        lambda _statement: calls.__setitem__("allocate", calls["allocate"] + 1),
    )
    monkeypatch.setattr(
        service,
        "run_codex_verification",
        lambda *_args, **_kwargs: calls.__setitem__("codex", calls["codex"] + 1),
    )

    stale_instance = _verify_json(statement=_STMT, proof=_PROOF)
    stale_instance["expected_verifier_instance_nonce"] = "stale-instance"
    response = _client().post("/verify", json=stale_instance)
    assert response.status_code == 409
    assert "instance changed" in response.json()["detail"]

    wrong_protocol = _verify_json(statement=_STMT, proof=_PROOF)
    wrong_protocol["expected_output_protocol_version"] = 2
    response = _client().post("/verify", json=wrong_protocol)
    assert response.status_code == 409

    stale_health = _verify_json(statement=_STMT, proof=_PROOF)
    stale_health["expected_verifier_bundle_digest"] = "0" * 64
    response = _client().post("/verify", json=stale_health)
    assert response.status_code == 409
    assert calls == {"allocate": 0, "codex": 0}
    assert service._SCHEDULER.snapshot()["counters"]["submitted"] == 0


# --------------------------------------------------------------------------- #
# /verify — happy path                                                        #
# --------------------------------------------------------------------------- #

def test_verify_accept_contract():
    def fake(run_id, statement, proof):
        assert run_id == "RID-fake"  # allocator was used
        return _CANNED_OK

    with _fake_run(fake):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "correct"
    assert body["verification_report"]["critical_errors"] == []
    assert "repair_hints" in body
    assert set(resp.headers).issuperset(
        {
            service.SCHEDULER_SOURCE_HEADER.lower(),
            service.SCHEDULER_KEY_HEADER.lower(),
            service.SCHEDULER_WAIT_HEADER.lower(),
        }
    )
    assert resp.headers[service.SCHEDULER_SOURCE_HEADER] == "launched"
    assert len(resp.headers[service.SCHEDULER_KEY_HEADER]) == 64
    assert resp.headers[service.SCHEDULER_WAIT_HEADER].isdecimal()
    assert not any(key.startswith("scheduler_") for key in body)


def test_exact_duplicate_coalesces_one_launch_and_cache_hits_afterward():
    entered = threading.Event()
    release = threading.Event()
    launches = 0

    def fake(run_id, statement, proof):
        nonlocal launches
        launches += 1
        entered.set()
        assert release.wait(5)
        return _CANNED_OK

    with _fake_run(fake):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                lambda: _client().post(
                    "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
                )
            )
            assert entered.wait(2)
            second = pool.submit(
                lambda: _client().post(
                    "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
                )
            )
            deadline = time.monotonic() + 2
            while service._SCHEDULER.snapshot()["waiting_clients"] != 1:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            release.set()
            responses = [first.result(2), second.result(2)]

        cached = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )

    assert launches == 1
    assert {response.headers[service.SCHEDULER_SOURCE_HEADER] for response in responses} == {
        "launched",
        "coalesced",
    }
    assert cached.headers[service.SCHEDULER_SOURCE_HEADER] == "cache_hit"
    assert all(response.json() == _CANNED_OK for response in [*responses, cached])


def test_queued_duplicate_timeout_returns_same_429_reason_and_launches_no_work():
    entered = threading.Event()
    release = threading.Event()
    launches = 0

    def fake(run_id, statement, proof):
        nonlocal launches
        launches += 1
        entered.set()
        assert release.wait(5)
        return _CANNED_OK

    running_payload = _verify_json(statement=_STMT, proof=_PROOF)
    queued_payload = _verify_json(
        statement=_STMT,
        proof=_PROOF + " This exact request has a distinct scheduler key.",
    )
    with _fake_run(fake):
        service._SCHEDULER = VerificationScheduler(
            instance_nonce=service.VERIFY_INSTANCE_NONCE,
            limits=SchedulerLimits(queue_wait_seconds=0.05),
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            running = pool.submit(
                lambda: _client().post("/verify", json=running_payload)
            )
            assert entered.wait(2)
            queued_leader = pool.submit(
                lambda: _client().post("/verify", json=queued_payload)
            )
            deadline = time.monotonic() + 2
            while service._SCHEDULER.snapshot()["distinct_queue_depth"] != 1:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            queued_duplicate = pool.submit(
                lambda: _client().post("/verify", json=queued_payload)
            )
            deadline = time.monotonic() + 2
            while service._SCHEDULER.snapshot()["waiting_clients"] != 2:
                assert time.monotonic() < deadline
                time.sleep(0.005)

            timed_out = [queued_leader.result(2), queued_duplicate.result(2)]
            release.set()
            assert running.result(2).status_code == 200

    assert launches == 1
    for response in timed_out:
        assert response.status_code == 429
        assert response.headers[service.SCHEDULER_REJECTION_HEADER] == (
            "queue_wait_timeout"
        )
        assert response.json()["detail"] == (
            "verification scheduler queue wait timed out"
        )
    assert {
        response.headers[service.SCHEDULER_SOURCE_HEADER] for response in timed_out
    } == {"rejected", "coalesced"}


def test_execution_profile_drift_changes_key_and_launches_again(monkeypatch):
    launches = 0

    def fake(run_id, statement, proof):
        nonlocal launches
        launches += 1
        return _CANNED_OK

    with _fake_run(fake):
        first = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
        cached = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
        monkeypatch.setenv("DANUS_VERIFY_MODEL", "profile-drift-model")
        drifted = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )

    assert launches == 2
    assert cached.headers[service.SCHEDULER_SOURCE_HEADER] == "cache_hit"
    assert first.headers[service.SCHEDULER_KEY_HEADER] != drifted.headers[
        service.SCHEDULER_KEY_HEADER
    ]


def test_scheduler_key_binds_every_exact_request_component():
    profile = verify_launcher.capture_execution_profile().canonical()
    baseline = service.VerifyRequest.model_validate(
        _verify_json(
            statement=_STMT,
            proof=_PROOF,
            glossary_introduces={"X": "definition"},
            fact_context=_FACT_CONTEXT,
        )
    )
    baseline_key = service._scheduler_key(baseline, execution_profile=profile)
    variants = [
        baseline.model_copy(update={"statement": _STMT + " Exact drift."}),
        baseline.model_copy(update={"proof": _PROOF + " Exact drift."}),
        baseline.model_copy(update={"glossary_introduces": {"X": "changed"}}),
        baseline.model_copy(
            update={"fact_context": _make_context(candidate_fact_id="dddddddddddddddd")}
        ),
    ]
    assert all(
        service._scheduler_key(variant, execution_profile=profile) != baseline_key
        for variant in variants
    )
    drifted_profile = dict(profile, model=profile["model"] + "-different")
    assert service._scheduler_key(
        baseline, execution_profile=drifted_profile
    ) != baseline_key


def test_valid_needs_context_is_cached_after_independent_validation():
    launches = 0
    needs_context = {
        "output_schema_version": 3,
        "verification_status": "needs_context",
        "verification_report": {"summary": "", "critical_errors": [], "gaps": []},
        "verdict": "wrong",
        "needs_expanded_proofs": [{"id": "aaaaaaaaaaaaaaaa", "reason": "needed"}],
        "repair_hints": "",
    }

    def fake(run_id, statement, proof):
        nonlocal launches
        launches += 1
        return needs_context

    with _fake_run(fake):
        first = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
        second = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    assert first.status_code == second.status_code == 200
    assert second.headers[service.SCHEDULER_SOURCE_HEADER] == "cache_hit"
    assert launches == 1


def test_invalid_completed_result_fails_and_is_never_cached():
    launches = 0
    invalid = dict(_CANNED_OK, verdict="wrong")

    def fake(run_id, statement, proof):
        nonlocal launches
        launches += 1
        return invalid

    with _fake_run(fake):
        responses = [
            _client().post(
                "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
            )
            for _ in range(2)
        ]
    assert [response.status_code for response in responses] == [500, 500]
    assert all(
        response.headers[service.SCHEDULER_SOURCE_HEADER] == "launched"
        for response in responses
    )
    assert launches == 2


def test_scheduler_snapshot_is_read_only_and_has_no_request_keys():
    response = _client().get("/scheduler")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "schema_version",
        "instance_nonce",
        "paid_concurrency_limit",
        "running",
        "distinct_queue_depth",
        "active_keys",
        "waiting_clients",
        "cache_entries",
        "cache_bytes",
        "limits",
        "counters",
    }
    assert body["instance_nonce"] == service.VERIFY_INSTANCE_NONCE
    assert body["paid_concurrency_limit"] == 1
    assert "running_key" not in body and "queued_keys" not in body
    assert _STMT not in json.dumps(body)


def test_verify_reject_verdict_still_200():
    # a "wrong" verdict is a normal 200 response (the verdict is the payload).
    canned = {
        "output_schema_version": 3,
        "verification_status": "final",
        "verification_report": {
            "summary": "gap",
            "critical_errors": [],
            "gaps": [{
                "location": "proof",
                "issue": "missing step",
                "candidate_evidence": {
                    "source": "proof",
                    "line": 1,
                    "exact_line": _PROOF,
                },
            }],
        },
        "verdict": "wrong",
        "needs_expanded_proofs": [],
        "repair_hints": "fix the gap",
    }
    with _fake_run(lambda run_id, statement, proof: canned):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    assert resp.status_code == 200 and resp.json()["verdict"] == "wrong"


def test_verify_forwards_optional_fact_context():
    def fake(
        run_id, statement, proof, fact_context=None, glossary_introduces=None
    ):
        assert run_id == "RID-fake"
        assert fact_context == _FACT_CONTEXT
        assert glossary_introduces == {"X": "a compact space"}
        return _CANNED_OK

    with _fake_run(fake):
        resp = _client().post(
            "/verify",
            json=_verify_json(
                statement=_STMT,
                proof=_PROOF,
                fact_context=_FACT_CONTEXT,
                glossary_introduces={"X": "a compact space"},
            ),
        )
    assert resp.status_code == 200 and resp.json()["verdict"] == "correct"
    assert resp.json()["verification_context_digest"] == _FACT_CONTEXT["digest"]


def test_verify_rejects_incomplete_or_tampered_context_before_codex():
    cases = []
    incomplete = dict(_FACT_CONTEXT, complete=False)
    cases.append(incomplete)
    truncated = dict(_FACT_CONTEXT, truncated=True)
    cases.append(truncated)
    tampered = dict(_FACT_CONTEXT, digest="sha256:" + "0" * 64)
    cases.append(tampered)
    omitted_glossary = dict(
        _FACT_CONTEXT, omitted_glossary_terms=["Q_X"], complete=False, truncated=True
    )
    cases.append(omitted_glossary)
    inconsistent_glossary = dict(_FACT_CONTEXT, glossary={"Q_X": "changed"})
    cases.append(inconsistent_glossary)
    project_glossary_scope = dict(_SCOPE, include_project_glossary=True)
    project_glossary_context = dict(
        _FACT_CONTEXT, scope=project_glossary_scope
    )
    project_glossary_context.pop("digest")
    project_glossary_context["digest"] = verification_context_digest(
        context=project_glossary_context
    )
    cases.append(project_glossary_context)
    malformed = dict(_FACT_CONTEXT)
    malformed.pop("scope")
    cases.append(malformed)

    with _fake_run(_must_not_run):
        for fact_context in cases:
            resp = _client().post(
                "/verify",
                json=_verify_json(
                    statement=_STMT, proof=_PROOF, fact_context=fact_context
                ),
            )
            assert resp.status_code == 400 and "invalid fact_context" in resp.json()["detail"]


def test_verify_requires_context_for_cited_internal_fact():
    proof = _PROOF + " Apply verified fact aaaaaaaaaaaaaaaa to finish."
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=proof)
        )
    assert resp.status_code == 400 and "fact_context is required" in resp.json()["detail"]


def test_verify_requires_context_for_internal_fact_id_in_statement():
    statement = _STMT + " This is the consequence recorded as bbbbbbbbbbbbbbbb."
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify", json=_verify_json(statement=statement, proof=_PROOF)
        )
    assert resp.status_code == 400 and "fact_context is required" in resp.json()["detail"]


def test_verify_requires_declared_and_cited_predecessors_to_match():
    fact_id = "aaaaaaaaaaaaaaaa"
    facts = [{
        "fact_id": fact_id,
        "statement": "A holds",
        "predecessors": [],
        "glossary_introduces": {},
    }]
    context = _make_context(facts)
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify",
            json=_verify_json(
                statement=_STMT, proof=_PROOF, fact_context=context
            ),
        )
    assert resp.status_code == 400 and "declared but not cited" in resp.json()["detail"]


def test_verify_matches_declared_predecessors_across_statement_and_proof():
    first = "aaaaaaaaaaaaaaaa"
    second = "bbbbbbbbbbbbbbbb"
    facts = [
        {
            "fact_id": fact_id,
            "statement": f"Premise {index} holds",
            "predecessors": [],
            "glossary_introduces": {},
        }
        for index, fact_id in enumerate((first, second), start=1)
    ]
    context = _make_context(facts)
    statement = _STMT + f" Assume the direct predecessor {first}."
    proof = _PROOF + f" Apply the other direct predecessor {second}."

    def fake(run_id, statement, proof, fact_context=None, glossary_introduces=None):
        assert fact_context == context
        return _CANNED_OK

    with _fake_run(fake):
        resp = _client().post(
            "/verify",
            json=_verify_json(
                statement=statement, proof=proof, fact_context=context
            ),
        )
    assert resp.status_code == 200


def test_verify_accepts_exact_adaptive_expansion_and_rejects_proof_leakage():
    direct = "aaaaaaaaaaaaaaaa"
    ancestor = "bbbbbbbbbbbbbbbb"
    facts = [
        {
            "fact_id": direct,
            "statement": "Direct premise",
            "predecessors": [ancestor],
            "glossary_introduces": {},
        },
        {
            "fact_id": ancestor,
            "statement": "Ancestor premise",
            "predecessors": [],
            "glossary_introduces": {"A": "the ancestor object"},
        },
    ]
    expanded = [{"fact_id": ancestor, "proof": "complete ancestor proof"}]
    context = _make_context(
        facts,
        requested=[direct],
        expanded_proofs=expanded,
        expansion_round=1,
    )

    def fake(run_id, statement, proof, fact_context=None, glossary_introduces=None):
        assert fact_context == context
        return _CANNED_OK

    with _fake_run(fake):
        resp = _client().post(
            "/verify",
            json=_verify_json(
                statement=_STMT,
                proof=_PROOF + f" Apply {direct}.",
                fact_context=context,
            ),
        )
    assert resp.status_code == 200

    leaked = json.loads(json.dumps(context))
    leaked["facts"][1]["proof"] = "leaked ancestor proof"
    leaked.pop("digest")
    leaked["digest"] = verification_context_digest(context=leaked)
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify",
            json=_verify_json(
                statement=_STMT,
                proof=_PROOF + f" Apply {direct}.",
                fact_context=leaked,
            ),
        )
    assert resp.status_code == 400
    assert "fact statement card" in resp.json()["detail"]


def test_verify_rejects_undeclared_fact_id_in_statement_with_context():
    statement = _STMT + " This also invokes cccccccccccccccc."
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify",
            json=_verify_json(
                statement=statement,
                proof=_PROOF,
                fact_context=_FACT_CONTEXT,
            ),
        )
    assert resp.status_code == 400 and "undeclared" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# /verify — pre-parse ingress controls                                        #
# --------------------------------------------------------------------------- #

async def _raw_asgi_post(*, receive):
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/verify",
        "raw_path": b"/verify",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 43210),
        "server": ("127.0.0.1", 8091),
    }

    async def send(message):
        messages.append(message)

    await service.app(scope, receive, send)
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert len(starts) == 1
    return starts[0]["status"]


def test_verify_rejects_chunked_oversized_body_before_pydantic(monkeypatch):
    monkeypatch.setattr(service, "VERIFY_MAX_REQUEST_BYTES", 8)
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": b"123456789", "more_body": False}

    assert asyncio.run(_raw_asgi_post(receive=receive)) == 413


def test_verify_times_out_slow_body_before_pydantic(monkeypatch):
    monkeypatch.setattr(service, "VERIFY_BODY_TIMEOUT_SECONDS", 0.01)

    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    assert asyncio.run(_raw_asgi_post(receive=receive)) == 408


def test_verify_rejects_when_preparse_admission_is_full(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(service, "_ADMISSION_SLOTS", slots)
    try:
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    finally:
        slots.release()
    assert resp.status_code == 429 and "busy" in resp.json()["detail"]


def test_paid_lease_survives_asgi_cancellation_until_sync_job_terminal(monkeypatch):
    """A disconnected caller cannot free capacity while its paid job runs."""
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    allocations = []

    def blocked_codex(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return _CANNED_OK

    monkeypatch.setattr(service, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(
        service,
        "_allocate_run_id",
        lambda statement: allocations.append(statement) or f"RID-{len(allocations)}",
    )
    monkeypatch.setattr(service, "run_codex_verification", blocked_codex)

    payload = json.dumps(_verify_json(statement=_STMT, proof=_PROOF)).encode()

    async def scenario():
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}

        messages = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/verify",
            "raw_path": b"/verify",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 44000),
            "server": ("127.0.0.1", 8091),
        }

        async def send(message):
            messages.append(message)

        first = asyncio.create_task(service.app(scope, receive, send))
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()  # model an ASGI/client cancellation after paid work began

        # An exact duplicate joins the still-owned paid flight.  Cancellation of
        # the ASGI task must neither launch again nor release the paid slot.
        second_task = asyncio.create_task(asyncio.to_thread(
            lambda: _client().post(
                "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
            )
        ))
        deadline = asyncio.get_running_loop().time() + 2
        while service._SCHEDULER.snapshot()["waiting_clients"] != 1:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        assert len(allocations) == 1

        health = await asyncio.to_thread(lambda: _client().get("/health"))
        assert health.status_code == 200
        assert not finished.is_set()

        release.set()
        second = await asyncio.wait_for(second_task, 2)
        assert second.status_code == 200
        assert second.headers[service.SCHEDULER_SOURCE_HEADER] == "coalesced"
        try:
            await asyncio.wait_for(first, 2)
        except asyncio.CancelledError:
            pass
        assert await asyncio.to_thread(finished.wait, 2)

        third = await asyncio.to_thread(
            lambda: _client().post(
                "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
            )
        )
        assert third.status_code == 200
        assert third.headers[service.SCHEDULER_SOURCE_HEADER] == "cache_hit"
        assert len(allocations) == 1

    try:
        asyncio.run(scenario())
    finally:
        release.set()


# --------------------------------------------------------------------------- #
# /verify — precheck rejections happen BEFORE any codex run (400)             #
# --------------------------------------------------------------------------- #

def _must_not_run(*a, **k):
    raise AssertionError("codex must not run when a precheck rejects")


def test_verify_vacuous_proof_400():
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof="QED")
        )
    assert resp.status_code == 400 and "vacuous proof" in resp.json()["detail"]


def test_verify_p1_precheck_400():
    bad = ("The result holds as declared in problem.md, which lists it as a verified "
           "building block, so we are done with the argument here.")
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=bad)
        )
    assert resp.status_code == 400 and "[P1 on proof]" in resp.json()["detail"]


def test_gateway_preflight_failure_allocates_nothing_and_starts_no_codex(monkeypatch):
    calls = {"allocate": 0, "codex": 0}

    def fail_preflight():
        raise service.GatewayRuntimeUnavailable("bad gateway import")

    def allocate(_statement):
        calls["allocate"] += 1
        return "must-not-exist"

    def codex(*args, **kwargs):
        calls["codex"] += 1
        raise AssertionError("codex must not run")

    monkeypatch.setattr(service, "require_gateway_runtime", fail_preflight)
    monkeypatch.setattr(service, "_allocate_run_id", allocate)
    monkeypatch.setattr(service, "run_codex_verification", codex)
    resp = _client().post(
        "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
    )

    assert resp.status_code == 500
    assert calls == {"allocate": 0, "codex": 0}
    assert "gateway runtime preflight failed" in resp.json()["detail"]


def test_second_preflight_failure_returns_500_without_result_or_codex(
    tmp_path, monkeypatch
):
    results_root = tmp_path / "runs"
    codex_calls = []

    def fail_second_preflight():
        raise verify_launcher.GatewayRuntimeUnavailable("runtime broke after admission")

    monkeypatch.setattr(service, "require_gateway_runtime", lambda: None)
    monkeypatch.setattr(
        verify_launcher, "require_gateway_runtime", fail_second_preflight
    )
    monkeypatch.setattr(
        verify_launcher.subprocess,
        "run",
        lambda *args, **kwargs: codex_calls.append((args, kwargs)),
    )
    monkeypatch.setenv("VERIFIER_RESULTS_DIR", str(results_root))

    resp = _client().post(
        "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
    )

    assert resp.status_code == 500
    assert "runtime broke after admission" in resp.json()["detail"]
    assert codex_calls == []
    allocated = list(results_root.iterdir())
    assert len(allocated) == 1
    assert list(allocated[0].iterdir()) == []


# --------------------------------------------------------------------------- #
# /verify — launcher error mappings surface as the raised status              #
# --------------------------------------------------------------------------- #

def _raiser(status, detail):
    def fn(run_id, statement, proof):
        raise HTTPException(status_code=status, detail=detail)
    return fn


def test_verify_timeout_504():
    with _fake_run(_raiser(504, "codex exec timed out after 900s")):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    assert resp.status_code == 504 and "timed out" in resp.json()["detail"]


def test_verify_exit_500():
    with _fake_run(_raiser(500, "codex exec failed with exit code 7")):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    assert resp.status_code == 500 and "exit code" in resp.json()["detail"]


def test_verify_missing_output_500():
    with _fake_run(_raiser(500, "verification output was not found")):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    assert resp.status_code == 500 and "was not found" in resp.json()["detail"]


def test_verify_bad_json_500():
    with _fake_run(_raiser(500, "verification output ... is not valid JSON")):
        resp = _client().post(
            "/verify", json=_verify_json(statement=_STMT, proof=_PROOF)
        )
    assert resp.status_code == 500 and "not valid JSON" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# /verify — schema validation (pydantic, 422) before prechecks               #
# --------------------------------------------------------------------------- #

def test_verify_empty_field_422():
    with _fake_run(_must_not_run):
        resp = _client().post(
            "/verify", json=_verify_json(statement="", proof=_PROOF)
        )
    assert resp.status_code == 422


def test_verify_missing_field_422():
    with _fake_run(_must_not_run):
        resp = _client().post("/verify", json=_verify_json(statement=_STMT))
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# `python -m danus.verify` entry — uvicorn mocked, no bind                    #
# --------------------------------------------------------------------------- #

def test_main_entry_runs_uvicorn(monkeypatch):
    import os
    import runpy

    calls = {}
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(app, host, port):  # noqa: ANN001
        calls["app"] = app
        calls["host"] = host
        calls["port"] = port

    fake_uvicorn.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("VERIFY_HOST", "127.0.0.1")
    monkeypatch.setenv("VERIFY_PORT", "8199")
    monkeypatch.delenv("CODEX_TIMEOUT_SECONDS", raising=False)

    runpy.run_module("danus.verify", run_name="__main__")

    assert calls["host"] == "127.0.0.1" and calls["port"] == 8199
    assert calls["app"] is not None
    # the entrypoint sets a bounded default per-verification timeout
    assert os.environ.get("CODEX_TIMEOUT_SECONDS") == "900"


def main() -> None:
    import inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                print(f"  [skip standalone] {name} (needs pytest fixture)")
                continue
            fn()
            print(f"  [ok] {name}")
    print("ALL SERVICE TESTS PASSED")


if __name__ == "__main__":
    main()
