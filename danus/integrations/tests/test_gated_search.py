"""Offline tests for gated_search — mocked HTTP, no network, no clock deps.

Covers the boundary cases that decide whether the gate is real:
  * the 2007-04 arXiv identifier change (naive int(YYMM) compare is wrong)
  * whole-month drop, not just the source ID
  * undatable / missing IDs fail closed
  * mode=open is refused while an eval cutoff is set
  * over-fetch + recall-saturation telemetry
  * audit ledger records every call and every dropped ID
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error

import pytest

try:  # in-repo
    from danus.integrations import gated_search as gs
except ImportError:  # standalone checkout of the module
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import gated_search as gs  # type: ignore


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _cfg(**kw):
    base = dict(mode="dated", cutoff="2603", source_ids="", run_id="t", audit_path="", overfetch=5)
    base.update(kw)
    return gs.GateConfig(**base)


def _mock_urlopen(payload, monkeypatch, capture=None):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner, n=-1):
            b = json.dumps(payload).encode("utf-8")
            return b if n is None or n < 0 else b[:n]

    def fake(req, timeout=None):
        if capture is not None:
            capture.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(gs.urllib.request, "urlopen", fake)


def _arxiv_hit(aid, thm="T", title="P"):
    return {"title": title, "theorem": thm, "arxiv_id": aid, "theorem_id": "thm:1"}


# --------------------------------------------------------------------------- #
# arxiv_month / parse_cutoff                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("aid,y,m", [
    ("2603.01234", 2026, 3),
    ("2603.01234v7", 2026, 3),
    ("arXiv:2512.09999", 2025, 12),
    ("0704.00001", 2007, 4),
    ("math.NT/0605123", 2006, 5),
    ("math/9107001", 1991, 7),
    ("hep-th/9912001v2", 1999, 12),
])
def test_arxiv_month_parses(aid, y, m):
    assert gs.arxiv_month(aid) == y * 12 + (m - 1)


@pytest.mark.parametrize("aid", ["", "   ", "not-an-id", "2613.00001", "2600.00001",
                                 "26031234", "2603.123", "math.NT/0613123"])
def test_arxiv_month_rejects(aid):
    assert gs.arxiv_month(aid) is None


def test_century_boundary_is_not_a_naive_int_compare():
    """int('9107') > int('2603') — a naive compare passes a 1991 paper as
    'after' a 2026 cutoff and would invert the whole gate."""
    old = gs.arxiv_month("math/9107001")   # 1991-07
    new = gs.arxiv_month("2603.00001")     # 2026-03
    assert old < new
    assert int("9107") > int("2603")       # the trap this guards against


@pytest.mark.parametrize("raw,expect", [
    ("2603", 2026 * 12 + 2),
    ("2026-03", 2026 * 12 + 2),
    ("2026/3", 2026 * 12 + 2),
    ("202603", 2026 * 12 + 2),
])
def test_parse_cutoff(raw, expect):
    assert gs.parse_cutoff(raw) == expect


@pytest.mark.parametrize("raw", ["", "20263", "2026-13", "march", "0000"])
def test_parse_cutoff_rejects(raw):
    assert gs.parse_cutoff(raw) is None


# --------------------------------------------------------------------------- #
# mode resolution / fail-closed                                                #
# --------------------------------------------------------------------------- #

def test_unset_mode_is_open_without_cutoff(monkeypatch):
    for k in ("DANUS_RETRIEVAL_MODE", "DANUS_EVAL_CUTOFF", "DANUS_EVAL_SOURCE_ID",
              "DANUS_EVAL_RUN_ID", "DANUS_RETRIEVAL_AUDIT"):
        monkeypatch.delenv(k, raising=False)
    assert gs.GateConfig().mode == "open"


def test_unset_mode_fails_closed_to_off_under_eval(monkeypatch):
    monkeypatch.delenv("DANUS_RETRIEVAL_MODE", raising=False)
    monkeypatch.setenv("DANUS_EVAL_CUTOFF", "2603")
    assert gs.GateConfig().mode == "off"


def test_open_mode_is_refused_under_eval(monkeypatch):
    out = gs.search("q", config=_cfg(mode="open"))
    assert out["gate_fatal"] is True
    assert out["results"] == []
    assert "refused" in out["error"]


def test_dated_without_cutoff_is_refused():
    out = gs.search("q", config=_cfg(mode="dated", cutoff=""))
    assert out["gate_fatal"] is True
    assert out["results"] == []


def test_unparseable_cutoff_is_refused():
    out = gs.search("q", config=_cfg(cutoff="march-2026"))
    assert out["gate_fatal"] is True


def test_invalid_mode_is_refused():
    out = gs.search("q", config=_cfg(mode="yolo", cutoff=""))
    assert out["gate_fatal"] is True


def test_off_mode_returns_nothing_and_says_so():
    out = gs.search("q", config=_cfg(mode="off"))
    assert out["count"] == 0 and out["results"] == []
    assert "disabled" in out["error"]


def test_off_mode_never_touches_the_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("mode=off must not open a socket")
    monkeypatch.setattr(gs.urllib.request, "urlopen", boom)
    gs.search("q", config=_cfg(mode="off"))


# --------------------------------------------------------------------------- #
# dated gate                                                                   #
# --------------------------------------------------------------------------- #

def test_whole_cutoff_month_is_dropped_not_just_the_source(monkeypatch):
    _mock_urlopen([
        _arxiv_hit("2603.00001"),          # the source paper
        _arxiv_hit("2603.09999"),          # a same-month companion — the real leak path
        _arxiv_hit("2602.00001"),          # before cutoff, keep
    ], monkeypatch)
    out = gs.search("q", num_results=10, config=_cfg(source_ids="2603.00001"))
    assert [r["arxiv_id"] for r in out["results"]] == ["2602.00001"]
    assert out["dropped_by_gate"] == 2
    assert out["run_invalid"] is True


def test_source_id_blocked_in_any_month(monkeypatch):
    _mock_urlopen([_arxiv_hit("2201.00001v3"), _arxiv_hit("2202.00002")], monkeypatch)
    out = gs.search("q", config=_cfg(source_ids="arXiv:2201.00001", audit_path=""))
    assert [r["arxiv_id"] for r in out["results"]] == ["2202.00002"]
    assert out["gate_violations"] == 1 and out["run_invalid"] is True


def test_undatable_id_fails_closed(monkeypatch, tmp_path):
    ledger = tmp_path / "a.jsonl"
    _mock_urlopen([_arxiv_hit(""), _arxiv_hit("garbage"), _arxiv_hit("2601.00001")], monkeypatch)
    out = gs.search("q", config=_cfg(audit_path=str(ledger)))
    assert [r["arxiv_id"] for r in out["results"]] == ["2601.00001"]
    assert out["dropped_by_gate"] == 2
    rec = json.loads(ledger.read_text().strip())
    assert {d["reason"] for d in rec["dropped"]} == {"undatable_id"}


def test_envelope_never_echoes_blocked_ids(monkeypatch):
    """The envelope reaches the model's context. Handing back the blocked
    arxiv_id would leak the source paper's identifier — counts only."""
    _mock_urlopen([_arxiv_hit("2603.00001"), _arxiv_hit("2601.00001")], monkeypatch)
    out = gs.search("q", config=_cfg(source_ids="2603.00001"))
    blob = json.dumps(out)
    assert "2603.00001" not in blob
    assert out["gate_violations"] == 1 and out["run_invalid"] is True


