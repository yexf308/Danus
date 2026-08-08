"""Tests for danus.gateway — role gating + tool wiring over danus.core.

The verify service is mocked (we replace ``server._verify``), so fact_submit is
exercised without a live verifier or codex. Config is read from the environment
at call time, so each test sets DANUS_* around a temp project dir.

Runs standalone (``python -m danus.gateway.tests.test_gateway``) and under pytest.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from danus.core import FactGraph, GlobalMemory, compute_fact_id
from danus.gateway import build_app, tools_for
from danus.gateway import server


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
            {"location": "proof", "issue": "mock rejection"}
        ]
        return {"output_schema_version": 2, "verification_status": "final",
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
):
    findings = []
    if status == "final" and verdict == "wrong":
        findings = [{"location": "proof", "issue": "mock rejection"}]
        repair_hints = repair_hints or "repair the mock gap"
    return {
        "output_schema_version": 2,
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


def test_fact_submit_reject_writes_nothing_but_traces():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("wrong", repair_hints="gap in step 2"):
        res = server.fact_submit(statement="bad", proof="hand-wave")
        assert res["accepted"] is False and res["repair_hints"] == "gap in step 2"
        fg = FactGraph(Path(d))
        assert fg.list() == []  # nothing written
        gm = GlobalMemory(Path(d))
        assert gm.read("verification")[-1]["verdict"] == "wrong"  # but traced


def test_fact_submit_returns_written_fact_when_trace_append_fails():
    original_append = GlobalMemory.append

    def fail_trace(self, *args, **kwargs):
        raise OSError("injected verification trace failure")

    GlobalMemory.append = fail_trace
    try:
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
            assert result["accepted"] is True and result["fact_id"]
            assert "injected verification trace failure" in result["trace_error"]
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
                "output_schema_version": 2,
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
            "output_schema_version": 2,
            "verification_status": "final",
            "verdict": "maybe",
            "needs_expanded_proofs": [],
            "verification_report": {},
            "repair_hints": "",
        },
        {
            "output_schema_version": 2,
            "verification_status": "final",
            "verdict": "correct",
            "needs_expanded_proofs": [],
            "verification_report": {
                "summary": "has gap", "critical_errors": [],
                "gaps": [{"location": "proof", "issue": "missing step"}],
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
        assert GlobalMemory(Path(d)).read("verification") == []


def test_fact_submit_rejects_old_service_without_context_attestation():
    """A rolling upgrade must not let an old service silently drop context."""
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        original = server._verify
        server._verify = lambda statement, proof, fact_context=None, glossary_introduces=None: {
            "output_schema_version": 2,
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
        assert GlobalMemory(Path(d)).read("verification") == []


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
                "output_schema_version": 2,
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
        assert "verification_context_changed" in result["write_error"]
        assert FactGraph(Path(d)).list() == []
        trace = GlobalMemory(Path(d)).read("verification")[-1]
        assert trace["verdict"] == "correct" and trace["write_error"]
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
    with _env(DANUS_AGENTS_ROOT=None, DANUS_PROJECT_DIR="/tmp/whatever"):
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
        with _env(DANUS_AGENTS_ROOT=root, DANUS_PROJECT_DIR=None, DANUS_AUTHOR="main_agent"):
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
