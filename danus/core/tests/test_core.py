"""Smoke tests for danus.core — the truth layer.

Exercises local memory, global memory (verifiable/evidence rule + status), and
the fact graph (content addressing + DAG + cascade revoke + external_refs). The
local->global->fact promotion is an *agent* behavior (prose); here we only drive
the data-structure calls the agent would make.

Runs standalone (``python -m danus.core.tests.test_core``) and under pytest.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from danus.core import (
    FactGraph,
    GlobalMemory,
    LocalMemory,
    clean_external_refs,
    compute_fact_id,
    parse_frontmatter,
    verification_context_digest,
)
from danus.core import glossary as _glossary
from danus.core import factgraph as _factgraph
from danus.core._util import append_jsonl, read_jsonl


def test_local_memory_edge_cases():
    with tempfile.TemporaryDirectory() as d:
        lm = LocalMemory(Path(d) / "worker")
        # a non-dict record is rejected
        try:
            lm.append("notes", "not a dict")  # type: ignore[arg-type]
            assert False, "should reject non-dict record"
        except ValueError:
            pass
        # appending to a brand-new channel registers it on the fly
        assert "scratch" not in lm.channels
        lm.append("scratch", {"x": 1})
        assert "scratch" in lm.channels
        assert lm.read("scratch")[0]["record"] == {"x": 1}


def test_global_memory_edge_cases():
    with tempfile.TemporaryDirectory() as d:
        gm = GlobalMemory(Path(d) / "p")
        # unknown kind is rejected
        try:
            gm.append("bogus_kind", claim="c", evidence="e", author="w")
            assert False, "should reject unknown kind"
        except ValueError:
            pass
        # invalid status is rejected
        try:
            gm.set_status("someid", "not-a-status")
            assert False, "should reject invalid status"
        except ValueError:
            pass
        # search: status fold-in + limit_per_kind + zero-score drop
        for i in range(3):
            gm.append("plan", claim=f"reduce to q>={i} case", evidence="", author="w")
        first = gm.read("plan")[0]["id"]
        gm.set_status(first, "supported")
        res = gm.search("reduce", kinds=["plan"], limit_per_kind=2)
        plan = res["results_by_kind"]["plan"]
        assert plan["count"] == 2  # limit_per_kind honored
        # the folded-in status appears on whichever ranked entry is `first`
        for hit in plan["results"]:
            if hit["entry"]["id"] == first:
                assert hit["entry"]["status"] == "supported"
        # a query matching nothing yields zero results (score<=0 break)
        assert gm.search("zzzquarkxyz", kinds=["plan"])["results_by_kind"]["plan"]["count"] == 0


def test_util_read_jsonl_missing_and_garbage():
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "nope.jsonl"
        assert read_jsonl(missing) == []  # missing file -> []
        garbage = Path(d) / "g.jsonl"
        garbage.write_text(
            '{"ok": 1}\n'          # valid dict
            "\n"                    # blank line skipped
            "not json at all\n"     # JSONDecodeError skipped
            "[1, 2, 3]\n"           # valid JSON but not a dict -> skipped
            '{"ok": 2}\n',
            encoding="utf-8",
        )
        rows = read_jsonl(garbage)
        assert rows == [{"ok": 1}, {"ok": 2}]  # only the well-formed dicts survive


def test_util_jsonl_concurrent_appends_are_complete():
    import threading

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "shared.jsonl"

        def append_worker(worker: int) -> None:
            for index in range(10):
                append_jsonl(
                    path,
                    {"worker": worker, "index": index, "payload": "x" * 4096},
                )

        threads = [
            threading.Thread(target=append_worker, args=(worker,))
            for worker in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
        records = read_jsonl(path)
        assert len(records) == 60
        assert {(record["worker"], record["index"]) for record in records} == {
            (worker, index) for worker in range(6) for index in range(10)
        }


def test_schema_clean_external_refs_extra_keys():
    # an entry with a key NOT in EXTERNAL_REF_KEYS: canonical keys first, extras
    # appended in sorted order (exercises the extra-key preserve branch)
    out = clean_external_refs([{"note": "z", "title": "T", "key": "K", "aardvark": 1}])
    assert out == [{"key": "K", "title": "T", "aardvark": 1, "note": "z"}]
    assert list(out[0]) == ["key", "title", "aardvark", "note"]


def test_glossary_flatten_and_undefined():
    # falsy -> {}
    assert _glossary.flatten(None) == {} and _glossary.flatten({}) == {}
    # nested {version, terms:{term:{definition, aliases}}} shape + flat shape
    nested = {"version": 1, "terms": {"S_M": {"definition": "a set", "aliases": ["SM"]}}}
    fl = _glossary.flatten(nested)
    assert fl["S_M"] == "a set" and fl["SM"] == "a set"  # alias inherits definition
    assert _glossary.flatten({"K_F": "canonical"}) == {"K_F": "canonical"}  # flat entry
    # undefined_symbols: a token whose base-form (sans arg list) is defined is OK.
    # "S_M(x)" is an interesting token; its base "S_M" is in `defined` -> not flagged.
    assert _glossary.undefined_symbols(
        statement="S_M(x) applied", proof="", defined={"S_M"}) == []
    # and if neither the token nor its base is defined, it IS flagged
    assert _glossary.undefined_symbols(
        statement="S_M(x) applied", proof="", defined=set()) == ["S_M(x)"]


def test_glossary_global_load_and_fallback():
    # the real packaged resource loads and flattens to a non-empty dict
    _glossary.global_glossary.cache_clear()
    real = _glossary.global_glossary()
    assert isinstance(real, dict)
    # missing resource -> _load_global_text returns None -> global_glossary() == {}
    orig = _glossary._load_global_text
    _glossary._load_global_text = lambda: None
    _glossary.global_glossary.cache_clear()
    try:
        assert _glossary.global_glossary() == {}
        assert _glossary.global_terms() == set()
    finally:
        _glossary._load_global_text = orig
        _glossary.global_glossary.cache_clear()
    # broken JSON in the resource -> JSONDecodeError -> {}
    _glossary._load_global_text = lambda: "{not: valid json"
    _glossary.global_glossary.cache_clear()
    try:
        assert _glossary.global_glossary() == {}
    finally:
        _glossary._load_global_text = orig
        _glossary.global_glossary.cache_clear()
    # confirm the real load path via importlib.resources returns text (not None)
    assert _glossary._load_global_text() is not None


def test_glossary_missing_resource_fallback():
    # point the loader at a package with no glossary resource -> None (the
    # FileNotFoundError/OSError branch of _load_global_text)
    import danus.core.glossary as g
    orig_res = g._GLOBAL_RESOURCE
    g._GLOBAL_RESOURCE = "does_not_exist_anywhere.json"
    try:
        assert g._load_global_text() is None
    finally:
        g._GLOBAL_RESOURCE = orig_res


def test_factgraph_edge_cases():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "proj")
        # intuition is serialized (## intuition block)
        fid = fg.add(problem_id="P", author="w", statement="A holds", proof="pf",
                     intuition="the key idea is X")
        assert "## intuition" in fg.get_raw(fid) and "the key idea is X" in fg.get_raw(fid)

        # search: `limit` cap is honored (three matching facts, limit=2)
        for s in ("B one", "B two", "B three"):
            fg.add(problem_id="P", author="w", statement=s, proof="about B")
        assert len(fg.search("B", limit=2)) == 2

        # glossary() with corrupt JSON on disk -> {} (never raises)
        fg.glossary_path.parent.mkdir(parents=True, exist_ok=True)
        fg.glossary_path.write_text("{not json", encoding="utf-8")
        assert fg.glossary() == {}
        try:
            fg.context([], predecessor_depth=None, glossary_texts=["Use Q_X."])
            assert False, "verification context must fail closed on a corrupt glossary"
        except ValueError as e:
            assert "glossary_integrity_error" in str(e)

        # revoke of an unknown fact_id raises
        try:
            fg.revoke("deadbeefdeadbeef", reason="nope")
            assert False, "should raise on unknown fact_id"
        except ValueError:
            pass


def test_factgraph_set_external_refs_edge_cases():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "proj")
        # unknown fact_id -> ValueError
        try:
            fg.set_external_refs("deadbeefdeadbeef", [])
            assert False, "should raise on unknown fact_id"
        except ValueError as e:
            assert "unknown fact_id" in str(e)

        # a fact whose file has NO external_refs line (legacy) -> the line is inserted
        fid = compute_fact_id(problem_id="P", predecessors=[], glossary_introduces={},
                              statement="L holds", proof="pf L")
        fg.facts_dir.mkdir(parents=True, exist_ok=True)
        legacy = (f"---\nfact_id: {fid}\nproblem_id: P\nauthor: w\n"
                  "predecessors: []\nglossary_introduces: {}\n---\n\n"
                  "## statement\nL holds\n\n## proof\npf L\n")
        fg._path(fid).write_text(legacy, encoding="utf-8")
        refs = [{"key": "K1", "title": "T1"}]
        assert fg.set_external_refs(fid, refs) == refs
        assert fg.external_refs(fid) == refs
        assert "external_refs:" in fg.get_raw(fid)

        # a malformed file (no frontmatter close) -> ValueError
        bad = compute_fact_id(problem_id="P", predecessors=[], glossary_introduces={},
                              statement="M", proof="p")
        fg._path(bad).write_text("---\nfact_id: x\nno close here\n", encoding="utf-8")
        try:
            fg.set_external_refs(bad, refs)
            assert False, "should raise on malformed frontmatter"
        except ValueError as e:
            assert "malformed" in str(e)


def test_parse_frontmatter_edge_cases():
    # external_refs with invalid JSON payload -> [] (JSONDecodeError branch)
    bad_refs = ("---\nfact_id: x\nproblem_id: P\nauthor: w\npredecessors: []\n"
                "glossary_introduces: {}\nexternal_refs: {not valid json\n---\n\n"
                "## statement\ns\n\n## proof\np\n")
    assert parse_frontmatter(bad_refs)["external_refs"] == []

    # a glossary block terminated by a NON-glossary, non-special line
    # (in_gloss stays True until a line fails _GLOSS_LINE_RE -> in_gloss=False)
    with_gloss = ("---\nfact_id: x\nproblem_id: P\nauthor: w\npredecessors: []\n"
                  "glossary_introduces:\n  X: a manifold\n"
                  "some_other_field: value\n"        # not a glossary line -> terminates block
                  "external_refs: []\n---\n\n"
                  "## statement\ns\n\n## proof\np\n")
    parsed = parse_frontmatter(with_gloss)
    assert parsed["glossary_introduces"] == {"X": "a manifold"}
    assert parsed["external_refs"] == []


def test_statement_of_helper():
    # Internal H2 markdown is content; only the reserved proof boundary ends it.
    raw = (
        "## statement\nA holds\n\n## Assumptions\nand more\n\n"
        "## proof\nirrelevant\n"
    )
    assert _factgraph.statement_of(raw) == "A holds ## Assumptions and more"


def test_local_memory():
    with tempfile.TemporaryDirectory() as d:
        lm = LocalMemory(Path(d) / "worker_high")
        lm.append("notes", {"thought": "try a Beatty-sequence decomposition"})
        lm.append("events", {"did": "searched arxiv for floor-sum bounds"})
        hits = lm.search("Beatty decomposition")
        assert hits["results_by_channel"]["notes"]["count"] == 1
        assert len(lm.read("events")) >= 2  # explicit event + auto breadcrumb


def test_global_memory():
    with tempfile.TemporaryDirectory() as d:
        gm = GlobalMemory(Path(d) / "project")

        # judgment (verifiable=false): no evidence required
        pid = gm.append("plan", claim="reduce to the q>=2 case", evidence="", author="worker_high")
        assert [e for e in gm.read("plan") if e["id"] == pid][0]["status"] == "open"

        # main-agent strategic guidance
        gm.append("master_guidance", claim="prioritize the symplectic-rank route",
                  evidence="pro: the rank obstruction is the crux", author="main_agent")

        # main-agent elaboration (judgment synthesis; verifiable=false, cited fact_ids in links)
        eid = gm.append("elaboration", claim="**Not solved.** Main blocker: rank obstruction",
                        evidence="## 0. Mathematical verdict\n**Not solved.** ...", author="main_agent",
                        links={"fact_ids": ["abc123"]})
        eentry = [e for e in gm.read("elaboration") if e["id"] == eid][0]
        assert eentry["status"] == "open" and eentry["links"]["fact_ids"] == ["abc123"]

        # verification trace (logged by fact_submit; verifiable=false, extra fields allowed)
        vid = gm.append("verification", claim="Lemma L fails for n=2", evidence="verdict: correct",
                        author="worker_xhigh", verdict="correct", fact_id="abc123")
        ventry = [e for e in gm.read("verification") if e["id"] == vid][0]
        assert ventry["verdict"] == "correct" and ventry["fact_id"] == "abc123"

        # verifiable kind with empty evidence is rejected
        try:
            gm.append("conclusion", claim="c", evidence="", author="w")
            assert False, "should require evidence"
        except ValueError:
            pass

        # a verifiable claim, then status transitions (agent-driven)
        gid = gm.append("counterexample", claim="Lemma L fails for n=2",
                        evidence="Take X=P^1; ... QED.", author="worker_xhigh")
        assert [e for e in gm.read("counterexample") if e["id"] == gid][0]["status"] == "unverified"
        gm.set_status(gid, "verified", fact_id="abc123")
        entry = [e for e in gm.read("counterexample") if e["id"] == gid][0]
        assert entry["status"] == "verified" and entry["fact_id"] == "abc123"


def test_factgraph():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "proj2")
        base = fg.add(problem_id="P", author="P_high", statement="A holds", proof="proof of A",
                      glossary_introduces={"X": "a complex manifold"})
        child = fg.add(problem_id="P", author="P_high", statement="B from A", proof="uses A",
                       predecessors=[base])
        grand = fg.add(problem_id="P", author="P_high", statement="C from B", proof="uses B",
                       predecessors=[child])

        # content addressing: same content (incl. glossary) -> same id
        assert base == compute_fact_id(problem_id="P", predecessors=[],
                                       glossary_introduces={"X": "a complex manifold"},
                                       statement="A holds", proof="proof of A")
        assert fg.predecessors(child) == [base]
        assert set(fg.descendants(base)) == {child, grand}
        assert "## statement" in fg.get_raw(base) and "## proof" in fg.get_raw(base)

        # derived fact index: BM25 search over fact bodies, rebuilt on demand
        hits = fg.search("B from A")
        assert hits and hits[0]["fact_id"] == child
        assert hits[0]["statement"] == "B from A"          # snippet is the ## statement body
        assert "proof" not in hits[0]                       # search stays summary-only
        assert all(h["score"] > 0 for h in hits)           # zero-score hits are dropped
        assert fg.search("nonexistent symplectic quark") == []

        # glossary: serialized in the node, merged into the project glossary, parsed back
        assert '"X": "a complex manifold"' in fg.get_raw(base)
        assert fg.glossary().get("X") == "a complex manifold"
        assert parse_frontmatter(fg.get_raw(base))["glossary_introduces"] == {"X": "a complex manifold"}

        # coverage check: a symbol defined in a predecessor is OK; an undefined one is flagged
        assert fg.undefined_symbols(statement="K_F equals zero", proof="by X",
                                    predecessors=[base], glossary_introduces={}) == ["K_F"]
        assert fg.undefined_symbols(statement="X is nice", proof="X is a manifold",
                                    predecessors=[base]) == []
        # global glossary: universal notation counts as defined everywhere (no project def needed)
        assert fg.undefined_symbols(statement="let epsilon in R+", proof="Z+ is nonempty") == []

        # cascade revoke + predecessor-revoked refusal
        revoked = fg.revoke(base, reason="A was wrong")
        assert set(revoked) == {base, child, grand}
        assert not fg.exists(base) and not fg.exists(child) and not fg.exists(grand)
        try:
            fg.add(
                problem_id="P",
                author="P_high",
                statement="A holds",
                proof="proof of A",
                glossary_introduces={"X": "a complex manifold"},
            )
            assert False, "a revoked content-addressed fact must not be resurrected"
        except ValueError as e:
            assert "fact_revoked" in str(e)
        assert not fg.exists(base) and fg._revoked_path(base).exists()
        try:
            fg.add(problem_id="P", author="P_high", statement="D from A", proof="uses A",
                   predecessors=[base])
            assert False, "should refuse revoked predecessor"
        except ValueError as e:
            assert "predecessor_revoked" in str(e)
        try:
            fg.add(problem_id="P", author="P_high", statement="phantom", proof="bad",
                   predecessors=["0000000000000000"])
            assert False, "should refuse an unknown predecessor"
        except ValueError as e:
            assert "predecessor_unknown" in str(e)
        live = fg.add(problem_id="P", author="P_high", statement="fresh", proof="proof")
        try:
            fg.add(problem_id="P", author="P_high", statement="duplicate edge", proof="bad",
                   predecessors=[live, live])
            assert False, "should refuse duplicate predecessor edges"
        except ValueError as e:
            assert "duplicate predecessor" in str(e)


def test_factgraph_lazy_context():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "context")
        base = fg.add(problem_id="P", author="w", statement="Base", proof="proof base")
        other = fg.add(problem_id="P", author="w", statement="Other", proof="proof other")
        child = fg.add(problem_id="P", author="w", statement="Child", proof="proof child",
                       predecessors=[base])
        root = fg.add(problem_id="P", author="w", statement="Root", proof="proof root",
                      predecessors=[child])
        unrelated = fg.add(problem_id="P", author="w", statement="Unrelated",
                           proof="must not be read")

        # Default is summary/relations only and depth zero.
        summary = fg.context([root])
        assert summary["facts"] == [{
            "fact_id": root, "statement": "Root", "predecessors": [child],
            "glossary_introduces": {},
        }]
        assert summary["complete"] is True and summary["truncated"] is False
        assert summary["schema_version"] == 1
        assert summary["scope"] == {
            "requested_fact_ids": [root],
            "predecessor_depth": 0,
            "proof_mode": "none",
            "include_project_glossary": True,
            "glossary_terms": [],
        }
        assert summary["glossary"] == {}
        assert summary["digest"].startswith("sha256:")
        assert "proof" not in summary["facts"][0]

        # Full closure is breadth-first with all requested roots before ancestors.
        reads = []
        original_get_raw = fg._get_raw_unchecked

        def recording_get_raw(fact_id):
            reads.append(fact_id)
            return original_get_raw(fact_id)

        fg._get_raw_unchecked = recording_get_raw  # type: ignore[assignment]
        try:
            closure = fg.context([root, other], predecessor_depth=None)
        finally:
            fg._get_raw_unchecked = original_get_raw  # type: ignore[assignment]
        expected_order = [root, other, child, base]
        assert [item["fact_id"] for item in closure["facts"]] == expected_order
        assert reads == expected_order and unrelated not in reads
        json.dumps(closure)  # the complete return envelope is JSON-safe

        # Selected hydrates only explicit roots; all hydrates the whole closure.
        selected = fg.context([root], predecessor_depth=None, proof_mode="selected")
        assert selected["facts"][0]["proof"] == "proof root"
        assert all("proof" not in item for item in selected["facts"][1:])
        hydrated = fg.context([root], predecessor_depth=None, proof_mode="all")
        assert [item["proof"] for item in hydrated["facts"]] == [
            "proof root", "proof child", "proof base",
        ]

        # A depth bound is complete for the requested scope, not a truncation.
        bounded = fg.context([root], predecessor_depth=1)
        assert [item["fact_id"] for item in bounded["facts"]] == [root, child]
        assert bounded["complete"] is True and bounded["truncated"] is False

        # Budgets charge whole records and stop at the first record that cannot fit.
        first_chars = len(json.dumps(
            closure["facts"][0], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ))
        budgeted = fg.context(
            [root, other], predecessor_depth=None, max_chars=first_chars
        )
        assert [item["fact_id"] for item in budgeted["facts"]] == [root]
        assert budgeted["omitted_fact_ids"] == [other, child, base]
        assert budgeted["characters_used"] == first_chars
        assert budgeted["character_budget"] == first_chars
        assert budgeted["truncated"] is True and budgeted["complete"] is False

        revoked = fg.add(problem_id="P", author="w", statement="Revoked", proof="bad")
        fg.revoke(revoked, reason="test")
        missing_id = "0000000000000000"
        unavailable = fg.context([missing_id, revoked], predecessor_depth=None)
        assert unavailable["missing_fact_ids"] == [missing_id]
        assert unavailable["revoked_fact_ids"] == [revoked]
        assert unavailable["complete"] is False and unavailable["facts"] == []

        removed = fg.add(problem_id="P", author="w", statement="Removed", proof="proof")
        dangling = fg.add(problem_id="P", author="w", statement="Dangling",
                          proof="uses missing", predecessors=[removed])
        (fg.facts_dir / f"{removed}.md").unlink()  # simulate a corrupt legacy graph
        transitive_missing = fg.context([dangling], predecessor_depth=None)
        assert [item["fact_id"] for item in transitive_missing["facts"]] == [dangling]
        assert transitive_missing["missing_fact_ids"] == [removed]
        assert transitive_missing["complete"] is False

        empty = fg.context([], predecessor_depth=None, proof_mode="selected", max_chars=0)
        assert empty["complete"] is True and empty["facts"] == []
        assert empty["characters_used"] == 0 and empty["omitted_fact_ids"] == []

        structured_proof = (
            "Opening argument.\n\n## Lemma 1\nEssential calculation.\n\n"
            "## Final step\nThe conclusion follows."
        )
        structured = fg.add(
            problem_id="P", author="w", statement="Structured", proof=structured_proof
        )
        structured_context = fg.context([structured], proof_mode="selected")
        assert structured_context["facts"][0]["proof"] == structured_proof

        # New files round-trip every API-valid glossary string and markdown H2
        # subsection. The final intuition heading remains a structural boundary,
        # while an identically named subsection inside the proof is preserved.
        rich_statement = "Claim body.\n\n## Assumptions\nAll hypotheses are explicit."
        rich_proof = (
            "Opening.\n\n## intuition\nThis heading belongs to the proof.\n\n"
            "## Final step\nDone."
        )
        rich_glossary = {"X:Y": "first line\nsecond: line"}
        rich = fg.add(
            problem_id="P",
            author="w",
            statement=rich_statement,
            proof=rich_proof,
            intuition="Separate non-hashed intuition.",
            glossary_introduces=rich_glossary,
        )
        rich_context = fg.context([rich], proof_mode="selected")
        assert rich_context["facts"][0]["statement"] == (
            "Claim body. ## Assumptions All hypotheses are explicit."
        )
        assert rich_context["facts"][0]["proof"] == rich_proof
        assert rich_context["facts"][0]["glossary_introduces"] == rich_glossary

        unicode_glossary = {"U_X": "first\u2028second\u2029third"}
        unicode_fact = fg.add(
            problem_id="P", author="w", statement="Unicode definition",
            proof="proof", glossary_introduces=unicode_glossary,
        )
        assert fg.context([unicode_fact])["facts"][0]["glossary_introduces"] == unicode_glossary

        before_invalid = set(fg.list())
        try:
            fg.add(
                problem_id="P", author="w", statement="Bad intuition boundary",
                proof="proof", intuition="first\n\n## intuition\nsecond",
            )
            assert False, "reserved intuition boundary must be rejected before write"
        except ValueError as e:
            assert "reserved '## intuition'" in str(e)
        assert set(fg.list()) == before_invalid

        # Project/global definitions are selected lazily from actual notation,
        # bound into the digest, and charged to the same whole-record budget.
        fg.add(
            problem_id="P",
            author="w",
            statement="Definition source",
            proof="definition proof",
            glossary_introduces={"Q_X": "a distinguished project object"},
        )
        glossary_context = fg.context(
            [], predecessor_depth=None, glossary_texts=["Apply Q_X now."]
        )
        assert glossary_context["facts"] == []
        assert glossary_context["glossary"] == {
            "Q_X": "a distinguished project object"
        }
        assert glossary_context["scope"]["glossary_terms"] == ["Q_X"]
        assert glossary_context["complete"] is True
        glossary_budget = fg.context(
            [], predecessor_depth=None, max_chars=1,
            glossary_texts=["Apply Q_X now."],
        )
        assert glossary_budget["facts"] == [] and glossary_budget["glossary"] == {}
        assert glossary_budget["omitted_glossary_terms"] == ["Q_X"]
        assert glossary_budget["complete"] is False and glossary_budget["truncated"] is True

        before_conflict = set(fg.list())
        try:
            fg.add(
                problem_id="P", author="w", statement="Conflicting definition",
                proof="proof", glossary_introduces={"Q_X": "a different object"},
            )
            assert False, "project glossary terms must be semantically stable"
        except ValueError as e:
            assert "glossary_conflict" in str(e)
        assert set(fg.list()) == before_conflict
        stable_context = fg.context(
            [], predecessor_depth=None, glossary_texts=["Apply Q_X now."]
        )
        assert stable_context["digest"] == glossary_context["digest"]
        assert stable_context["glossary"] == glossary_context["glossary"]

        try:
            fg.add(problem_id=" P ", author="w", statement="Bad id", proof="proof")
            assert False, "problem_id whitespace must be rejected before write"
        except ValueError as e:
            assert "problem_id" in str(e)
        assert set(fg.list()) == before_conflict

        tampered = fg.add(
            problem_id="P", author="w", statement="Tamper target", proof="original proof"
        )
        tampered_path = fg.facts_dir / f"{tampered}.md"
        tampered_path.write_text(
            tampered_path.read_text(encoding="utf-8").replace(
                "original proof", "silently changed proof"
            ),
            encoding="utf-8",
        )
        try:
            fg.context([tampered], proof_mode="selected")
            assert False, "tampered content-addressed fact must fail closed"
        except ValueError as e:
            assert "fact_integrity_error" in str(e)


def test_factgraph_lazy_context_deep_dag_is_iterative():
    """A dependency chain deeper than Python's recursion limit stays readable."""
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "deep-context")
        tip = None
        expected = []
        for index in range(1100):
            tip = fg.add(
                problem_id="P",
                author="w",
                statement=f"Fact {index}",
                proof=f"Proof {index}",
                predecessors=[tip] if tip else [],
            )
            expected.append(tip)

        context = fg.context(
            [tip], predecessor_depth=None, proof_mode="selected", max_chars=None
        )
        assert context["complete"] is True and context["truncated"] is False
        assert [record["fact_id"] for record in context["facts"]] == list(reversed(expected))
        assert "proof" in context["facts"][0]
        assert all("proof" not in record for record in context["facts"][1:])

        verification = fg.verification_context([tip], max_chars=None)
        assert verification["complete"] is True
        assert verification["scope"]["closure_fact_ids"] == list(reversed(expected))
        assert verification["expanded_proofs"] == []
        assert all("proof" not in record for record in verification["facts"])

        # Descendant discovery must use one active-file snapshot rather than
        # re-listing/re-reading the whole graph once per node (quadratic on this
        # 1,100-node chain).
        original_list = fg._list_unchecked
        list_calls = 0

        def counted_list():
            nonlocal list_calls
            list_calls += 1
            return original_list()

        fg._list_unchecked = counted_list  # type: ignore[assignment]
        try:
            assert fg.descendants(expected[0]) == expected[1:]
        finally:
            fg._list_unchecked = original_list  # type: ignore[assignment]
        assert list_calls == 1


