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

import pytest

from danus.core import (
    FactGraph,
    FactPromotionOutcomeUnknown,
    GlobalMemory,
    LocalMemory,
    clean_external_refs,
    compute_fact_id,
    fact_identity_from_verification_context,
    parse_frontmatter,
    verification_context_digest,
)
from danus.core import glossary as _glossary
from danus.core import factgraph as _factgraph
from danus.core import global_memory as _global_memory
from danus.core import schema as _schema
from danus.core._util import append_jsonl, read_jsonl


def _browser_consult_provenance_v1() -> dict[str, object]:
    return {
        "schema_version": 1,
        "transport": "chatgpt_pro_browser",
        "request_id": "1" * 16,
        "elaboration_id": "2" * 16,
        "context_id": "durable-context-1",
        "recommendation_id": "3" * 16,
        "binding_sha256": "4" * 64,
        "receipt_sha256": "5" * 64,
        "prompt_sha256": "6" * 64,
        "reply_sha256": "7" * 64,
        "adopted_strategy_sha256": "8" * 64,
        "trust": "adopted_strategy",
        "billing_basis": "subscription",
        "model": None,
        "ui_mode": "Pro",
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }


def _reject_consult_provenance(value: object) -> None:
    try:
        _schema.clean_consult_provenance(value)
        assert False, "invalid consult provenance must be rejected"
    except ValueError:
        pass


def test_consult_provenance_schema1_shape_is_unchanged():
    browser = _browser_consult_provenance_v1()
    assert _schema.clean_consult_provenance(browser) == browser

    metered = {
        "schema_version": 1,
        "transport": "gpt_pro",
        "trust": "provider_response",
        "billing_basis": "metered_api",
        "model": "gpt-pro",
        "ui_mode": None,
        "input_tokens": 10,
        "output_tokens": 20,
        "cost_usd": 3,
    }
    assert _schema.clean_consult_provenance(metered) == {
        "schema_version": 1,
        "transport": "gpt_pro",
        "request_id": None,
        "elaboration_id": None,
        "context_id": None,
        "recommendation_id": None,
        "binding_sha256": None,
        "receipt_sha256": None,
        "prompt_sha256": None,
        "reply_sha256": None,
        "adopted_strategy_sha256": None,
        "trust": "provider_response",
        "billing_basis": "metered_api",
        "model": "gpt-pro",
        "ui_mode": None,
        "input_tokens": 10,
        "output_tokens": 20,
        "cost_usd": 3.0,
    }

    for checkpoint_field, checkpoint_value in (
        ("checkpoint_id", "9" * 16),
        ("checkpoint_sha256", "a" * 64),
        ("checkpoint_bytes", 1),
    ):
        _reject_consult_provenance({**browser, checkpoint_field: checkpoint_value})


def test_consult_provenance_schema2_exact_checkpoint_projection():
    schema2 = {
        **_browser_consult_provenance_v1(),
        "schema_version": 2,
        "checkpoint_id": "9" * 16,
        "checkpoint_sha256": "a" * 64,
        "checkpoint_bytes": 32 * 1024,
    }
    cleaned = _schema.clean_consult_provenance(schema2)
    assert cleaned == schema2
    assert set(cleaned) == set(schema2)


def test_consult_provenance_schema2_rejects_partial_unknown_and_wrong_transport():
    schema2 = {
        **_browser_consult_provenance_v1(),
        "schema_version": 2,
        "checkpoint_id": "9" * 16,
        "checkpoint_sha256": "a" * 64,
        "checkpoint_bytes": 1024,
    }
    for checkpoint_field in (
        "checkpoint_id",
        "checkpoint_sha256",
        "checkpoint_bytes",
    ):
        _reject_consult_provenance(
            {key: item for key, item in schema2.items() if key != checkpoint_field}
        )
    _reject_consult_provenance({**schema2, "checkpoint_encoding": "utf-8"})
    _reject_consult_provenance({**schema2, "transport": "gpt_pro"})


def test_consult_provenance_schema2_validates_checkpoint_identity_and_size():
    schema2 = {
        **_browser_consult_provenance_v1(),
        "schema_version": 2,
        "checkpoint_id": "9" * 16,
        "checkpoint_sha256": "a" * 64,
        "checkpoint_bytes": 1,
    }
    for invalid_id in ("9" * 15, "9" * 17, "A" * 16, "g" * 16, None, 9):
        _reject_consult_provenance({**schema2, "checkpoint_id": invalid_id})
    for invalid_sha256 in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, None, 1):
        _reject_consult_provenance({**schema2, "checkpoint_sha256": invalid_sha256})
    for invalid_bytes in (0, -1, 32 * 1024 + 1, True, 1.0, None):
        _reject_consult_provenance({**schema2, "checkpoint_bytes": invalid_bytes})


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
        assert (
            gm.search("zzzquarkxyz", kinds=["plan"])["results_by_kind"]["plan"]["count"]
            == 0
        )


def test_global_memory_exact_get_is_bounded_and_unambiguous():
    with tempfile.TemporaryDirectory() as d:
        gm = GlobalMemory(Path(d) / "p")
        entry_id = gm.append(
            "obstacle", claim="exact obstruction", evidence="bounded", author="critic"
        )
        gm.set_status(entry_id, "supported", fact_id="0123456789abcdef")
        entry = gm.get(entry_id)
        assert entry["id"] == entry_id
        assert entry["kind"] == "obstacle"
        assert entry["status"] == "supported"
        assert entry["fact_id"] == "0123456789abcdef"

        for invalid in ("0" * 15, "0" * 17, "A" * 16, "g" * 16, None):
            try:
                gm.get(invalid)  # type: ignore[arg-type]
                assert False, "invalid exact-lookup id must be rejected"
            except ValueError:
                pass
        try:
            gm.get("f" * 16)
            assert False, "unknown exact-lookup id must be rejected"
        except ValueError as exc:
            assert "unknown" in str(exc)

    with tempfile.TemporaryDirectory() as d:
        gm = GlobalMemory(Path(d) / "p")
        duplicate_id = "1" * 16
        for kind in ("plan", "obstacle"):
            append_jsonl(
                gm._path(kind),
                {"id": duplicate_id, "kind": kind, "claim": kind, "evidence": ""},
            )
        try:
            gm.get(duplicate_id)
            assert False, "duplicate ids across kinds must fail closed"
        except ValueError as exc:
            assert "duplicate" in str(exc)

    with tempfile.TemporaryDirectory() as d:
        gm = GlobalMemory(Path(d) / "p")
        oversized_id = gm.append(
            "plan", claim="oversized", evidence="x" * (16 * 1024), author="worker"
        )
        try:
            gm.get(oversized_id)
            assert False, "oversized exact hydration must fail closed"
        except ValueError as exc:
            assert "serialized byte limit" in str(exc)


def test_global_memory_strict_reader_bounds_before_decode_or_json_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gm = GlobalMemory(tmp_path / "project")
    path = gm._path("advisor_checkpoint")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{" + b"x" * _global_memory.GM_IMMUTABLE_MAX_PHYSICAL_LINE_BYTES)
    calls = {"loads": 0}

    def forbidden_loads(_value):
        calls["loads"] += 1
        raise AssertionError("oversized physical lines must be rejected before JSON")

    monkeypatch.setattr(_global_memory.json, "loads", forbidden_loads)
    with pytest.raises(ValueError, match="physical byte limit"):
        gm.read_immutable("advisor_checkpoint")
    assert calls == {"loads": 0}


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b'{"id":"1111111111111111"}', "torn"),
        (b"\xff\n", "UTF-8"),
        (b"not-json\n", "malformed"),
    ],
)
def test_global_memory_strict_reader_rejects_torn_utf8_and_json(
    tmp_path: Path, raw: bytes, error: str
):
    gm = GlobalMemory(tmp_path / "project")
    path = gm._path("advisor_checkpoint")
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=error):
        gm.read_immutable("advisor_checkpoint")


def test_checkpoint_scoped_immutable_lookup_is_bounded_and_channel_local(
    tmp_path: Path,
):
    gm = GlobalMemory(tmp_path / "project")
    checkpoint_id = "1" * 16
    record = {
        "id": checkpoint_id,
        "kind": "advisor_checkpoint",
        "claim": "bounded checkpoint",
        "evidence": "x" * 31_000,
    }
    assert len(_global_memory.canonical_global_memory_record(record)) <= 32 * 1024
    append_jsonl(gm._path("advisor_checkpoint"), record)
    physical = gm._path("advisor_checkpoint").read_bytes()
    assert len(physical) <= _global_memory.GM_IMMUTABLE_MAX_PHYSICAL_LINE_BYTES

    unrelated = gm._path("plan")
    unrelated.write_bytes(
        b"{" + b"y" * _global_memory.GM_IMMUTABLE_MAX_PHYSICAL_LINE_BYTES
    )
    assert gm.get_immutable_in_kind("advisor_checkpoint", checkpoint_id) == record
    with pytest.raises(ValueError, match="physical byte limit"):
        gm.get_immutable(checkpoint_id)

    append_jsonl(gm._path("advisor_checkpoint"), record)
    with pytest.raises(ValueError, match="duplicate"):
        gm.get_immutable_in_kind("advisor_checkpoint", checkpoint_id)