def test_undatable_ids_are_not_counted_as_violations(monkeypatch):
    """An unparseable ID is dropped, but it is not evidence the index holds the
    source paper — only a real cutoff/source hit invalidates the run."""
    _mock_urlopen([_arxiv_hit("garbage"), _arxiv_hit("2601.00001")], monkeypatch)
    out = gs.search("q", config=_cfg())
    assert out["dropped_by_gate"] == 1
    assert "run_invalid" not in out
    assert "gate_violations" not in out


def test_boundary_month_is_exclusive(monkeypatch):
    _mock_urlopen([_arxiv_hit("2602.99999"), _arxiv_hit("2603.00001")], monkeypatch)
    out = gs.search("q", config=_cfg(cutoff="2603"))
    assert [r["arxiv_id"] for r in out["results"]] == ["2602.99999"]


# --------------------------------------------------------------------------- #
# over-fetch + recall telemetry                                                #
# --------------------------------------------------------------------------- #

def test_overfetch_multiplies_the_request(monkeypatch):
    seen = []
    _mock_urlopen([], monkeypatch, capture=seen)
    gs.search("q", num_results=10, config=_cfg(overfetch=5))
    assert seen[0]["num_results"] == 50


def test_open_mode_does_not_overfetch(monkeypatch):
    seen = []
    _mock_urlopen([], monkeypatch, capture=seen)
    gs.search("q", num_results=10, config=_cfg(mode="open", cutoff=""))
    assert seen[0]["num_results"] == 10