def test_verification_context_carries_large_statement_closure_without_proofs():
    """Large closures carry every statement/edge/definition but no proof in round zero."""
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "compact-verification-context")
        tip = None
        ids = []
        for index in range(500):
            glossary = (
                {"K_{F}": "the ancestor-defined compact-set invariant"}
                if index == 0
                else {}
            )
            tip = fg.add(
                problem_id="P",
                author="w",
                statement=f"Fact {index}: " + "mathematical premise text " * 18,
                proof=f"Proof {index}",
                predecessors=[tip] if tip else [],
                glossary_introduces=glossary,
            )
            ids.append(tip)

        context = fg.verification_context(
            [tip],
            max_chars=1_000_000,
            # Deliberately use a notation variant that literal matching misses.
            glossary_texts=["Use K_F in the candidate."],
        )
        assert context["complete"] is True and context["truncated"] is False
        assert [record["fact_id"] for record in context["facts"]] == list(reversed(ids))
        assert context["scope"]["closure_fact_ids"] == list(reversed(ids))
        assert context["scope"]["expansion_round"] == 0
        assert context["scope"]["expanded_proof_ids"] == []
        assert context["expanded_proofs"] == []
        assert all("proof" not in record for record in context["facts"])
        assert context["facts"][-1]["glossary_introduces"] == {
            "K_{F}": "the ancestor-defined compact-set invariant"
        }
        serialized = json.dumps(context, ensure_ascii=False)
        assert "Fact 0:" in serialized