@pytest.mark.parametrize(
    "invalid",
    [
        b'{"id":"0000000000000000","value":NaN}\n',
        b'{"id":"0000000000000000","value":Infinity}\n',
        b'{"id":"0000000000000000","value":-Infinity}\n',
        b'{"id":"0000000000000000","value":1e999}\n',
        b'{"id":"0000000000000000","id":"2222222222222222"}\n',
        b'{"id":"0000000000000000","nested":{"x":1,"x":2}}\n',
        (b'{"id":"0000000000000000","padding":"' + b"z" * (32 * 1024) + b'"}\n'),
    ],
)
def test_checkpoint_scoped_lookup_rejects_nonmatching_noncanonical_rows(
    tmp_path: Path, invalid: bytes
):
    gm = GlobalMemory(tmp_path / "project")
    target = {"id": "1" * 16, "kind": "advisor_checkpoint"}
    path = gm._path("advisor_checkpoint")
    path.parent.mkdir(parents=True)
    path.write_bytes(
        invalid + json.dumps(target, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    with pytest.raises(ValueError):
        gm.get_immutable_in_kind("advisor_checkpoint", target["id"])


def test_util_read_jsonl_missing_and_garbage():
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "nope.jsonl"
        assert read_jsonl(missing) == []  # missing file -> []
        garbage = Path(d) / "g.jsonl"
        garbage.write_text(
            '{"ok": 1}\n'  # valid dict
            "\n"  # blank line skipped
            "not json at all\n"  # JSONDecodeError skipped
            "[1, 2, 3]\n"  # valid JSON but not a dict -> skipped
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
    nested = {
        "version": 1,
        "terms": {"S_M": {"definition": "a set", "aliases": ["SM"]}},
    }
    fl = _glossary.flatten(nested)
    assert fl["S_M"] == "a set" and fl["SM"] == "a set"  # alias inherits definition
    assert _glossary.flatten({"K_F": "canonical"}) == {"K_F": "canonical"}  # flat entry
    # undefined_symbols: a token whose base-form (sans arg list) is defined is OK.
    # "S_M(x)" is an interesting token; its base "S_M" is in `defined` -> not flagged.
    assert (
        _glossary.undefined_symbols(
            statement="S_M(x) applied", proof="", defined={"S_M"}
        )
        == []
    )
    # and if neither the token nor its base is defined, it IS flagged
    assert _glossary.undefined_symbols(
        statement="S_M(x) applied", proof="", defined=set()
    ) == ["S_M(x)"]


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
        fid = fg.add(
            problem_id="P",
            author="w",
            statement="A holds",
            proof="pf",
            intuition="the key idea is X",
        )
        assert "## intuition" in fg.get_raw(fid) and "the key idea is X" in fg.get_raw(
            fid
        )

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
        fid = compute_fact_id(
            problem_id="P",
            predecessors=[],
            glossary_introduces={},
            statement="L holds",
            proof="pf L",
        )
        fg.facts_dir.mkdir(parents=True, exist_ok=True)
        legacy = (
            f"---\nfact_id: {fid}\nproblem_id: P\nauthor: w\n"
            "predecessors: []\nglossary_introduces: {}\n---\n\n"
            "## statement\nL holds\n\n## proof\npf L\n"
        )
        fg._path(fid).write_text(legacy, encoding="utf-8")
        refs = [{"key": "K1", "title": "T1"}]
        assert fg.set_external_refs(fid, refs) == refs
        assert fg.external_refs(fid) == refs
        assert "external_refs:" in fg.get_raw(fid)

        # a malformed file (no frontmatter close) -> ValueError
        bad = compute_fact_id(
            problem_id="P",
            predecessors=[],
            glossary_introduces={},
            statement="M",
            proof="p",
        )
        fg._path(bad).write_text("---\nfact_id: x\nno close here\n", encoding="utf-8")
        try:
            fg.set_external_refs(bad, refs)
            assert False, "should raise on malformed frontmatter"
        except ValueError as e:
            assert "malformed" in str(e)


def test_parse_frontmatter_edge_cases():
    # external_refs with invalid JSON payload -> [] (JSONDecodeError branch)
    bad_refs = (
        "---\nfact_id: x\nproblem_id: P\nauthor: w\npredecessors: []\n"
        "glossary_introduces: {}\nexternal_refs: {not valid json\n---\n\n"
        "## statement\ns\n\n## proof\np\n"
    )
    assert parse_frontmatter(bad_refs)["external_refs"] == []

    # a glossary block terminated by a NON-glossary, non-special line
    # (in_gloss stays True until a line fails _GLOSS_LINE_RE -> in_gloss=False)
    with_gloss = (
        "---\nfact_id: x\nproblem_id: P\nauthor: w\npredecessors: []\n"
        "glossary_introduces:\n  X: a manifold\n"
        "some_other_field: value\n"  # not a glossary line -> terminates block
        "external_refs: []\n---\n\n"
        "## statement\ns\n\n## proof\np\n"
    )
    parsed = parse_frontmatter(with_gloss)
    assert parsed["glossary_introduces"] == {"X": "a manifold"}
    assert parsed["external_refs"] == []


def test_statement_of_helper():
    # Internal H2 markdown is content; only the reserved proof boundary ends it.
    raw = "## statement\nA holds\n\n## Assumptions\nand more\n\n## proof\nirrelevant\n"
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
        pid = gm.append(
            "plan", claim="reduce to the q>=2 case", evidence="", author="worker_high"
        )
        assert [e for e in gm.read("plan") if e["id"] == pid][0]["status"] == "open"

        # main-agent strategic guidance
        gm.append(
            "master_guidance",
            claim="prioritize the symplectic-rank route",
            evidence="pro: the rank obstruction is the crux",
            author="main_agent",
        )

        # main-agent elaboration (judgment synthesis; verifiable=false, cited fact_ids in links)
        eid = gm.append(
            "elaboration",
            claim="**Not solved.** Main blocker: rank obstruction",
            evidence="## 0. Mathematical verdict\n**Not solved.** ...",
            author="main_agent",
            links={"fact_ids": ["abc123"]},
        )
        eentry = [e for e in gm.read("elaboration") if e["id"] == eid][0]
        assert eentry["status"] == "open" and eentry["links"]["fact_ids"] == ["abc123"]

        checkpoint = (
            "## Verified facts\n"
            "- `0123456789abcdef`: a verified reduction.\n\n"
            "## Failed routes and evidence\n"
            "- Route A conflicts with counterexample entry `deadbeef`.\n\n"
            "## Unresolved bottleneck\n"
            "No continuum-safe atomization is known.\n\n"
            "## Candidate decision question\n"
            "Which of two remaining routes should the workers prioritize?"
        )
        checkpoint_id = gm.append(
            "advisor_checkpoint",
            claim="Late advisor checkpoint: continuum-safe atomization is blocked",
            evidence=checkpoint,
            author="main_agent",
            links={"fact_ids": ["0123456789abcdef"]},
        )
        checkpoint_entry = [
            entry
            for entry in gm.read("advisor_checkpoint")
            if entry["id"] == checkpoint_id
        ][0]
        assert checkpoint_entry["verifiable"] is False
        assert checkpoint_entry["links"]["fact_ids"] == ["0123456789abcdef"]

        for invalid_evidence in (
            checkpoint.replace("## Candidate decision question", "## Missing"),
            checkpoint.replace("## Verified facts", "## TEMP")
            .replace("## Failed routes and evidence", "## Verified facts")
            .replace("## TEMP", "## Failed routes and evidence"),
            checkpoint + "x" * (16 * 1024),
        ):
            try:
                gm.append(
                    "advisor_checkpoint",
                    claim="invalid checkpoint",
                    evidence=invalid_evidence,
                    author="main_agent",
                    links={"fact_ids": []},
                )
                assert False, "should reject malformed/unbounded advisor checkpoint"
            except ValueError:
                pass
        try:
            gm.append(
                "advisor_checkpoint",
                claim="too many fact ids",
                evidence=checkpoint,
                author="main_agent",
                links={"fact_ids": [f"{index:016x}" for index in range(13)]},
            )
            assert False, "should cap an advisor checkpoint at 12 verified fact ids"
        except ValueError:
            pass

        # verification trace (logged by fact_submit; verifiable=false, extra fields allowed)
        vid = gm.append(
            "verification",
            claim="Lemma L fails for n=2",
            evidence="verdict: correct",
            author="worker_xhigh",
            verdict="correct",
            fact_id="abc123",
        )
        ventry = [e for e in gm.read("verification") if e["id"] == vid][0]
        assert ventry["verdict"] == "correct" and ventry["fact_id"] == "abc123"

        # verifiable kind with empty evidence is rejected
        try:
            gm.append("conclusion", claim="c", evidence="", author="w")
            assert False, "should require evidence"
        except ValueError:
            pass

        # a verifiable claim, then status transitions (agent-driven)
        gid = gm.append(
            "counterexample",
            claim="Lemma L fails for n=2",
            evidence="Take X=P^1; ... QED.",
            author="worker_xhigh",
        )
        assert [e for e in gm.read("counterexample") if e["id"] == gid][0][
            "status"
        ] == "unverified"
        gm.set_status(gid, "verified", fact_id="abc123")
        entry = [e for e in gm.read("counterexample") if e["id"] == gid][0]
        assert entry["status"] == "verified" and entry["fact_id"] == "abc123"


def test_factgraph():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "proj2")
        base = fg.add(
            problem_id="P",
            author="P_high",
            statement="A holds",
            proof="proof of A",
            glossary_introduces={"X": "a complex manifold"},
        )
        child = fg.add(
            problem_id="P",
            author="P_high",
            statement="B from A",
            proof="uses A",
            predecessors=[base],
        )
        grand = fg.add(
            problem_id="P",
            author="P_high",
            statement="C from B",
            proof="uses B",
            predecessors=[child],
        )

        # content addressing: same content (incl. glossary) -> same id
        assert base == compute_fact_id(
            problem_id="P",
            predecessors=[],
            glossary_introduces={"X": "a complex manifold"},
            statement="A holds",
            proof="proof of A",
        )
        assert fg.predecessors(child) == [base]
        assert set(fg.descendants(base)) == {child, grand}
        assert "## statement" in fg.get_raw(base) and "## proof" in fg.get_raw(base)

        # derived fact index: BM25 search over fact bodies, rebuilt on demand
        hits = fg.search("B from A")
        assert hits and hits[0]["fact_id"] == child
        assert hits[0]["statement"] == "B from A"  # snippet is the ## statement body
        assert "proof" not in hits[0]  # search stays summary-only
        assert all(h["score"] > 0 for h in hits)  # zero-score hits are dropped
        assert fg.search("nonexistent symplectic quark") == []

        # glossary: serialized in the node, merged into the project glossary, parsed back
        assert '"X": "a complex manifold"' in fg.get_raw(base)
        assert fg.glossary().get("X") == "a complex manifold"
        assert parse_frontmatter(fg.get_raw(base))["glossary_introduces"] == {
            "X": "a complex manifold"
        }

        # coverage check: a symbol defined in a predecessor is OK; an undefined one is flagged
        assert fg.undefined_symbols(
            statement="K_F equals zero",
            proof="by X",
            predecessors=[base],
            glossary_introduces={},
        ) == ["K_F"]
        assert (
            fg.undefined_symbols(
                statement="X is nice", proof="X is a manifold", predecessors=[base]
            )
            == []
        )
        # global glossary: universal notation counts as defined everywhere (no project def needed)
        assert (
            fg.undefined_symbols(statement="let epsilon in R+", proof="Z+ is nonempty")
            == []
        )

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
            fg.add(
                problem_id="P",
                author="P_high",
                statement="D from A",
                proof="uses A",
                predecessors=[base],
            )
            assert False, "should refuse revoked predecessor"
        except ValueError as e:
            assert "predecessor_revoked" in str(e)
        try:
            fg.add(
                problem_id="P",
                author="P_high",
                statement="phantom",
                proof="bad",
                predecessors=["0000000000000000"],
            )
            assert False, "should refuse an unknown predecessor"
        except ValueError as e:
            assert "predecessor_unknown" in str(e)
        live = fg.add(problem_id="P", author="P_high", statement="fresh", proof="proof")
        try:
            fg.add(
                problem_id="P",
                author="P_high",
                statement="duplicate edge",
                proof="bad",
                predecessors=[live, live],
            )
            assert False, "should refuse duplicate predecessor edges"
        except ValueError as e:
            assert "duplicate predecessor" in str(e)


def test_factgraph_lazy_context():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "context")
        base = fg.add(problem_id="P", author="w", statement="Base", proof="proof base")
        other = fg.add(
            problem_id="P", author="w", statement="Other", proof="proof other"
        )
        child = fg.add(
            problem_id="P",
            author="w",
            statement="Child",
            proof="proof child",
            predecessors=[base],
        )
        root = fg.add(
            problem_id="P",
            author="w",
            statement="Root",
            proof="proof root",
            predecessors=[child],
        )
        unrelated = fg.add(
            problem_id="P", author="w", statement="Unrelated", proof="must not be read"
        )

        # Default is summary/relations only and depth zero.
        summary = fg.context([root])
        assert summary["facts"] == [
            {
                "fact_id": root,
                "statement": "Root",
                "predecessors": [child],
                "glossary_introduces": {},
            }
        ]
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
            "proof root",
            "proof child",
            "proof base",
        ]

        # A depth bound is complete for the requested scope, not a truncation.
        bounded = fg.context([root], predecessor_depth=1)
        assert [item["fact_id"] for item in bounded["facts"]] == [root, child]
        assert bounded["complete"] is True and bounded["truncated"] is False

        # Budgets charge whole records and stop at the first record that cannot fit.
        first_chars = len(
            json.dumps(
                closure["facts"][0],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
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
        dangling = fg.add(
            problem_id="P",
            author="w",
            statement="Dangling",
            proof="uses missing",
            predecessors=[removed],
        )
        (fg.facts_dir / f"{removed}.md").unlink()  # simulate a corrupt legacy graph
        transitive_missing = fg.context([dangling], predecessor_depth=None)
        assert [item["fact_id"] for item in transitive_missing["facts"]] == [dangling]
        assert transitive_missing["missing_fact_ids"] == [removed]
        assert transitive_missing["complete"] is False

        empty = fg.context(
            [], predecessor_depth=None, proof_mode="selected", max_chars=0
        )
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
            problem_id="P",
            author="w",
            statement="Unicode definition",
            proof="proof",
            glossary_introduces=unicode_glossary,
        )
        assert (
            fg.context([unicode_fact])["facts"][0]["glossary_introduces"]
            == unicode_glossary
        )

        before_invalid = set(fg.list())
        try:
            fg.add(
                problem_id="P",
                author="w",
                statement="Bad intuition boundary",
                proof="proof",
                intuition="first\n\n## intuition\nsecond",
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
        assert glossary_context["glossary"] == {"Q_X": "a distinguished project object"}
        assert glossary_context["scope"]["glossary_terms"] == ["Q_X"]
        assert glossary_context["complete"] is True
        glossary_budget = fg.context(
            [],
            predecessor_depth=None,
            max_chars=1,
            glossary_texts=["Apply Q_X now."],
        )
        assert glossary_budget["facts"] == [] and glossary_budget["glossary"] == {}
        assert glossary_budget["omitted_glossary_terms"] == ["Q_X"]
        assert (
            glossary_budget["complete"] is False
            and glossary_budget["truncated"] is True
        )

        before_conflict = set(fg.list())
        try:
            fg.add(
                problem_id="P",
                author="w",
                statement="Conflicting definition",
                proof="proof",
                glossary_introduces={"Q_X": "a different object"},
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
            problem_id="P",
            author="w",
            statement="Tamper target",
            proof="original proof",
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
        assert [record["fact_id"] for record in context["facts"]] == list(
            reversed(expected)
        )
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
            problem_id="P",
            author="w",
            statement="Base",
            proof="base proof bytes",
            glossary_introduces={"B": "base object"},
        )
        left = fg.add(
            problem_id="P",
            author="w",
            statement="Left",
            proof="left proof",
            predecessors=[base],
        )
        right = fg.add(
            problem_id="P",
            author="w",
            statement="Right",
            proof="right proof",
            predecessors=[base],
        )
        root = fg.add(
            problem_id="P",
            author="w",
            statement="Root",
            proof="root proof",
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
        assert len(first["facts"]) == len(
            {record["fact_id"] for record in first["facts"]}
        )
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
            lambda value: value.__setitem__(
                "characters_used", value["characters_used"] + 1
            ),
        ):
            variant = json.loads(json.dumps(second))
            variant.pop("digest")
            mutate(variant)
            variants.append(verification_context_digest(context=variant))
        assert all(digest != second["digest"] for digest in variants)

        proof_record = {"fact_id": base, "proof": "base proof bytes"}
        record_chars = len(
            json.dumps(
                proof_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
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
            problem_id="P",
            author="w",
            statement="child",
            proof="proof",
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

        reader = threading.Thread(target=lambda: results.setdefault("read", fg.list()))
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
            problem_id="P",
            author="w",
            statement="left",
            proof="proof",
            predecessors=[root],
        )
        right = fg.add(
            problem_id="P",
            author="w",
            statement="right",
            proof="proof",
            predecessors=[root],
        )
        left_leaf = fg.add(
            problem_id="P",
            author="w",
            statement="left leaf",
            proof="proof",
            predecessors=[left],
        )
        right_leaf = fg.add(
            problem_id="P",
            author="w",
            statement="right leaf",
            proof="proof",
            predecessors=[right],
        )
        join = fg.add(
            problem_id="P",
            author="w",
            statement="join",
            proof="proof",
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
            problem_id="P",
            author="w",
            statement="Definition source",
            proof="proof",
            glossary_introduces={term: "a fixed object"},
        )
        independent = fg.add(
            problem_id="P",
            author="w",
            statement=f"{term} has property A",
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
            problem_id="P",
            author="w",
            statement=f"{term} has property A",
            proof=f"Use the definition of {term}.",
            predecessors=[source],
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
            [],
            predecessor_depth=None,
            proof_mode="none",
            glossary_texts=[f"Use {term}."],
        )
        assert still_available["complete"] is True
        assert still_available["glossary"] == {term: definition}

        fg.revoke(second, reason="last introducer withdrawn")
        assert term not in fg.glossary()
        removed = fg.context(
            [],
            predecessor_depth=None,
            proof_mode="none",
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
            problem_id="P",
            author="w",
            statement="Definition source",
            proof="proof",
            glossary_introduces={term: "temporary definition"},
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
                [],
                predecessor_depth=None,
                proof_mode="none",
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
        assert fg.revoke(fact_id, reason="retry may use a different message") == [
            fact_id
        ]
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
                    problem_id="P",
                    author="w",
                    statement="Never partial",
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
                    problem_id="P",
                    author="w",
                    statement="Definition candidate",
                    proof="proof",
                    glossary_introduces={term: "a test object"},
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
                    problem_id="P",
                    author="w",
                    statement="Pending definition",
                    proof="proof",
                    glossary_introduces={term: "a pending object"},
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


def test_fact_add_no_glossary_post_replace_fsync_failure_rolls_back():
    """A fact is not published until its data rename is durably acknowledged."""
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "no-glossary-fsync")
        original_fsync_directory = fg._fsync_directory
        injected = False

        def fail_after_fact_directory_fsync(directory):
            nonlocal injected
            original_fsync_directory(directory)
            if directory == fg.facts_dir and not injected:
                injected = True
                raise OSError("injected post-replace fact fsync failure")

        fg._fsync_directory = fail_after_fact_directory_fsync  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="Never visible after a failed add",
                    proof="proof",
                )
                assert False, "the post-replace fsync failure must propagate"
            except OSError as exc:
                assert "post-replace fact fsync failure" in str(exc)
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        assert fg.list() == []
        assert not fg.pending_add_path.exists()
        assert not fg.pending_add_commit_path.exists()


def test_fact_add_exact_retry_returns_without_rewriting_committed_state():
    """A lost-response retry is an idempotent read of the existing exact commit."""
    for index, glossary_introduces in enumerate(
        ({}, {"IDEMPOTENT_RETRY_X_418": "the retry test object"})
    ):
        with tempfile.TemporaryDirectory() as d:
            fg = FactGraph(Path(d) / f"idempotent-retry-{index}")
            kwargs = {
                "problem_id": "P",
                "author": "w",
                "statement": f"An exactly repeated fact {index}",
                "proof": "proof",
                "glossary_introduces": glossary_introduces,
            }
            fact_id = fg.add(**kwargs)
            original_fsync_directory = fg._fsync_directory
            fact_fsync_attempted = False

            def reject_redundant_fact_fsync(directory):
                nonlocal fact_fsync_attempted
                if directory == fg.facts_dir:
                    fact_fsync_attempted = True
                    raise OSError("redundant fact rewrite must not run")
                original_fsync_directory(directory)

            fg._fsync_directory = reject_redundant_fact_fsync  # type: ignore[method-assign]
            try:
                assert fg.add(**kwargs) == fact_id
            finally:
                fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

            assert fact_fsync_attempted is False
            assert fg.list() == [fact_id]
            assert not fg.pending_add_path.exists()
            assert not fg.pending_add_commit_path.exists()


def test_full_fact_identity_is_canonical_and_binds_context_and_glossary():
    common = {
        "problem_id": "P",
        "predecessors": ["b" * 16, "a" * 16],
        "glossary_introduces": {"Y": " a   definition ", "X": "another"},
        "statement": "  A   holds\n",
        "proof": "by   induction",
        "context_bindings": {
            "facts": [{"fact_id": "a" * 16, "statement": "base"}],
            "projection": "test",
        },
        "glossary_bindings": {"Z": " fixed   object "},
    }
    first = _schema.compute_fact_identity(**common)
    cosmetic = _schema.compute_fact_identity(
        **{
            **common,
            "predecessors": list(reversed(common["predecessors"])),
            "glossary_introduces": {"X": "another", "Y": "a definition"},
            "statement": "A holds",
            "proof": "by induction",
            "glossary_bindings": {"Z": "fixed object"},
        }
    )
    assert first == cosmetic and len(first) == 64
    assert (
        _schema.compute_fact_identity(
            **{**common, "glossary_bindings": {"Z": "a changed object"}}
        )
        != first
    )
    assert (
        _schema.compute_fact_identity(
            **{
                **common,
                "context_bindings": {
                    "facts": [{"fact_id": "a" * 16, "statement": "changed"}],
                    "projection": "test",
                },
            }
        )
        != first
    )


def test_round_zero_full_identity_uses_exact_snapshot_and_ignores_budgets():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "round-zero-identity")
        content = {
            "problem_id": "P",
            "predecessors": [],
            "glossary_introduces": {},
            "statement": "A budget-stable semantic candidate",
            "proof": "A complete proof of the candidate.",
        }
        fact_id = compute_fact_id(**content)

        def snapshot(character_budget: int) -> dict[str, object]:
            return fg.verification_context(
                [],
                max_chars=character_budget,
                candidate_fact_id=fact_id,
                expanded_proof_ids=[],
                expansion_round=0,
                expanded_proof_max_chars=character_budget,
                glossary_texts=[
                    content["statement"],
                    content["proof"],
                    "mutable intuition-only display text",
                ],
                glossary_exclude_terms=[],
            )

        first_context = snapshot(200_000)
        drifted_budget_context = snapshot(210_000)
        assert first_context["digest"] != drifted_budget_context["digest"]
        first_identity = fact_identity_from_verification_context(
            verification_context=first_context,
            **content,
        )
        assert first_identity == fact_identity_from_verification_context(
            verification_context=drifted_budget_context,
            **content,
        )

        assert fg.add(author="worker", **content) == fact_id
        with fg.locked_active_fact_identity(fact_id) as active_identity:
            assert active_identity == first_identity
        assert fg.lookup_active_exact_identity(**content) == (fact_id, first_identity)

        try:
            fact_identity_from_verification_context(
                verification_context=first_context,
                **{**content, "statement": "A distinct colliding candidate"},
            )
            assert False, "round-zero context must bind exact candidate content"
        except ValueError as exc:
            assert "round-zero candidate snapshot" in str(exc)


def test_active_exact_lookup_is_read_only_and_short_collisions_fail_closed():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "exact-identity")
        original_refs = [{"key": "ORIGINAL", "title": "Original reference"}]
        fact_id = fg.add(
            problem_id="P",
            author="first-author",
            statement="An exact active fact",
            proof="A complete proof.",
            intuition="first intuition",
            external_refs=original_refs,
        )
        fact_path = fg._path(fact_id)
        before = (fact_path.stat().st_mtime_ns, fact_path.read_bytes())

        assert (
            fg.lookup_active_exact(
                problem_id="P",
                statement="  An exact   active fact ",
                proof="A complete proof.",
            )
            == fact_id
        )
        assert (
            fg.add(
                problem_id="P",
                author="different-author",
                statement="An exact active fact",
                proof="A complete proof.",
                intuition="different mutable intuition",
                external_refs=[{"key": "NEW", "title": "Must not replace"}],
            )
            == fact_id
        )
        assert (fact_path.stat().st_mtime_ns, fact_path.read_bytes()) == before
        assert fg.external_refs(fact_id) == original_refs

        original_compute = _factgraph.compute_fact_id
        _factgraph.compute_fact_id = lambda **_kwargs: fact_id
        try:
            try:
                fg.lookup_active_exact(
                    problem_id="P",
                    statement="A genuinely different colliding statement",
                    proof="Different proof.",
                )
                assert False, "a short-id/full-identity collision must fail closed"
            except ValueError as exc:
                assert "fact_identity_collision" in str(exc)
        finally:
            _factgraph.compute_fact_id = original_compute

        assert fg.revoke(fact_id, reason="identity revoked") == [fact_id]
        assert (
            fg.lookup_active_exact(
                problem_id="P",
                statement="An exact active fact",
                proof="A complete proof.",
            )
            is None
        )