def test_recall_saturation_is_flagged(monkeypatch):
    # ask for 5, over-fetch 25, but only 2 survive the gate -> effective k = 2
    _mock_urlopen([_arxiv_hit("2603.0000%d" % i) for i in range(9)] +
                  [_arxiv_hit("2601.00001"), _arxiv_hit("2601.00002")], monkeypatch)
    out = gs.search("q", num_results=5, config=_cfg())
    assert out["count"] == 2
    assert out["recall_saturated"] is True


def test_no_saturation_flag_when_k_is_met(monkeypatch):
    _mock_urlopen([_arxiv_hit("2601.0000%d" % i) for i in range(9)], monkeypatch)
    out = gs.search("q", num_results=5, config=_cfg())
    assert out["count"] == 5
    assert "recall_saturated" not in out


# --------------------------------------------------------------------------- #
# strict / matlas.ai                                                           #
# --------------------------------------------------------------------------- #

def test_strict_hits_matlas_ai_and_maps_the_schema(monkeypatch):
    seen = []
    _mock_urlopen([{
        "type": "paper", "entity_name": "Theorem 2.1", "doi": "10.1000/xyz",
        "title": "On something", "authors": "A. Author", "journal": "Ann. Math.",
        "year": "2011", "statement": "Every X is Y.", "candidate_id": "c1",
    }], monkeypatch, capture=seen)
    out = gs.search("q", num_results=3, config=_cfg(mode="strict"))
    r = out["results"][0]
    assert out["endpoint"] == gs.MATLAS_AI_URL
    assert r["theorem"] == "Every X is Y."      # statement -> theorem
    assert r["theorem_id"] == "Theorem 2.1"     # entity_name -> theorem_id
    assert r["arxiv_id"] == ""                  # no arXiv in this corpus
    assert r["doi"] == "10.1000/xyz" and r["journal"] == "Ann. Math."
    assert set(gs.RESULT_FIELDS) <= set(r)


def test_strict_clamps_num_results_to_the_matlas_ai_range(monkeypatch):
    seen = []
    _mock_urlopen([], monkeypatch, capture=seen)
    gs.search("q", num_results=1, config=_cfg(mode="strict"))
    assert seen[0]["num_results"] == 10          # API minimum
    seen.clear()
    gs.search("q", num_results=100, config=_cfg(mode="strict", overfetch=5))
    assert seen[0]["num_results"] == 200         # API maximum


def test_strict_year_guard_drops_at_or_after_cutoff_year(monkeypatch):
    _mock_urlopen([
        {"year": "2026", "doi": "d1", "statement": "s", "title": "t", "entity_name": "e"},
        {"year": "2011", "doi": "d2", "statement": "s", "title": "t", "entity_name": "e"},
        {"year": "", "doi": "d3", "statement": "s", "title": "t", "entity_name": "e"},
    ], monkeypatch)
    out = gs.search("q", num_results=10, config=_cfg(mode="strict"))
    assert [r["doi"] for r in out["results"]] == ["d2"]


# --------------------------------------------------------------------------- #
# error handling — the contract is "never raises"                              #
# --------------------------------------------------------------------------- #

def test_http_error_degrades(monkeypatch):
    exc = urllib.error.HTTPError(url=gs.ARXIV_INDEX_URL, code=403, msg="Forbidden",
                                 hdrs=None, fp=None)
    monkeypatch.setattr(gs.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(exc))
    out = gs.search("q", config=_cfg())
    assert out["results"] == [] and "http 403" in out["error"]