def test_adaptive_verification_context_diamond_deduplicates_and_binds_every_layer():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "adaptive-diamond")
        base = fg.add(
            problem_id="P", author="w", statement="Base",
            proof="base proof bytes", glossary_introduces={"B": "base object"},
        )
        left = fg.add(
            problem_id="P", author="w", statement="Left", proof="left proof",
            predecessors=[base],
        )
        right = fg.add(
            problem_id="P", author="w", statement="Right", proof="right proof",
            predecessors=[base],
        )
        root = fg.add(
            problem_id="P", author="w", statement="Root", proof="root proof",
            predecessors=[left, right],
        )

        reads = []
        original_get_raw = fg._get_raw_unchecked

        def counted_get_raw(fact_id):
            reads.append(fact_id)
            return original_get_raw(fact_id)

        fg._get_raw_unchecked = counted_get_raw  # type: ignore[assignment]
        try:
            first = fg.verification_context([root], max_chars=None)
        finally:
            fg._get_raw_unchecked = original_get_raw  # type: ignore[assignment]
        closure_order = [root, left, right, base]
        assert reads == closure_order
        assert first["scope"]["closure_fact_ids"] == closure_order
        assert [record["fact_id"] for record in first["facts"]] == closure_order
        assert len(first["facts"]) == len({record["fact_id"] for record in first["facts"]})
        assert first["expanded_proofs"] == []
        assert all("proof" not in record for record in first["facts"])

        second = fg.verification_context(
            [root],
            max_chars=None,
            expanded_proof_ids=[base, right],
            expansion_round=1,
            expanded_proof_max_chars=200000,
        )
        # Caller order cannot perturb attestation: expanded records follow the
        # authenticated closure order and every proof remains a whole record.
        assert second["scope"]["expanded_proof_ids"] == [right, base]
        assert second["expanded_proofs"] == [
            {"fact_id": right, "proof": "right proof"},
            {"fact_id": base, "proof": "base proof bytes"},
        ]
        assert second["digest"] != first["digest"]

        variants = []
        for mutate in (
            lambda value: value["facts"][0].__setitem__("statement", "Root!"),
            lambda value: value["facts"][0]["predecessors"].__setitem__(0, base),
            lambda value: value["facts"][-1]["glossary_introduces"].__setitem__(
                "B", "base object!"
            ),
            lambda value: value["scope"].__setitem__("expansion_round", 2),
            lambda value: value["scope"].__setitem__(
                "candidate_fact_id", "ffffffffffffffff"
            ),
            lambda value: value["expanded_proofs"][0].__setitem__(
                "proof", "right proof!"
            ),
            lambda value: value.__setitem__("characters_used", value["characters_used"] + 1),
        ):
            variant = json.loads(json.dumps(second))
            variant.pop("digest")
            mutate(variant)
            variants.append(verification_context_digest(context=variant))
        assert all(digest != second["digest"] for digest in variants)

        proof_record = {"fact_id": base, "proof": "base proof bytes"}
        record_chars = len(json.dumps(
            proof_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ))
        omitted = fg.verification_context(
            [root],
            max_chars=None,
            expanded_proof_ids=[base],
            expansion_round=1,
            expanded_proof_max_chars=record_chars - 1,
        )
        assert omitted["expanded_proofs"] == []
        assert omitted["omitted_expanded_proof_ids"] == [base]
        assert omitted["complete"] is False and omitted["truncated"] is True
        exact = fg.verification_context(
            [root],
            max_chars=None,
            expanded_proof_ids=[base],
            expansion_round=1,
            expanded_proof_max_chars=record_chars,
        )
        assert exact["expanded_proofs"] == [proof_record]
        assert exact["expanded_proof_characters"] == record_chars