def test_fact_add_commit_marker_fsync_failure_rolls_back():
    """A visible but unacknowledged commit marker cannot publish the new data."""
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "commit-marker-fsync")
        original_fsync_directory = fg._fsync_directory
        injected = False

        def fail_after_commit_marker_directory_fsync(directory):
            nonlocal injected
            original_fsync_directory(directory)
            if (
                directory == fg.dir
                and fg.pending_add_commit_path.exists()
                and not injected
            ):
                injected = True
                raise OSError("injected commit-marker fsync failure")

        fg._fsync_directory = fail_after_commit_marker_directory_fsync  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="Not committed until the marker fsync returns",
                    proof="proof",
                    glossary_introduces={
                        "FAILED_COMMIT_X_615": "an uncommitted test object"
                    },
                )
                assert False, "the commit-marker fsync failure must propagate"
            except OSError as exc:
                assert "commit-marker fsync failure" in str(exc)
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        assert fg.list() == []
        assert "FAILED_COMMIT_X_615" not in fg.glossary()
        assert not fg.pending_add_path.exists()
        assert not fg.pending_add_commit_path.exists()
        assert not fg.pending_add_abort_path.exists()

    # If the uncertain marker itself cannot be removed, the durable rollback
    # intent makes every read fail closed.  A later mutation completes rollback
    # before preparing any new add.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "commit-marker-uncertain"
        fg = FactGraph(root)
        original_fsync_directory = fg._fsync_directory
        original_unlink = fg._unlink_durable
        injected = False

        def fail_commit_marker_fsync_once(directory):
            nonlocal injected
            original_fsync_directory(directory)
            if (
                directory == fg.dir
                and fg.pending_add_commit_path.exists()
                and not fg.pending_add_abort_path.exists()
                and not injected
            ):
                injected = True
                raise OSError("injected uncertain commit-marker fsync")

        def fail_commit_marker_unlink(path):
            if path == fg.pending_add_commit_path:
                raise OSError("injected commit-marker unlink failure")
            original_unlink(path)

        fg._fsync_directory = fail_commit_marker_fsync_once  # type: ignore[method-assign]
        fg._unlink_durable = fail_commit_marker_unlink  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="An uncertain commit must remain hidden",
                    proof="proof",
                )
                assert False, "uncertain rollback must surface recovery-required"
            except RuntimeError as exc:
                assert "fact_graph_recovery_required" in str(exc)
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]

        assert fg.pending_add_abort_path.exists()
        try:
            FactGraph(root).list()
            assert False, "uncertain rollback state must fail closed"
        except ValueError as exc:
            assert "fact_graph_recovery_required" in str(exc)
        survivor = FactGraph(root).add(
            problem_id="P",
            author="w",
            statement="After rollback recovery",
            proof="proof",
        )
        restarted = FactGraph(root)
        assert restarted.list() == [survivor]
        assert not restarted.pending_add_path.exists()
        assert not restarted.pending_add_commit_path.exists()
        assert not restarted.pending_add_abort_path.exists()


