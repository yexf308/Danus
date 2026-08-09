"""Tests for danus.gateway — role gating + tool wiring over danus.core.

The verify service is mocked (we replace ``server._verify``), so fact_submit is
exercised without a live verifier or codex. Config is read from the environment
at call time, so each test sets DANUS_* around a temp project dir.

Runs standalone (``python -m danus.gateway.tests.test_gateway``) and under pytest.
"""

from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import FactGraph, GlobalMemory, compute_fact_id
from danus.gateway import build_app, tools_for
from danus.gateway import server
from danus.hotjoin import HotJoinStore


_TEST_VERIFIER_BUNDLE_DIGEST = "a" * 64


def _health_response(
    *, protocol=server.VERIFICATION_OUTPUT_PROTOCOL_VERSION,
    digest=_TEST_VERIFIER_BUNDLE_DIGEST,
    include_contract=True,
):
    body = {"status": "ok", "pid": 1234}
    if include_contract:
        body.update(
            output_protocol_version=protocol,
            verifier_bundle_digest=digest,
        )
    return io.BytesIO(json.dumps(body).encode("utf-8"))


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
            capture.update({
                "statement": statement, "proof": proof, "fact_context": fact_context,
                "glossary_introduces": glossary_introduces,
            })
        if raise_exc is not None:
            raise raise_exc
        findings = [] if verdict == "correct" else [
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
        return {"output_schema_version": 3, "verification_status": "final",
                "verdict": verdict, "needs_expanded_proofs": [],
                "repair_hints": repair_hints,
                "verification_context_digest": fact_context["digest"],
                "verification_report": {
                    "summary": "mock", "critical_errors": [], "gaps": findings,
                }}

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
        findings = [{
            "location": "proof",
            "issue": "mock rejection",
            "candidate_evidence": {
                "source": "proof",
                "line": 1,
                "exact_line": candidate_proof,
            },
        }]
        repair_hints = repair_hints or "repair the mock gap"
    return {
        "output_schema_version": 3,
        "verification_status": status,
        "verification_report": {
            "summary": "mock", "critical_errors": [], "gaps": findings,
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
    assert "fact_context" not in tools_for("verifier")
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="tester"
    ):
        out = server.gm_add("plan", claim="reduce to q>=2", evidence="")
        assert out["kind"] == "plan" and out["id"]
        hits = server.gm_search("reduce")
        assert hits["results_by_kind"]["plan"]["count"] == 1
        # fact_search over an empty graph is well-formed
        assert server.fact_search("anything")["results"] == []


def test_fact_context_gateway_defaults_to_summary_only():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="tester"
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="Base", proof="proof base")
        child = fg.add(problem_id="P", author="w", statement="Child", proof="proof child",
                       predecessors=[base])
        out = server.fact_context([child])
        assert out["facts"] == [{
            "fact_id": child, "statement": "Child", "predecessors": [base],
            "glossary_introduces": {},
        }]
        selected = server.fact_context(
            [child], predecessor_depth=None, proof_mode="selected"
        )
        assert selected["facts"][0]["proof"] == "proof child"
        assert "proof" not in selected["facts"][1]


def test_fact_submit_accept_writes_fact_and_traces():
    captured = {}
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct", capture=captured):
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
    response_body = io.BytesIO(
        b"x" * (server._VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES + 1)
    )

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


def test_fact_submit_audits_body_free_human_frontier_without_verifier_leak():
    captured = {}
    sentinel = "OWNER-DIRECTION-MUST-NOT-ENTER-VERIFIER-4b91"
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d,
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="worker_high",
        DANUS_ROLE="worker",
        DANUS_HOTJOIN_ENABLED="1",
        DANUS_HOTJOIN_TARGET="worker_high",
        DANUS_VERIFY_URL="http://mock",
        DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct", capture=captured):
        store = HotJoinStore(Path(d))
        message = store.enqueue(target="worker_high", body=sentinel)
        assert (
            store.claim(
                target="worker_high", owner="test-broker", allow_queued=True
            )
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
        (project / ".human-intervention").symlink_to(Path(outside), target_is_directory=True)
        with _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_ROLE="worker",
            DANUS_HOTJOIN_ENABLED="1",
            DANUS_HOTJOIN_TARGET="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ), _mock_verify("correct", capture={}) as _unused:
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("wrong", repair_hints="gap in step 2"):
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
            with tempfile.TemporaryDirectory() as d, _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="worker_high",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ), _mock_verify("correct"):
                result = server.fact_submit(
                    statement="A durable accepted statement",
                    proof="A complete durable proof for this accepted statement.",
                )
                expected_error = (
                    str(injected_error) or type(injected_error).__name__
                )
                assert result["accepted"] is True and result["fact_id"]
                assert result["promoted"] is True
                assert result["submission_status"] == "promoted"
                assert result["trace_error"] == expected_error
                assert FactGraph(Path(d)).exists(result["fact_id"])
    finally:
        GlobalMemory.append = original_append


def test_fact_submit_verify_error_is_clean():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct", raise_exc=RuntimeError("service down")):
        res = server.fact_submit(statement="s", proof="p")
        assert res["accepted"] is False and res["verdict"] == "error"
        assert res["promoted"] is False
        assert res["submission_status"] == "error"
        assert res["verification_verdict"] is None
        assert "service down" in res["error"]


def test_fact_submit_sends_full_statement_closure_and_no_ancestor_proofs():
    captured = {}
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct", capture=captured):
        fg = FactGraph(Path(d))
        base = fg.add(
            problem_id="P",
            author="w",
            statement="A holds",
            proof="pf A",
            glossary_introduces={"A": "the base assertion"},
        )
        direct = fg.add(problem_id="P", author="w", statement="B from A", proof="pf B",
                        predecessors=[base])
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
        assert facts[-1]["glossary_introduces"] == {
            "A": "the base assertion"
        }
        assert captured["fact_context"]["expanded_proofs"] == []
        assert captured["fact_context"]["scope"]["proof_mode"] == "adaptive"
        assert captured["fact_context"]["scope"]["expansion_round"] == 0
        assert captured["fact_context"]["scope"]["closure_fact_ids"] == [
            direct, base
        ]
        assert captured["fact_context"]["complete"] is True
        assert captured["glossary_introduces"] == {
            "C_result": "the downstream conclusion"
        }


def test_fact_submit_adaptively_hydrates_only_requested_ancestor_proof():
    contexts = []
    candidate_proofs = []
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        fg = FactGraph(Path(d))
        base = fg.add(
            problem_id="P", author="w", statement="Base premise",
            proof="BASE PROOF SECRET BYTES",
        )
        left = fg.add(
            problem_id="P", author="w", statement="Left consequence",
            proof="LEFT PROOF MUST STAY OMITTED", predecessors=[base],
        )
        right = fg.add(
            problem_id="P", author="w", statement="Right consequence",
            proof="RIGHT PROOF MUST STAY OMITTED", predecessors=[base],
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d,
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock",
        DANUS_PROBLEM_ID="P",
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
        with tempfile.TemporaryDirectory() as d, _env(
            DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
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
            server._verify = lambda statement, proof, fact_context=None, glossary_introduces=None, req=requests: _verify_response(
                fact_context,
                status="needs_context",
                verdict="wrong",
                requests=req,
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        fg = FactGraph(Path(d))
        ancestor = fg.add(
            problem_id="P", author="w", statement="Ancestor", proof="proof A"
        )
        original = server._verify
        server._verify = lambda statement, proof, fact_context=None, glossary_introduces=None: _verify_response(
            fact_context,
            status="needs_context",
            verdict="correct",
            requests=[{"id": ancestor, "reason": "need it"}],
        )
        try:
            invalid = server.fact_submit(
                statement="Candidate one", proof=f"Use {ancestor}.",
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
                statement="Candidate two", proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert calls == 2
        assert repeated["accepted"] is False and repeated["verdict"] == "error"
        assert "already expanded" in repeated["error"]


def test_fact_submit_final_wrong_after_expansion_never_writes():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
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
                statement="Candidate", proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert calls == 2
        assert result["accepted"] is False and result["verdict"] == "wrong"
        assert result["expanded_proof_ids"] == [ancestor]
        assert fg.list() == [ancestor]


def test_fact_submit_expansion_proof_and_round_budgets_fail_closed():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS="1",
    ):
        fg = FactGraph(Path(d))
        ancestor = fg.add(
            problem_id="P", author="w", statement="Ancestor",
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
                statement="Candidate", proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert calls == 1
        assert result["accepted"] is False and result["verdict"] == "error"
        assert "omitted expanded proof" in result["error"]

    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_MAX_EXPANSION_ROUNDS="0",
    ):
        fg = FactGraph(Path(d))
        ancestor = fg.add(
            problem_id="P", author="w", statement="Ancestor", proof="proof A"
        )
        original = server._verify
        server._verify = lambda statement, proof, fact_context=None, glossary_introduces=None: _verify_response(
            fact_context,
            status="needs_context",
            verdict="wrong",
            requests=[{"id": ancestor, "reason": "inspect proof"}],
        )
        try:
            result = server.fact_submit(
                statement="Candidate", proof=f"Use {ancestor}.",
                predecessors=[ancestor],
            )
        finally:
            server._verify = original
        assert result["accepted"] is False and result["verdict"] == "error"
        assert "maximum expansion rounds (0) exceeded" in result["error"]


def test_fact_submit_expanded_proof_count_budget_fails_before_hydration():
    contexts = []
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_MAX_EXPANDED_PROOFS="1",
    ):
        fg = FactGraph(Path(d))
        left = fg.add(
            problem_id="P", author="w", statement="Left ancestor",
            proof="LEFT PROOF MUST NOT BE HYDRATED",
        )
        right = fg.add(
            problem_id="P", author="w", statement="Right ancestor",
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

        def request_both(
            statement, proof, fact_context=None, glossary_introduces=None
        ):
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct", capture=captured):
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct", capture=captured):
        FactGraph(Path(d)).add(
            problem_id="P", author="w", statement="Definition source",
            proof="definition proof", glossary_introduces={"Q_Y": "a project object"},
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        original = server._verify

        def change_glossary_during_verify(
            statement, proof, fact_context=None, glossary_introduces=None
        ):
            FactGraph(Path(d)).add(
                problem_id="P", author="other", statement="New definition", proof="proof",
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
                    "summary": "mock", "critical_errors": [], "gaps": [],
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


def test_fact_submit_correct_glossary_conflict_is_not_promoted_or_written():
    """A correct verdict is not a successful publication if glossary CAS fails."""
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d,
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock",
        DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct"):
        fg = FactGraph(Path(d))
        existing = fg.add(
            problem_id="P",
            author="other",
            statement="Q_X is fixed",
            proof="definition proof",
            glossary_introduces={"Q_X": "the existing project object"},
        )

        def graph_bytes() -> dict[str, bytes]:
            return {
                str(path.relative_to(fg.dir)): path.read_bytes()
                for path in sorted(fg.dir.rglob("*"))
                if path.is_file()
            }

        before_graph = graph_bytes()
        before_glossary = fg.glossary_path.read_bytes()
        result = server.fact_submit(
            statement="Q_X has another property",
            proof="A complete proof for the proposed meaning of Q_X.",
            glossary_introduces={"Q_X": "a conflicting project object"},
        )

        # ``accepted`` keeps its historical verifier-only meaning. The explicit
        # promotion fields are the fail-honest publication signal.
        assert result["accepted"] is True
        assert result["verification_verdict"] == "correct"
        assert result["promoted"] is False
        assert result["submission_status"] == "verified_not_promoted"
        assert result["fact_id"] is None
        assert "glossary_conflict" in result["write_error"]

        assert fg.list() == [existing]
        assert fg.glossary_path.read_bytes() == before_glossary
        assert graph_bytes() == before_graph
        assert not fg.pending_add_path.exists()

        trace = GlobalMemory(Path(d)).read("verification")[-1]
        assert trace["verdict"] == "correct"
        assert trace["verification_verdict"] == "correct"
        assert trace["promoted"] is False
        assert trace["submission_status"] == "verified_not_promoted"
        assert trace["fact_id"] is None
        assert "glossary_conflict" in trace["write_error"]


def test_fact_submit_empty_write_exceptions_are_not_promoted_or_written():
    """A falsey diagnostic must never turn a failed graph write into success."""
    original_add = FactGraph.add_if_context_unchanged
    try:
        for injected_error in (OSError(), MemoryError()):
            def fail_write(self, **kwargs):
                raise injected_error

            FactGraph.add_if_context_unchanged = fail_write
            with tempfile.TemporaryDirectory() as d, _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="worker_high",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ), _mock_verify("correct"):
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
        with tempfile.TemporaryDirectory() as d, _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ), _mock_verify("correct"):
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
        with tempfile.TemporaryDirectory() as d, _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ), _mock_verify("correct"):
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
            with tempfile.TemporaryDirectory() as d, _env(
                DANUS_PROJECT_DIR=d,
                DANUS_AGENTS_ROOT=None,
                DANUS_AUTHOR="worker_high",
                DANUS_VERIFY_URL="http://mock",
                DANUS_PROBLEM_ID="P",
            ), _mock_verify("correct"):
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
        with tempfile.TemporaryDirectory() as d, _env(
            DANUS_PROJECT_DIR=d,
            DANUS_AGENTS_ROOT=None,
            DANUS_AUTHOR="worker_high",
            DANUS_VERIFY_URL="http://mock",
            DANUS_PROBLEM_ID="P",
        ), _mock_verify("correct"):
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d,
        DANUS_AGENTS_ROOT=None,
        DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock",
        DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct"):
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


def test_fact_submit_blocks_missing_and_revoked_before_verify():
    calls = {"count": 0}

    def must_not_verify(statement, proof, fact_context=None, glossary_introduces=None):
        calls["count"] += 1
        raise AssertionError("verifier must not be called")

    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        fg = FactGraph(Path(d))
        revoked = fg.add(problem_id="P", author="w", statement="A", proof="pf A")
        fg.revoke(revoked, reason="wrong")
        removed = fg.add(problem_id="P", author="w", statement="Removed", proof="pf")
        dangling = fg.add(problem_id="P", author="w", statement="Dangling",
                          proof="uses missing", predecessors=[removed])
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

    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_CONTEXT_MAX_CHARS="1",
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
        with tempfile.TemporaryDirectory() as d, _env(
            DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
        ), _mock_verify("correct"):
            res = server.fact_submit(statement="X thing", proof="because")
            assert res["accepted"] is True and res["undefined_symbols"] == []
    finally:
        FactGraph.undefined_symbols = orig


def test_fact_submit_nondict_verify_body_is_clean():
    # a valid-JSON but non-dict verify response must not crash the gate
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        orig = server._verify
        server._verify = lambda statement, proof, fact_context=None, glossary_introduces=None: ["not", "a", "dict"]
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
                "summary": "has gap", "critical_errors": [],
                "gaps": [{
                    "location": "proof",
                    "issue": "missing step",
                    "candidate_evidence": {
                        "source": "proof",
                        "line": 1,
                        "exact_line": "a complete proof",
                    },
                }],
            },
            "repair_hints": "",
        },
    ]
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        original = server._verify
        try:
            for payload in invalid_payloads:
                server._verify = lambda statement, proof, fact_context=None, glossary_introduces=None, p=payload: {
                    **p,
                    "verification_context_digest": fact_context["digest"],
                }
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
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
                    "gaps": [{
                        "location": "proof line 1",
                        "issue": "The candidate allegedly used d < h.",
                        "candidate_evidence": {
                            "source": "proof",
                            "line": 1,
                            "exact_line": "The candidate proves d < h.",
                        },
                    }],
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        original = server._verify
        server._verify = lambda statement, proof, fact_context=None, glossary_introduces=None: {
            "output_schema_version": 2,  # old unattested service
            "verification_status": "final",
            "verdict": "correct",
            "needs_expanded_proofs": [],
            "repair_hints": "",
            "verification_report": {
                "summary": "legacy response", "critical_errors": [], "gaps": [],
            },
        }
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
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
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
                    "summary": "mock", "critical_errors": [], "gaps": [],
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
            self.wfile.write(b'{"verdict": "correct", "verification_report": {"ok": true}}')

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
                "facts": [], "complete": True, "truncated": False,
                "missing_fact_ids": [], "revoked_fact_ids": [],
                "omitted_fact_ids": [], "characters_used": 0,
                "character_budget": 200000,
            }
            out = server._verify("S(n)=n^2", "induction", fact_context=fact_context)
            assert out["verdict"] == "correct"
        sent = json.loads(captured["body"])
        assert sent["expected_output_protocol_version"] == 3
        assert (
            sent["expected_verifier_bundle_digest"]
            == _TEST_VERIFIER_BUNDLE_DIGEST
        )
        assert sent["statement"] == "S(n)=n^2"
        assert sent["fact_context"] == fact_context
        assert captured["ctype"] == "application/json"
        # a garbage timeout falls back to the default (no crash)
        with _env(DANUS_VERIFY_URL=url, DANUS_VERIFY_TIMEOUT="not-an-int"):
            assert server._verify("s", "p")["verdict"] == "correct"
    finally:
        srv.shutdown()


def test_fact_revoke_cascades():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="main_agent",
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        child = fg.add(problem_id="P", author="w", statement="B from A", proof="uses A",
                       predecessors=[base])
        out = server.fact_revoke(base, reason="A was wrong")
        assert set(out["revoked"]) == {base, child}
        assert not fg.exists(base) and not fg.exists(child)


def test_search_arxiv_theorems_delegates(monkeypatch=None):
    # the tool is a thin wrapper over danus.integrations.search; stub it (offline)
    orig = server._arxiv_search
    server._arxiv_search = lambda query, num_results=10: {
        "query": query, "num_results": num_results, "results": [{"title": "T"}]}
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
            out = server.gm_add("master_guidance", claim="try route X", evidence="", project="proj_a")
            assert out["id"]
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
                lambda: server.fact_search("x", project="other"),
                lambda: server.fact_context([], project="other"),
            ):
                with pytest.raises(RuntimeError, match="only the main role"):
                    operation()
        assert GlobalMemory(other).read("master_guidance") == []


def test_project_resolution_rejects_symlinked_selector_and_pinned_project():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
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
    test_fact_submit_correct_glossary_conflict_is_not_promoted_or_written()
    print("  [ok] correct + glossary conflict -> verified, not promoted, no graph change")
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