def test_factgraph_public_snapshot_lock_linearizes_list_against_revoke():
    """A reader holding SH sees the whole pre-state; cascade waits for it."""
    import threading
    import time

    with tempfile.TemporaryDirectory() as d:
        graph_root = Path(d) / "read-snapshot-lock"
        fg = FactGraph(graph_root)
        root = fg.add(problem_id="P", author="w", statement="root", proof="proof")
        child = fg.add(
            problem_id="P", author="w", statement="child", proof="proof",
            predecessors=[root],
        )
        unrelated = fg.add(
            problem_id="P", author="w", statement="unrelated", proof="proof"
        )
        expected_before = sorted([root, child, unrelated])
        entered_read = threading.Event()
        release_read = threading.Event()
        results = {}
        original_list_unchecked = fg._list_unchecked

        def paused_list_unchecked():
            entered_read.set()
            assert release_read.wait(5)
            return original_list_unchecked()

        fg._list_unchecked = paused_list_unchecked  # type: ignore[assignment]

        reader = threading.Thread(
            target=lambda: results.setdefault("read", fg.list())
        )
        writer = threading.Thread(
            target=lambda: results.setdefault(
                "revoked", FactGraph(graph_root).revoke(root, reason="race")
            )
        )
        reader.start()
        assert entered_read.wait(5)
        writer.start()
        time.sleep(0.1)
        assert writer.is_alive(), "exclusive revoke must wait for the shared snapshot"
        release_read.set()
        reader.join(5)
        writer.join(5)
        fg._list_unchecked = original_list_unchecked  # type: ignore[assignment]

        assert not reader.is_alive() and not writer.is_alive()
        assert results["read"] == expected_before
        assert set(results["revoked"]) == {root, child}
        assert FactGraph(graph_root).list() == [unrelated]