def test_fact_add_reports_unknown_when_rollback_intent_cannot_be_durable():
    """An ambiguous crash outcome is never mislabeled as a definitive rollback."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "promotion-unknown"
        fg = FactGraph(root)
        original_atomic_write = fg._atomic_write_text
        original_fsync_directory = fg._fsync_directory

        def inject_ambiguous_markers(path, text):
            if path == fg.pending_add_commit_path:
                original_atomic_write(path, text)
                raise MemoryError("injected error after durable commit marker")
            if path == fg.pending_add_abort_path:
                path.write_text(text, encoding="utf-8")
                raise OSError("injected error before rollback-marker durability")
            original_atomic_write(path, text)

        def fail_abort_directory_fsync(directory):
            if directory == fg.dir and fg.pending_add_abort_path.exists():
                raise OSError("injected rollback-marker fsync failure")
            original_fsync_directory(directory)

        fg._atomic_write_text = inject_ambiguous_markers  # type: ignore[method-assign]
        fg._fsync_directory = fail_abort_directory_fsync  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="Durability outcome is intentionally ambiguous",
                    proof="proof",
                )
                assert False, "an ambiguous outcome must not return a fact id"
            except FactPromotionOutcomeUnknown as exc:
                assert "fact_graph_promotion_unknown" in str(exc)
        finally:
            fg._atomic_write_text = original_atomic_write  # type: ignore[method-assign]
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        assert fg.pending_add_path.exists()
        assert fg.pending_add_commit_path.exists()
        assert fg.pending_add_abort_path.exists()
        try:
            fg.list()
            assert False, "a visible rollback marker must fail closed"
        except ValueError as exc:
            assert "fact_graph_recovery_required" in str(exc)

        # Simulate power loss discarding only the non-durable abort entry.  The
        # durable commit marker then preserves the exact fact on restart.  This
        # is compatible only with the explicit unknown outcome above, never with
        # a definitive promoted:false response.
        fg.pending_add_abort_path.unlink()
        restarted = FactGraph(root)
        visible = restarted.list()
        assert len(visible) == 1
        assert restarted.get_raw(visible[0]) is not None


def test_factgraph_all_journal_unlinks_retry_transient_directory_fsync_failure():
    """Every transaction-marker deletion retries a one-shot root fsync fault."""
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "journal-unlink-retry")
        marker_paths = (
            fg.pending_add_path,
            fg.pending_add_commit_path,
            fg.pending_add_abort_path,
            fg.pending_revocation_path,
        )
        for marker_path in marker_paths:
            fg._atomic_write_text(marker_path, "{}\n")
            original_fsync_directory = fg._fsync_directory
            root_attempts = 0

            def fail_first_root_fsync(directory):
                nonlocal root_attempts
                if directory == fg.dir:
                    root_attempts += 1
                    if root_attempts == 1:
                        raise OSError("injected one-shot journal cleanup fsync failure")
                original_fsync_directory(directory)

            fg._fsync_directory = fail_first_root_fsync  # type: ignore[method-assign]
            try:
                fg._unlink_durable(marker_path)
            finally:
                fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

            assert root_attempts == 2
            assert not marker_path.exists()


def test_factgraph_read_barriers_visible_commit_marker_before_exposure():
    """A commit rename is never authoritative before its root-dir barrier."""

    class SimulatedCrash(BaseException):
        pass

    def leave_visible_commit_before_directory_fsync(root, statement):
        fg = FactGraph(root)
        original_fsync_directory = fg._fsync_directory
        crashed = False

        def crash_at_commit_marker_directory_fsync(directory):
            nonlocal crashed
            if (
                directory == fg.dir
                and fg.pending_add_commit_path.exists()
                and fg.pending_add_path.exists()
                and not fg.pending_add_abort_path.exists()
                and not crashed
            ):
                crashed = True
                raise SimulatedCrash()
            original_fsync_directory(directory)

        fg._fsync_directory = crash_at_commit_marker_directory_fsync  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement=statement,
                    proof="proof",
                )
                assert False, "the simulated process crash must escape"
            except SimulatedCrash:
                pass
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        expected_id = compute_fact_id(
            problem_id="P",
            predecessors=[],
            glossary_introduces={},
            statement=statement,
            proof="proof",
        )
        assert fg.pending_add_path.exists()
        assert fg.pending_add_commit_path.exists()
        return expected_id

    # A one-shot failure is retried under the shared lock; only the successful
    # second barrier permits the exact committed fact to become visible.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "commit-read-one-shot"
        expected_id = leave_visible_commit_before_directory_fsync(
            root, "visible commit requires a read barrier"
        )
        reader = FactGraph(root)
        original_fsync_directory = reader._fsync_directory
        root_attempts = 0

        def fail_first_read_barrier(directory):
            nonlocal root_attempts
            if directory == reader.dir:
                root_attempts += 1
                if root_attempts == 1:
                    raise OSError("injected one-shot read barrier failure")
            original_fsync_directory(directory)

        reader._fsync_directory = fail_first_read_barrier  # type: ignore[method-assign]
        try:
            assert reader.list() == [expected_id]
        finally:
            reader._fsync_directory = original_fsync_directory  # type: ignore[method-assign]
        assert root_attempts == 2

        survivor = reader.add(
            problem_id="P", author="w", statement="after durable read", proof="proof"
        )
        assert reader.list() == sorted([expected_id, survivor])

    # Persistent failure exposes nothing.  If power loss then discards the
    # unacknowledged commit entry, prepared-only recovery rolls it back without
    # contradicting any successful read.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "commit-read-persistent"
        expected_id = leave_visible_commit_before_directory_fsync(
            root, "never expose an unacknowledged commit marker"
        )
        reader = FactGraph(root)
        original_fsync_directory = reader._fsync_directory

        def fail_read_barrier_persistently(directory):
            if directory == reader.dir:
                raise OSError("injected persistent read barrier failure")
            original_fsync_directory(directory)

        reader._fsync_directory = fail_read_barrier_persistently  # type: ignore[method-assign]
        try:
            try:
                reader.list()
                assert False, "an unacknowledged commit must not be exposed"
            except ValueError as exc:
                assert "committed add marker durability barrier failed" in str(exc)
        finally:
            reader._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        reader.pending_add_commit_path.unlink()
        restarted = FactGraph(root)
        try:
            restarted.list()
            assert False, "prepared-only crash state must remain fail closed"
        except ValueError as exc:
            assert "fact_graph_recovery_required" in str(exc)
        survivor = restarted.add(
            problem_id="P", author="w", statement="after crash rollback", proof="proof"
        )
        assert restarted.list() == [survivor]
        assert restarted.get_raw(expected_id) is None


def test_factgraph_root_directory_parent_barrier_retries_before_graph_writes():
    """Root mkdir durability is retried and persistent failure changes no data."""
    # A one-shot parent fsync error is absorbed by the bounded retry, so the
    # first mutation can safely proceed.
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "mkdir-parent-one-shot")
        original_fsync_directory = fg._fsync_directory
        parent_attempts = 0

        def fail_parent_barrier_once(directory):
            nonlocal parent_attempts
            if directory == fg.dir.parent:
                parent_attempts += 1
                if parent_attempts == 1:
                    raise OSError("injected one-shot parent fsync failure")
            original_fsync_directory(directory)

        fg._fsync_directory = fail_parent_barrier_once  # type: ignore[method-assign]
        try:
            fact_id = fg.add(
                problem_id="P",
                author="w",
                statement="safe after one-shot root parent failure",
                proof="proof",
            )
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        assert parent_attempts >= 2
        assert fg.list() == [fact_id]

    # If both bounded attempts fail after mkdir, the visible directory is not
    # assumed durable.  The next call barriers its parent even though it exists.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "mkdir-parent-retry"
        fg = FactGraph(root)
        original_fsync_directory = fg._fsync_directory
        failed_parent_attempts = 0

        def fail_initial_factgraph_parent_barrier(directory):
            nonlocal failed_parent_attempts
            if directory == fg.dir.parent:
                failed_parent_attempts += 1
                if failed_parent_attempts <= 2:
                    raise OSError("injected persistent first parent fsync failure")
            original_fsync_directory(directory)

        fg._fsync_directory = fail_initial_factgraph_parent_barrier  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="must wait for durable graph root",
                    proof="proof",
                )
                assert False, "the first undurable root creation must fail"
            except OSError as exc:
                assert "persistent first parent fsync failure" in str(exc)
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        assert fg.dir.exists()
        parent_barriers = 0

        def record_parent_barrier(directory):
            nonlocal parent_barriers
            if directory == fg.dir.parent:
                parent_barriers += 1
            original_fsync_directory(directory)

        fg._fsync_directory = record_parent_barrier  # type: ignore[method-assign]
        try:
            fact_id = fg.add(
                problem_id="P",
                author="w",
                statement="written only after durable graph root",
                proof="proof",
            )
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        assert parent_barriers >= 1
        assert fg.list() == [fact_id]

    # A persistent parent failure occurs before even the lock file or journals
    # are created.  Removing the empty, unacknowledged directory models power
    # loss discarding its parent entry; restart then begins from a clean state.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "mkdir-parent-power-loss"
        fg = FactGraph(root)
        original_fsync_directory = fg._fsync_directory

        def fail_parent_barrier_persistently(directory):
            if directory == fg.dir.parent:
                raise OSError("injected persistent parent fsync failure")
            original_fsync_directory(directory)

        fg._fsync_directory = fail_parent_barrier_persistently  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="never written under an undurable graph root",
                    proof="proof",
                )
                assert False, "persistent root parent failure must stop mutation"
            except OSError as exc:
                assert "persistent parent fsync failure" in str(exc)
        finally:
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        assert fg.dir.exists()
        assert list(fg.dir.iterdir()) == []
        fg.dir.rmdir()
        restarted = FactGraph(root)
        fact_id = restarted.add(
            problem_id="P",
            author="w",
            statement="safe after simulated loss of the unacknowledged root",
            proof="proof",
        )
        assert restarted.list() == [fact_id]


def test_fact_add_committed_cleanup_failure_preserves_fact_and_glossary():
    """Cleanup is not part of the publication decision after durable commit."""
    for index, injected_error in enumerate((OSError(), MemoryError())):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / f"whole-cleanup-failure-{index}"
            fg = FactGraph(root)
            term = f"WHOLE_CLEANUP_X_{index}"
            original_cleanup = fg._cleanup_committed_add_unlocked

            def fail_whole_cleanup(_error=injected_error):
                raise _error

            fg._cleanup_committed_add_unlocked = fail_whole_cleanup  # type: ignore[method-assign]
            try:
                fact_id = fg.add(
                    problem_id="P",
                    author="w",
                    statement=f"Committed despite cleanup exception {index}",
                    proof="proof",
                    glossary_introduces={term: "a durably committed test object"},
                )
            finally:
                fg._cleanup_committed_add_unlocked = original_cleanup  # type: ignore[method-assign]

            assert fg.pending_add_path.exists()
            assert fg.pending_add_commit_path.exists()
            restarted = FactGraph(root)
            assert restarted.list() == [fact_id]
            assert restarted.glossary()[term] == "a durably committed test object"
            later = restarted.add(
                problem_id="P",
                author="w",
                statement=f"After whole cleanup exception {index}",
                proof="proof",
            )
            assert restarted.list() == sorted([fact_id, later])
            assert not restarted.pending_add_path.exists()
            assert not restarted.pending_add_commit_path.exists()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "committed-cleanup"
        fg = FactGraph(root)
        term = "COMMITTED_CLEANUP_X_812"
        original_unlink = fg._unlink_durable
        injected = False

        def unlink_commit_then_fail(path):
            nonlocal injected
            original_unlink(path)
            if path == fg.pending_add_commit_path and not injected:
                injected = True
                raise OSError("injected committed-marker unlink fsync failure")

        fg._unlink_durable = unlink_commit_then_fail  # type: ignore[method-assign]
        try:
            fact_id = fg.add(
                problem_id="P",
                author="w",
                statement="A durably committed definition",
                proof="proof",
                glossary_introduces={term: "the committed test object"},
            )
        finally:
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]

        restarted = FactGraph(root)
        assert restarted.list() == [fact_id]
        assert restarted.glossary()[term] == "the committed test object"
        assert not restarted.pending_add_path.exists()
        assert not restarted.pending_add_commit_path.exists()

    # If cleanup cannot even unlink the committed marker, reads still validate
    # and expose the exact commit.  A later mutation must finalize that marker
    # before it is allowed to prepare another transaction.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "committed-marker-residue"
        fg = FactGraph(root)
        original_unlink = fg._unlink_durable

        def leave_committed_marker(path):
            if path == fg.pending_add_commit_path:
                raise OSError("injected committed-marker residue")
            original_unlink(path)

        fg._unlink_durable = leave_committed_marker  # type: ignore[method-assign]
        try:
            committed = fg.add(
                problem_id="P",
                author="w",
                statement="Committed with a residual marker",
                proof="proof",
            )
        finally:
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]

        restarted = FactGraph(root)
        assert restarted.list() == [committed]
        assert restarted.pending_add_commit_path.exists()
        later = restarted.add(
            problem_id="P", author="w", statement="After finalization", proof="proof"
        )
        assert restarted.list() == sorted([committed, later])
        assert not restarted.pending_add_path.exists()
        assert not restarted.pending_add_commit_path.exists()


def test_committed_marker_unlink_ambiguity_blocks_mutable_metadata_change():
    """A resurrectable commit marker cannot be invalidated by later ref edits."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "committed-unlink-ambiguity"
        fg = FactGraph(root)
        original_unlink = fg._unlink_durable
        original_fsync_directory = fg._fsync_directory
        committed_marker = b""
        cleanup_ambiguous = False

        def unlink_commit_without_directory_barrier(path):
            nonlocal committed_marker, cleanup_ambiguous
            if path == fg.pending_add_commit_path:
                committed_marker = path.read_bytes()
                path.unlink()
                cleanup_ambiguous = True
                raise OSError("injected unlink-before-fsync failure")
            original_unlink(path)

        def fail_root_barrier_after_ambiguous_unlink(directory):
            if cleanup_ambiguous and directory == fg.dir:
                raise OSError("injected persistent root directory fsync failure")
            original_fsync_directory(directory)

        fg._unlink_durable = unlink_commit_without_directory_barrier  # type: ignore[method-assign]
        fg._fsync_directory = fail_root_barrier_after_ambiguous_unlink  # type: ignore[method-assign]
        try:
            fact_id = fg.add(
                problem_id="P",
                author="w",
                statement="Committed before ambiguous marker cleanup",
                proof="proof",
            )
            raw_before = fg.get_raw(fact_id)
            assert committed_marker and not fg.pending_add_commit_path.exists()
            try:
                fg.set_external_refs(
                    fact_id,
                    [{"key": "R", "title": "must not be written yet"}],
                )
                assert False, "persistent directory ambiguity must block ref mutation"
            except RuntimeError as exc:
                assert "mutation directory durability barrier failed" in str(exc)
            assert fg.get_raw(fact_id) == raw_before
            assert fg.external_refs(fact_id) == []
        finally:
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        # Model the only crash state permitted by the failed directory fsync:
        # the old commit-marker entry reappears.  Since the ref edit was blocked,
        # its exact byte hash still matches and normal recovery can finish.
        fg.pending_add_commit_path.write_bytes(committed_marker)
        restarted = FactGraph(root)
        assert restarted.list() == [fact_id]
        refs = [{"key": "R", "title": "written after durable recovery"}]
        assert restarted.set_external_refs(fact_id, refs) == refs
        assert restarted.external_refs(fact_id) == refs
        assert not restarted.pending_add_commit_path.exists()