def test_network_error_degrades(monkeypatch):
    monkeypatch.setattr(gs.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")))
    out = gs.search("q", config=_cfg())
    assert out["results"] == [] and out["error"].startswith("network:")


def test_non_list_body_degrades(monkeypatch):
    _mock_urlopen({"oops": 1}, monkeypatch)
    out = gs.search("q", config=_cfg())
    assert out["results"] == [] and "JSON list" in out["error"]


def test_empty_query_short_circuits():
    out = gs.search("   ", config=_cfg())
    assert out["count"] == 0 and out["error"] == "empty query"


def test_non_numeric_num_results_never_raises(monkeypatch):
    seen = []
    _mock_urlopen([], monkeypatch, capture=seen)
    out = gs.search(
        "q",
        num_results="not-a-number",
        config=_cfg(mode="open", cutoff=""),
    )
    assert out["results"] == []
    assert seen[0]["num_results"] == 10


def test_oversized_result_field_rejects_the_response(monkeypatch):
    monkeypatch.setattr(gs, "_MAX_FIELD_CHARS", 4)
    _mock_urlopen([_arxiv_hit("2601.00001", thm="too long")], monkeypatch)
    out = gs.search("q", config=_cfg())
    assert out["results"] == []
    assert "field theorem exceeds" in out["error"]


# --------------------------------------------------------------------------- #
# audit ledger                                                                 #
# --------------------------------------------------------------------------- #

def test_audit_records_every_dropped_id(monkeypatch, tmp_path):
    ledger = tmp_path / "audit.jsonl"
    _mock_urlopen([_arxiv_hit("2603.00001"), _arxiv_hit("2601.00001")], monkeypatch)
    gs.search("q", config=_cfg(audit_path=str(ledger), run_id="run-7"))
    rec = json.loads(ledger.read_text().strip())
    assert rec["run_id"] == "run-7" and rec["mode"] == "dated" and rec["cutoff"] == "2603"
    assert rec["violations"] == 1
    assert {d["arxiv_id"] for d in rec["dropped"]} == {"2603.00001"}
    assert rec["kept"] == 1 and "query_sha" in rec and "ts" in rec


def test_audit_appends(monkeypatch, tmp_path):
    ledger = tmp_path / "audit.jsonl"
    _mock_urlopen([_arxiv_hit("2601.00001")], monkeypatch)
    cfg = _cfg(audit_path=str(ledger))
    gs.search("a", config=cfg)
    gs.search("b", config=cfg)
    assert len(ledger.read_text().strip().splitlines()) == 2


def test_audit_records_misconfiguration(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    gs.search("q", config=_cfg(mode="open", audit_path=str(ledger)))
    assert json.loads(ledger.read_text().strip())["event"] == "gate_misconfigured"


def test_audit_failure_does_not_kill_the_call(monkeypatch, tmp_path):
    _mock_urlopen([_arxiv_hit("2601.00001")], monkeypatch)
    out = gs.search("q", config=_cfg(audit_path=str(tmp_path / "nope" / "a.jsonl")))
    assert out["count"] == 1


# --------------------------------------------------------------------------- #
# digest                                                                       #
# --------------------------------------------------------------------------- #

def test_digest_is_stable_and_content_addressed(monkeypatch):
    _mock_urlopen([_arxiv_hit("2601.00001", thm="A")], monkeypatch)
    a = gs.search("q", config=_cfg())
    _mock_urlopen([_arxiv_hit("2601.00001", thm="A")], monkeypatch)
    b = gs.search("q", config=_cfg())
    _mock_urlopen([_arxiv_hit("2601.00001", thm="B")], monkeypatch)
    c = gs.search("q", config=_cfg())
    assert a["digest"] == b["digest"] != c["digest"]


# --------------------------------------------------------------------------- #
# response size cap (danus_review [PS2] — inherited call shape)                #
# --------------------------------------------------------------------------- #

def test_oversized_response_is_rejected_not_truncated(monkeypatch):
    big = json.dumps([_arxiv_hit("2601.00001", thm="x" * (gs._MAX_BODY_BYTES + 64))])

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1):
            b = big.encode()
            return b if n is None or n < 0 else b[:n]

    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = gs.search("q", config=_cfg())
    assert out["results"] == []
    assert "exceeds" in out["error"]


def test_body_exactly_at_cap_is_accepted(monkeypatch):
    payload = json.dumps([_arxiv_hit("2601.00001")]).encode()
    monkeypatch.setattr(gs, "_MAX_BODY_BYTES", len(payload))

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1):
            return payload if n is None or n < 0 else payload[:n]

    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = gs.search("q", config=_cfg())
    assert out["count"] == 1


def test_strict_keeps_undated_books_but_still_fails_papers_closed(monkeypatch):
    """matlas.ai returns every `type: "book"` row with an empty `year`. Failing
    those closed deleted the whole textbook corpus for no contamination benefit;
    an undated *paper* is still dropped."""
    _mock_urlopen([
        {"type": "book", "year": "", "doi": "", "statement": "s", "title": "Classical Fourier Analysis", "entity_name": "Thm 1"},
        {"type": "paper", "year": "", "doi": "d2", "statement": "s", "title": "t", "entity_name": "e"},
        {"type": "paper", "year": "2011", "doi": "d3", "statement": "s", "title": "t", "entity_name": "e"},
        {"type": "paper", "year": "2026", "doi": "d4", "statement": "s", "title": "t", "entity_name": "e"},
    ], monkeypatch)
    out = gs.search("q", num_results=10, config=_cfg(mode="strict"))
    kept = [(r["source_type"], r["doi"]) for r in out["results"]]
    assert kept == [("book", ""), ("paper", "d3")]
    assert out["dropped_by_gate"] == 2