def test_factgraph_descendants_reverse_adjacency_handles_branching_dag():
    """Branches and a shared child are traversed once from one graph snapshot."""
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "branching-descendants")
        root = fg.add(problem_id="P", author="w", statement="root", proof="proof")
        left = fg.add(
            problem_id="P", author="w", statement="left", proof="proof",
            predecessors=[root],
        )
        right = fg.add(
            problem_id="P", author="w", statement="right", proof="proof",
            predecessors=[root],
        )
        left_leaf = fg.add(
            problem_id="P", author="w", statement="left leaf", proof="proof",
            predecessors=[left],
        )
        right_leaf = fg.add(
            problem_id="P", author="w", statement="right leaf", proof="proof",
            predecessors=[right],
        )
        join = fg.add(
            problem_id="P", author="w", statement="join", proof="proof",
            predecessors=[left_leaf, right_leaf],
        )
        unrelated = fg.add(
            problem_id="P", author="w", statement="unrelated", proof="proof"
        )

        descendants = fg.descendants(root)
        assert set(descendants) == {left, right, left_leaf, right_leaf, join}
        assert len(descendants) == len(set(descendants))
        assert unrelated not in descendants


def test_revoke_rebuilds_project_glossary_from_active_facts():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "glossary-provenance")
        term = "PROVENANCE_X_321"
        source = fg.add(
            problem_id="P", author="w", statement="Definition source",
            proof="proof", glossary_introduces={term: "a fixed object"},
        )
        independent = fg.add(
            problem_id="P", author="w", statement=f"{term} has property A",
            proof=f"A self-contained proof mentioning {term}.",
        )
        verification_view = fg.context(
            [independent],
            predecessor_depth=None,
            proof_mode="none",
            glossary_texts=[f"Use {term}."],
            include_project_glossary=False,
        )
        assert verification_view["glossary"] == {}
        assert verification_view["scope"]["include_project_glossary"] is False
        dependent = fg.add(
            problem_id="P", author="w", statement=f"{term} has property A",
            proof=f"Use the definition of {term}.", predecessors=[source],
        )
        assert set(fg.revoke(source, reason="definition withdrawn")) == {
            source,
            dependent,
        }
        assert fg.exists(independent), "only declared DAG dependencies cascade"

    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "glossary-revoke")
        term = "REV_ONLY_X_987"
        definition = "an object introduced by an active verified fact"
        first = fg.add(
            problem_id="P",
            author="w",
            statement="First definition source",
            proof="proof one",
            glossary_introduces={term: definition},
        )
        second = fg.add(
            problem_id="P",
            author="w",
            statement="Second definition source",
            proof="proof two",
            glossary_introduces={term: definition},
        )

        fg.revoke(first, reason="first introducer withdrawn")
        assert fg.glossary()[term] == definition
        still_available = fg.context(
            [], predecessor_depth=None, proof_mode="none",
            glossary_texts=[f"Use {term}."],
        )
        assert still_available["complete"] is True
        assert still_available["glossary"] == {term: definition}

        fg.revoke(second, reason="last introducer withdrawn")
        assert term not in fg.glossary()
        removed = fg.context(
            [], predecessor_depth=None, proof_mode="none",
            glossary_texts=[f"Use {term}."],
        )
        assert removed["complete"] is True
        assert removed["glossary"] == {}

    # Crash ordering is conservative: the future glossary is committed before
    # any fact move, so even an injected move failure cannot leave a definition
    # authoritative after revocation has begun.
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "failed-revoke")
        term = "REV_CRASH_X_654"
        fact_id = fg.add(
            problem_id="P", author="w", statement="Definition source",
            proof="proof", glossary_introduces={term: "temporary definition"},
        )
        original_move = _factgraph.shutil.move

        def fail_move(*_args, **_kwargs):
            raise OSError("injected move failure")

        _factgraph.shutil.move = fail_move
        try:
            try:
                fg.revoke(fact_id, reason="injected failure")
                assert False, "injected move failure must propagate"
            except OSError as exc:
                assert "injected move failure" in str(exc)
        finally:
            _factgraph.shutil.move = original_move
        for reader in (
            lambda: fg.exists(fact_id),
            fg.list,
            fg.glossary,
            lambda: fg.context(
                [], predecessor_depth=None, proof_mode="none",
                glossary_texts=[f"Use {term}."],
            ),
            lambda: fg.search("Definition"),
        ):
            try:
                reader()
                assert False, "a pending cascade must make every truth read fail closed"
            except ValueError as exc:
                assert "fact_graph_recovery_required" in str(exc)

        # Retrying the original mutation resumes the journal idempotently.
        assert fg.revoke(fact_id, reason="retry may use a different message") == [fact_id]
        assert not fg.exists(fact_id)
        assert term not in fg.glossary()