def test_prepared_marker_unlink_ambiguity_blocks_later_mutation():
    """A failed rollback cleanup cannot erase its journal then permit writes."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "prepared-unlink-ambiguity"
        fg = FactGraph(root)
        stable = fg.add(problem_id="P", author="w", statement="stable", proof="proof")
        stable_raw = fg.get_raw(stable)
        stable_path = fg._path(stable)
        original_atomic_write = fg._atomic_write_text
        original_unlink = fg._unlink_durable
        original_fsync_directory = fg._fsync_directory
        prepared_marker = b""
        cleanup_ambiguous = False

        def fail_candidate_fact_write(path, text):
            if path.parent == fg.facts_dir and path != stable_path:
                raise OSError("injected candidate fact write failure")
            original_atomic_write(path, text)

        def unlink_prepared_without_directory_barrier(path):
            nonlocal prepared_marker, cleanup_ambiguous
            if path == fg.pending_add_path:
                prepared_marker = path.read_bytes()
                path.unlink()
                cleanup_ambiguous = True
                raise OSError("injected prepared-marker cleanup failure")
            original_unlink(path)

        def fail_root_barrier_after_ambiguous_unlink(directory):
            if cleanup_ambiguous and directory == fg.dir:
                raise OSError("injected persistent root directory fsync failure")
            original_fsync_directory(directory)

        fg._atomic_write_text = fail_candidate_fact_write  # type: ignore[method-assign]
        fg._unlink_durable = unlink_prepared_without_directory_barrier  # type: ignore[method-assign]
        fg._fsync_directory = fail_root_barrier_after_ambiguous_unlink  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="candidate rolled back before commit",
                    proof="proof",
                )
                assert False, "ambiguous rollback cleanup must fail closed"
            except RuntimeError as exc:
                assert "fact_graph_recovery_required" in str(exc)
            assert prepared_marker and not fg.pending_add_path.exists()
            try:
                fg.set_external_refs(
                    stable,
                    [{"key": "R", "title": "must not cross rollback ambiguity"}],
                )
                assert False, "persistent directory ambiguity must block ref mutation"
            except RuntimeError as exc:
                assert "mutation directory durability barrier failed" in str(exc)
            assert fg.get_raw(stable) == stable_raw
        finally:
            fg._atomic_write_text = original_atomic_write  # type: ignore[method-assign]
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        fg.pending_add_path.write_bytes(prepared_marker)
        restarted = FactGraph(root)
        refs = [{"key": "R", "title": "written after prepared recovery"}]
        assert restarted.set_external_refs(stable, refs) == refs
        assert restarted.list() == [stable]
        assert restarted.external_refs(stable) == refs
        assert not restarted.pending_add_path.exists()


def test_aborted_add_final_prepared_unlink_ambiguity_blocks_later_mutation():
    """Abort recovery also requires durable final prepared-marker removal."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "aborted-prepared-unlink-ambiguity"
        fg = FactGraph(root)
        stable = fg.add(problem_id="P", author="w", statement="stable", proof="proof")
        stable_raw = fg.get_raw(stable)
        original_atomic_write = fg._atomic_write_text
        original_unlink = fg._unlink_durable
        original_fsync_directory = fg._fsync_directory
        prepared_marker = b""
        cleanup_ambiguous = False
        commit_write_failed = False

        def commit_marker_then_fail(path, text):
            nonlocal commit_write_failed
            if path == fg.pending_add_commit_path and not commit_write_failed:
                commit_write_failed = True
                original_atomic_write(path, text)
                raise OSError("injected error after durable commit-marker write")
            original_atomic_write(path, text)

        def unlink_final_prepared_without_directory_barrier(path):
            nonlocal prepared_marker, cleanup_ambiguous
            if path == fg.pending_add_path and not fg.pending_add_abort_path.exists():
                prepared_marker = path.read_bytes()
                path.unlink()
                cleanup_ambiguous = True
                raise OSError("injected aborted prepared-marker cleanup failure")
            original_unlink(path)

        def fail_root_barrier_after_ambiguous_unlink(directory):
            if cleanup_ambiguous and directory == fg.dir:
                raise OSError("injected persistent root directory fsync failure")
            original_fsync_directory(directory)

        fg._atomic_write_text = commit_marker_then_fail  # type: ignore[method-assign]
        fg._unlink_durable = unlink_final_prepared_without_directory_barrier  # type: ignore[method-assign]
        fg._fsync_directory = fail_root_barrier_after_ambiguous_unlink  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="candidate with explicit abort recovery",
                    proof="proof",
                )
                assert False, "ambiguous abort cleanup must fail closed"
            except RuntimeError as exc:
                assert "fact_graph_recovery_required" in str(exc)
            assert prepared_marker and not fg.pending_add_path.exists()
            assert not fg.pending_add_abort_path.exists()
            try:
                fg.set_external_refs(
                    stable,
                    [{"key": "R", "title": "must not cross abort ambiguity"}],
                )
                assert False, "persistent directory ambiguity must block ref mutation"
            except RuntimeError as exc:
                assert "mutation directory durability barrier failed" in str(exc)
            assert fg.get_raw(stable) == stable_raw
        finally:
            fg._atomic_write_text = original_atomic_write  # type: ignore[method-assign]
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        # If only the prepared entry reappears, ordinary prepared recovery
        # repeats the already-completed rollback before allowing the ref edit.
        fg.pending_add_path.write_bytes(prepared_marker)
        restarted = FactGraph(root)
        refs = [{"key": "R", "title": "written after abort recovery"}]
        assert restarted.set_external_refs(stable, refs) == refs
        assert restarted.list() == [stable]
        assert restarted.external_refs(stable) == refs
        assert not restarted.pending_add_path.exists()


def test_revocation_marker_unlink_ambiguity_blocks_later_mutation():
    """A completed revoke with an ambiguous journal unlink stays retry-safe."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "revocation-unlink-ambiguity"
        fg = FactGraph(root)
        stable = fg.add(problem_id="P", author="w", statement="stable", proof="proof")
        victim = fg.add(problem_id="P", author="w", statement="victim", proof="proof")
        stable_raw = fg.get_raw(stable)
        original_unlink = fg._unlink_durable
        original_fsync_directory = fg._fsync_directory
        revocation_marker = b""
        cleanup_ambiguous = False

        def unlink_revocation_without_directory_barrier(path):
            nonlocal revocation_marker, cleanup_ambiguous
            if path == fg.pending_revocation_path:
                revocation_marker = path.read_bytes()
                path.unlink()
                cleanup_ambiguous = True
                raise OSError("injected revocation-marker cleanup failure")
            original_unlink(path)

        def fail_root_barrier_after_ambiguous_unlink(directory):
            if cleanup_ambiguous and directory == fg.dir:
                raise OSError("injected persistent root directory fsync failure")
            original_fsync_directory(directory)

        fg._unlink_durable = unlink_revocation_without_directory_barrier  # type: ignore[method-assign]
        fg._fsync_directory = fail_root_barrier_after_ambiguous_unlink  # type: ignore[method-assign]
        try:
            try:
                fg.revoke(victim, reason="test ambiguous cleanup")
                assert False, "ambiguous revocation cleanup must propagate"
            except OSError as exc:
                assert "revocation-marker cleanup failure" in str(exc)
            assert revocation_marker and not fg.pending_revocation_path.exists()
            try:
                fg.set_external_refs(
                    stable,
                    [{"key": "R", "title": "must not cross revoke ambiguity"}],
                )
                assert False, "persistent directory ambiguity must block ref mutation"
            except RuntimeError as exc:
                assert "mutation directory durability barrier failed" in str(exc)
            assert fg.get_raw(stable) == stable_raw
        finally:
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]
            fg._fsync_directory = original_fsync_directory  # type: ignore[method-assign]

        fg.pending_revocation_path.write_bytes(revocation_marker)
        restarted = FactGraph(root)
        refs = [{"key": "R", "title": "written after revoke recovery"}]
        assert restarted.set_external_refs(stable, refs) == refs
        assert restarted.list() == [stable]
        assert restarted.external_refs(stable) == refs
        logged = read_jsonl(restarted.revocation_log)
        assert [entry["fact_id"] for entry in logged].count(victim) == 1
        assert not restarted.pending_revocation_path.exists()


def test_fact_add_restart_recovers_prepared_and_preserves_committed():
    """Crash recovery rolls back prepared state but never a committed marker."""

    class SimulatedCrash(BaseException):
        pass

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "prepared-crash"
        fg = FactGraph(root)
        term = "PREPARED_CRASH_X_724"
        original_atomic_write = fg._atomic_write_text

        def crash_before_commit_marker(path, text):
            if path == fg.pending_add_commit_path:
                raise SimulatedCrash()
            original_atomic_write(path, text)

        fg._atomic_write_text = crash_before_commit_marker  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="Prepared but not committed",
                    proof="proof",
                    glossary_introduces={term: "a transient test object"},
                )
                assert False, "the simulated crash must escape"
            except SimulatedCrash:
                pass
        finally:
            fg._atomic_write_text = original_atomic_write  # type: ignore[method-assign]

        restarted = FactGraph(root)
        try:
            restarted.list()
            assert False, "prepared crash state must fail closed before recovery"
        except ValueError as exc:
            assert "fact_graph_recovery_required" in str(exc)
        survivor = restarted.add(
            problem_id="P", author="w", statement="Recovery survivor", proof="proof"
        )
        assert restarted.list() == [survivor]
        assert term not in restarted.glossary()
        assert not restarted.pending_add_path.exists()
        assert not restarted.pending_add_commit_path.exists()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "committed-crash"
        fg = FactGraph(root)
        term = "COMMITTED_CRASH_X_539"
        expected_fact_id = compute_fact_id(
            problem_id="P",
            predecessors=[],
            glossary_introduces={term: "a durable test object"},
            statement="Committed before cleanup",
            proof="proof",
        )
        original_unlink = fg._unlink_durable

        def crash_before_committed_cleanup(path):
            if path == fg.pending_add_path:
                raise SimulatedCrash()
            original_unlink(path)

        fg._unlink_durable = crash_before_committed_cleanup  # type: ignore[method-assign]
        try:
            try:
                fg.add(
                    problem_id="P",
                    author="w",
                    statement="Committed before cleanup",
                    proof="proof",
                    glossary_introduces={term: "a durable test object"},
                )
                assert False, "the simulated crash must escape"
            except SimulatedCrash:
                pass
        finally:
            fg._unlink_durable = original_unlink  # type: ignore[method-assign]

        assert fg.pending_add_path.exists()
        assert fg.pending_add_commit_path.exists()
        restarted = FactGraph(root)
        assert restarted.list() == [expected_fact_id]
        assert restarted.glossary()[term] == "a durable test object"
        survivor = restarted.add(
            problem_id="P", author="w", statement="After committed crash", proof="proof"
        )
        assert restarted.list() == sorted([expected_fact_id, survivor])
        assert restarted.glossary()[term] == "a durable test object"
        assert not restarted.pending_add_path.exists()
        assert not restarted.pending_add_commit_path.exists()


def test_revoke_journal_recovers_second_move_and_log_failures_once():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "second-move")
        root = fg.add(problem_id="P", author="w", statement="root", proof="proof")
        child = fg.add(
            problem_id="P",
            author="w",
            statement="child",
            proof="proof",
            predecessors=[root],
        )
        leaf = fg.add(
            problem_id="P",
            author="w",
            statement="leaf",
            proof="proof",
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
        assert sorted(entry["fact_id"] for entry in logged) == sorted(
            [root, child, leaf]
        )

    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "log-failure")
        root = fg.add(problem_id="P", author="w", statement="root", proof="proof")
        child = fg.add(
            problem_id="P",
            author="w",
            statement="child",
            proof="proof",
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
                problem_id="P",
                author="w",
                statement="Conflicting global alias",
                proof="proof",
                glossary_introduces={term: definition + " (different meaning)"},
            )
            assert False, "project glossary must not shadow a global definition"
        except ValueError as exc:
            assert "glossary_conflict" in str(exc)


def test_glossary_conflict_preflight_is_a_read_only_snapshot():
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d) / "glossary-preflight")
        fg.add(
            problem_id="P",
            author="w",
            statement="Q_X is fixed",
            proof="definition proof",
            glossary_introduces={"Q_X": "the established project object"},
        )

        def graph_bytes() -> dict[str, bytes]:
            return {
                str(path.relative_to(fg.dir)): path.read_bytes()
                for path in sorted(fg.dir.rglob("*"))
                if path.is_file()
            }

        before = graph_bytes()
        assert fg.glossary_conflicts(
            {
                "Q_X": "a conflicting project object",
                "Q_Y": "a new compatible project object",
            }
        ) == ["Q_X"]
        assert fg.glossary_conflicts({"Q_X": "the established project object"}) == []
        assert graph_bytes() == before


def test_factgraph_mutation_lock_serializes_add_and_revoke():
    """A revoke started during atomic compare+add must include the new child."""
    import threading
    import time

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "locked-graph"
        fg = FactGraph(root)
        predecessor = fg.add(problem_id="P", author="w", statement="A", proof="proof A")
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
        refs = [
            {
                "key": "HL26",
                "authors": ["Han", "Liu"],
                "title": "On X",
                "arxiv": "2603.03817",
                "year": 2026,
                "cited_for": "Theorem 1.2",
            }
        ]

        # 1) BACKWARD COMPAT (load-bearing): external_refs is NOT hashed, so adding
        #    refs does not change the fact_id, and the id equals the bare compute_fact_id.
        bare = compute_fact_id(
            problem_id="P",
            predecessors=[],
            glossary_introduces={},
            statement="A holds",
            proof="proof of A",
        )
        fid_a = fg.add(
            problem_id="P",
            author="w",
            statement="A holds",
            proof="proof of A",
            external_refs=refs,
        )
        assert fid_a == bare, "external_refs must not change the fact_id"

        # 2) refs round-trip through serialize/parse and the read helper
        assert fg.external_refs(fid_a) == refs
        assert parse_frontmatter(fg.get_raw(fid_a))["external_refs"] == refs
        assert "external_refs:" in fg.get_raw(fid_a)

        # 3) same content + no refs => SAME id (dedup); re-adding is idempotent
        fid_a2 = fg.add(
            problem_id="P", author="w", statement="A holds", proof="proof of A"
        )
        assert fid_a2 == fid_a

        # 4) a fact written without refs reads back as [] (and old-format files too)
        fid_b = fg.add(
            problem_id="P", author="w", statement="B holds", proof="proof of B"
        )
        assert fg.external_refs(fid_b) == []
        legacy = (
            "---\nfact_id: deadbeefdeadbeef\nproblem_id: P\nauthor: w\n"
            "predecessors: []\nglossary_introduces: {}\n---\n\n## statement\nx\n\n## proof\ny\n"
        )
        assert (
            parse_frontmatter(legacy)["external_refs"] == []
        )  # no field -> default []

        # 5) set_external_refs (the auditor's path): rewrites refs, preserves id + body
        body_before = fg.get_raw(fid_b).split("## statement", 1)[1]
        out = fg.set_external_refs(fid_b, refs)
        assert out == refs and fg.external_refs(fid_b) == refs
        assert fg.exists(fid_b)  # id/file unchanged
        assert (
            fg.get_raw(fid_b).split("## statement", 1)[1] == body_before
        )  # body untouched

        # 6) normalization: non-dict entries dropped, canonical key order, [] for empty
        assert clean_external_refs([{"title": "T", "key": "K"}, "junk", 7]) == [
            {"key": "K", "title": "T"}
        ]
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
    test_global_memory_exact_get_is_bounded_and_unambiguous()
    print("  [ok] global memory exact get: bounded / unique / strict id")
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
    print(
        "  [ok] external_refs: not hashed (backward compat) + round-trip + auditor rewrite"
    )
    print("ALL CORE TESTS PASSED")


if __name__ == "__main__":
    main()