def test_fact_add_is_atomic_and_rolls_back_glossary_failure():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "atomic-fact-write")
        original_replace = _factgraph.os.replace

        def fail_fact_replace(source, destination):
            if Path(destination).parent == fg.facts_dir:
                raise OSError("injected fact write failure")
            return original_replace(source, destination)

        _factgraph.os.replace = fail_fact_replace
        try:
            try:
                fg.add(
                    problem_id="P", author="w", statement="Never partial",
                    proof="proof",
                )
                assert False, "injected fact write failure must propagate"
            except OSError as exc:
                assert "injected fact write failure" in str(exc)
        finally:
            _factgraph.os.replace = original_replace
        assert fg.list() == []
        assert list(fg.facts_dir.glob(".*.tmp")) == []

    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "failed-add-glossary")
        term = "ADD_ROLLBACK_X_432"
        original_glossary_write = fg._write_project_glossary_atomic

        def fail_glossary_write(_glossary):
            raise OSError("injected glossary write failure")

        fg._write_project_glossary_atomic = fail_glossary_write  # type: ignore[assignment]
        try:
            try:
                fg.add(
                    problem_id="P", author="w", statement="Definition candidate",
                    proof="proof", glossary_introduces={term: "a test object"},
                )
                assert False, "injected glossary failure must propagate"
            except OSError as exc:
                assert "injected glossary write failure" in str(exc)
        finally:
            fg._write_project_glossary_atomic = original_glossary_write  # type: ignore[assignment]

        assert fg.list() == []
        assert fg.glossary() == {}
        assert not fg.pending_add_path.exists()

    # If even the immediate rollback hits I/O failure, the journal remains and
    # blocks every read until a later mutation can restore the pre-add snapshot.
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "recover-add-rollback")
        term = "ADD_PENDING_X_765"
        original_glossary_write = fg._write_project_glossary_atomic
        original_unlink = fg._unlink_durable

        def fail_glossary_write(_glossary):
            raise OSError("injected glossary write failure")

        def fail_fact_rollback(path):
            if path.parent == fg.facts_dir:
                raise OSError("injected rollback failure")
            return original_unlink(path)

        fg._write_project_glossary_atomic = fail_glossary_write  # type: ignore[assignment]
        fg._unlink_durable = fail_fact_rollback  # type: ignore[assignment]
        try:
            try:
                fg.add(
                    problem_id="P", author="w", statement="Pending definition",
                    proof="proof", glossary_introduces={term: "a pending object"},
                )
                assert False, "failed rollback must surface a recovery error"
            except RuntimeError as exc:
                assert "fact_graph_recovery_required" in str(exc)
        finally:
            fg._write_project_glossary_atomic = original_glossary_write  # type: ignore[assignment]
            fg._unlink_durable = original_unlink  # type: ignore[assignment]
        assert fg.pending_add_path.exists()
        try:
            fg.list()
            assert False, "the unrolled add must not be exposed"
        except ValueError as exc:
            assert "fact_graph_recovery_required" in str(exc)

        survivor = fg.add(
            problem_id="P", author="w", statement="Independent", proof="proof"
        )
        assert fg.list() == [survivor]
        assert fg.glossary() == {}
        assert not fg.pending_add_path.exists()


def test_revoke_journal_recovers_second_move_and_log_failures_once():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "second-move")
        root = fg.add(problem_id="P", author="w", statement="root", proof="proof")
        child = fg.add(
            problem_id="P", author="w", statement="child", proof="proof",
            predecessors=[root],
        )
        leaf = fg.add(
            problem_id="P", author="w", statement="leaf", proof="proof",
            predecessors=[child],
        )
        original_move = _factgraph.shutil.move
        move_count = 0

        def fail_second_move(source, destination):
            nonlocal move_count
            move_count += 1
            if move_count == 2:
                raise OSError("injected second move failure")
            return original_move(source, destination)

        _factgraph.shutil.move = fail_second_move
        try:
            try:
                fg.revoke(root, reason="cascade")
                assert False, "the second move failure must propagate"
            except OSError as exc:
                assert "injected second move failure" in str(exc)
        finally:
            _factgraph.shutil.move = original_move
        try:
            fg.list()
            assert False, "a partially moved cascade must be hidden"
        except ValueError as exc:
            assert "fact_graph_recovery_required" in str(exc)

        # Any later mutation resumes the old cascade before changing the graph.
        survivor = fg.add(
            problem_id="P", author="w", statement="new independent fact", proof="proof"
        )
        assert fg.list() == [survivor]
        assert all(fg._revoked_path(item).exists() for item in (root, child, leaf))
        logged = read_jsonl(fg.revocation_log)
        assert sorted(entry["fact_id"] for entry in logged) == sorted([root, child, leaf])

    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "log-failure")
        root = fg.add(problem_id="P", author="w", statement="root", proof="proof")
        child = fg.add(
            problem_id="P", author="w", statement="child", proof="proof",
            predecessors=[root],
        )
        original_log_write = fg._write_revocation_log_atomic

        def commit_log_then_fail(pending):
            original_log_write(pending)
            raise OSError("injected post-log failure")

        fg._write_revocation_log_atomic = commit_log_then_fail  # type: ignore[assignment]
        try:
            try:
                fg.revoke(root, reason="bad theorem")
                assert False, "post-log failure must propagate"
            except OSError as exc:
                assert "injected post-log failure" in str(exc)
        finally:
            fg._write_revocation_log_atomic = original_log_write  # type: ignore[assignment]
        assert fg.pending_revocation_path.exists()
        assert fg.revoke(root, reason="retry") == [root, child]
        logged = read_jsonl(fg.revocation_log)
        assert [entry["fact_id"] for entry in logged].count(root) == 1
        assert [entry["fact_id"] for entry in logged].count(child) == 1
        assert not fg.pending_revocation_path.exists()


def test_project_glossary_cannot_change_global_semantics():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "global-shadow")
        term, definition = next(iter(_glossary.global_glossary().items()))
        try:
            fg.add(
                problem_id="P", author="w", statement="Conflicting global alias",
                proof="proof",
                glossary_introduces={term: definition + " (different meaning)"},
            )
            assert False, "project glossary must not shadow a global definition"
        except ValueError as exc:
            assert "glossary_conflict" in str(exc)


def test_factgraph_mutation_lock_serializes_add_and_revoke():
    """A revoke started during atomic compare+add must include the new child."""
    import threading
    import time

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "locked-graph"
        fg = FactGraph(root)
        predecessor = fg.add(
            problem_id="P", author="w", statement="A", proof="proof A"
        )
        snapshot = fg.context(
            [predecessor], predecessor_depth=None, proof_mode="none", max_chars=None
        )
        entered_add = threading.Event()
        release_add = threading.Event()
        result = {}
        original_add_unlocked = fg._add_unlocked

        def paused_add_unlocked(**kwargs):
            entered_add.set()
            assert release_add.wait(5)
            return original_add_unlocked(**kwargs)

        fg._add_unlocked = paused_add_unlocked  # type: ignore[assignment]

        def add_child():
            result["child"] = fg.add_if_context_unchanged(
                expected_context=snapshot,
                context_max_chars=None,
                problem_id="P",
                author="w",
                statement="B",
                proof="proof B",
                predecessors=[predecessor],
            )

        def revoke_parent():
            result["revoked"] = FactGraph(root).revoke(predecessor, reason="race")

        add_thread = threading.Thread(target=add_child)
        revoke_thread = threading.Thread(target=revoke_parent)
        add_thread.start()
        assert entered_add.wait(5)
        revoke_thread.start()
        time.sleep(0.1)
        assert revoke_thread.is_alive(), "revoke must wait for the add transaction lock"
        release_add.set()
        add_thread.join(5)
        revoke_thread.join(5)
        fg._add_unlocked = original_add_unlocked  # type: ignore[assignment]

        assert not add_thread.is_alive() and not revoke_thread.is_alive()
        assert set(result["revoked"]) == {predecessor, result["child"]}
        assert FactGraph(root).list() == []


def test_external_refs():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "proj3")
        refs = [{"key": "HL26", "authors": ["Han", "Liu"], "title": "On X",
                 "arxiv": "2603.03817", "year": 2026, "cited_for": "Theorem 1.2"}]

        # 1) BACKWARD COMPAT (load-bearing): external_refs is NOT hashed, so adding
        #    refs does not change the fact_id, and the id equals the bare compute_fact_id.
        bare = compute_fact_id(problem_id="P", predecessors=[], glossary_introduces={},
                               statement="A holds", proof="proof of A")
        fid_a = fg.add(problem_id="P", author="w", statement="A holds", proof="proof of A",
                       external_refs=refs)
        assert fid_a == bare, "external_refs must not change the fact_id"

        # 2) refs round-trip through serialize/parse and the read helper
        assert fg.external_refs(fid_a) == refs
        assert parse_frontmatter(fg.get_raw(fid_a))["external_refs"] == refs
        assert "external_refs:" in fg.get_raw(fid_a)

        # 3) same content + no refs => SAME id (dedup); re-adding is idempotent
        fid_a2 = fg.add(problem_id="P", author="w", statement="A holds", proof="proof of A")
        assert fid_a2 == fid_a

        # 4) a fact written without refs reads back as [] (and old-format files too)
        fid_b = fg.add(problem_id="P", author="w", statement="B holds", proof="proof of B")
        assert fg.external_refs(fid_b) == []
        legacy = ("---\nfact_id: deadbeefdeadbeef\nproblem_id: P\nauthor: w\n"
                  "predecessors: []\nglossary_introduces: {}\n---\n\n## statement\nx\n\n## proof\ny\n")
        assert parse_frontmatter(legacy)["external_refs"] == []   # no field -> default []

        # 5) set_external_refs (the auditor's path): rewrites refs, preserves id + body
        body_before = fg.get_raw(fid_b).split("## statement", 1)[1]
        out = fg.set_external_refs(fid_b, refs)
        assert out == refs and fg.external_refs(fid_b) == refs
        assert fg.exists(fid_b)                                            # id/file unchanged
        assert fg.get_raw(fid_b).split("## statement", 1)[1] == body_before  # body untouched

        # 6) normalization: non-dict entries dropped, canonical key order, [] for empty
        assert clean_external_refs([{"title": "T", "key": "K"}, "junk", 7]) == [{"key": "K", "title": "T"}]
        assert clean_external_refs(None) == [] and clean_external_refs([]) == []


def main() -> None:
    test_local_memory()
    print("  [ok] local memory append/search")
    test_local_memory_edge_cases()
    print("  [ok] local memory: non-dict record rejected + new-channel registration")
    test_global_memory()
    print("  [ok] global memory append/status/search + evidence rule")
    test_global_memory_edge_cases()
    print("  [ok] global memory: unknown kind / bad status / search limit+fold-in")
    test_util_read_jsonl_missing_and_garbage()
    print("  [ok] _util.read_jsonl: missing file + garbage/non-dict lines skipped")
    test_schema_clean_external_refs_extra_keys()
    print("  [ok] schema.clean_external_refs: extra keys preserved (sorted)")
    test_glossary_flatten_and_undefined()
    print("  [ok] glossary flatten (nested+flat) + undefined base-form")
    test_glossary_global_load_and_fallback()
    print("  [ok] glossary global load + missing/broken resource fallbacks")
    test_glossary_missing_resource_fallback()
    print("  [ok] glossary _load_global_text missing-resource -> None")
    test_factgraph()
    print("  [ok] fact graph content-addressing + DAG + cascade revoke")
    test_factgraph_lazy_context()
    print("  [ok] fact graph lazy context + hydration + completeness + budget")
    test_factgraph_lazy_context_deep_dag_is_iterative()
    print("  [ok] fact graph lazy context handles a 1100-node dependency chain")
    test_factgraph_descendants_reverse_adjacency_handles_branching_dag()
    print("  [ok] fact graph descendants handles branching/shared-child DAGs")
    test_factgraph_mutation_lock_serializes_add_and_revoke()
    print("  [ok] fact graph mutation lock serializes compare+add against revoke")
    test_factgraph_edge_cases()
    print("  [ok] fact graph: intuition/search-limit/corrupt-glossary/revoke-unknown")
    test_factgraph_set_external_refs_edge_cases()
    print("  [ok] fact graph set_external_refs: unknown/legacy-insert/malformed")
    test_parse_frontmatter_edge_cases()
    print("  [ok] parse_frontmatter: bad external_refs JSON + glossary terminator")
    test_statement_of_helper()
    print("  [ok] statement_of preserves internal headings and stops at proof")
    test_external_refs()
    print("  [ok] external_refs: not hashed (backward compat) + round-trip + auditor rewrite")
    print("ALL CORE TESTS PASSED")


if __name__ == "__main__":
    main()
