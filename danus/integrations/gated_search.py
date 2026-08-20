"""Gated literature retrieval — the single contamination chokepoint for evals.

Why this module exists
----------------------
``danus.integrations.matlas`` is the only outbound network call in ``danus/``.
It is *named* Matlas but its default endpoint is ``leansearch.net/thm/search``,
an **arXiv** theorem index with **no date parameter**, returning verbatim
statements tagged with ``arxiv_id``.  Every MathArena arXiv-derived benchmark
(``arxivmath``, ``arxivlean``, ``brokenarxiv``) is built from arXiv papers
posted in a known month.  Running those benchmarks through an unfiltered arXiv
index means the source paper's own theorem can be handed to the agent verbatim.

This module wraps that call with a fail-closed gate.  It is a drop-in for
``matlas.search`` — same envelope, same "never raises" contract — so
``gateway/server.py:search_arxiv_theorems`` only has to change its import.

Modes (``DANUS_RETRIEVAL_MODE``)
-------------------------------
``open``    legacy behaviour, no filtering.  **Rejected when an eval cutoff is
            set** — that combination is the exact mistake this module exists to
            prevent, so it fails closed instead of silently running ungated.
``strict``  matlas.ai only.  Corpus is 8.07M statements from 435K peer-reviewed
            papers in 180 curated journals (1826-2025) + 1.9K textbooks, and
            **deliberately excludes arXiv** (arXiv:2604.17484).  A 2026 preprint
            is structurally absent; the ``year`` guard below is defence in depth.
``dated``   arXiv index, month-granularity gate.  arXiv IDs encode ``YYMM``, so
            the cutoff is enforced offline with no date oracle and no extra
            request.  The **whole cutoff month is dropped**, not just the source
            ID: a same-month companion paper by the same authors is the most
            common leak path.
``off``     zero-retrieval control arm.  Required — without it you cannot show
            that a gated score is not a retrieved score.

Environment
-----------
``DANUS_RETRIEVAL_MODE``   open | strict | dated | off.  Unset => ``open`` when
                           no cutoff is configured (production unchanged),
                           ``off`` when a cutoff is set (evals fail closed).
``DANUS_EVAL_CUTOFF``      ``YYMM`` / ``YYYY-MM`` / ``YYYYMM``.  Presence of this
                           variable is what marks a run as an eval run.
``DANUS_EVAL_SOURCE_ID``   arXiv ID (or comma-separated list) of the problem's
                           source paper(s); blocked regardless of month.
``DANUS_EVAL_RUN_ID``      opaque run identifier recorded in the audit ledger.
``DANUS_RETRIEVAL_AUDIT``  path to an append-only JSONL audit ledger.
``DANUS_RETRIEVAL_OVERFETCH``  over-fetch multiplier (default 5).  Post-filtering
                           without over-fetching silently halves recall.
``DANUS_ARXIV_INDEX_URL``  arXiv-index endpoint for ``dated``/``open``.
``DANUS_MATLAS_URL``       matlas.ai endpoint for ``strict``.

Contract
--------
``search()`` never raises.  It returns
``{query, count, results, endpoint, mode, digest[, error][, gate]}`` where each
result always carries the legacy ``("title", "theorem", "arxiv_id",
"theorem_id")`` keys, plus provenance keys when the provider supplies them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# constants                                                                    #
# --------------------------------------------------------------------------- #

#: arXiv theorem index (verbatim statements + arxiv_id).  NOT matlas.ai.
ARXIV_INDEX_URL = os.environ.get(
    "DANUS_ARXIV_INDEX_URL", "https://leansearch.net/thm/search"
)

#: matlas.ai — peer-reviewed journals + textbooks, arXiv deliberately excluded.
MATLAS_AI_URL = os.environ.get(
    "DANUS_MATLAS_URL", "https://matlas.ai/api/search"
)

#: Legacy normalized fields.  Always present on every result, in every mode.
RESULT_FIELDS: Tuple[str, ...] = ("title", "theorem", "arxiv_id", "theorem_id")

#: Extra provenance fields matlas.ai supplies (absent from the arXiv index).
PROVENANCE_FIELDS: Tuple[str, ...] = ("doi", "journal", "year", "authors", "source_type")

_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)

_DEFAULT_TIMEOUT = 30
_UA = "danus-gated/1.0 (+https://frenzymath.com)"

#: Hard cap on an external response body.  ``danus_review_2026-08-18.md`` [PS2]
#: flags the original ``matlas.py`` for reading an unbounded external body:
#: memory-exhaustion DoS plus prompt-injection amplification.  This module
#: inherited the same call shape, so the cap is enforced here rather than
#: repeating the finding.  Override with ``DANUS_RETRIEVAL_MAX_BYTES``.
try:
    _MAX_BODY_BYTES = int(os.environ.get("DANUS_RETRIEVAL_MAX_BYTES", "8388608"))
except ValueError:
    _MAX_BODY_BYTES = 8 * 1024 * 1024
if not 1 <= _MAX_BODY_BYTES <= 64 * 1024 * 1024:
    _MAX_BODY_BYTES = 8 * 1024 * 1024

_MAX_RESULTS = 200
_MAX_FIELD_CHARS = 262_144

_VALID_MODES = ("open", "strict", "dated", "off")

# matlas.ai/api/search constrains num_results to [10, 200].
_MATLAS_AI_MIN, _MATLAS_AI_MAX = 10, 200

# New-style arXiv IDs: YYMM.NNNNN[vN], optional "arXiv:" prefix.
_RE_NEW = re.compile(r"^(?:arxiv:)?\s*(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?\s*$", re.I)
# Old-style arXiv IDs: archive[.SUBJ]/YYMMNNN[vN], e.g. math.NT/0605123.
_RE_OLD = re.compile(r"^(?:arxiv:)?\s*[a-z-]+(?:\.[a-z]{2})?/(\d{2})(\d{2})(\d{3})(?:v\d+)?\s*$", re.I)


# --------------------------------------------------------------------------- #
# arXiv ID -> absolute month index                                             #
# --------------------------------------------------------------------------- #

def _month_index(yy: int, mm: int) -> Optional[int]:
    """Absolute month index for a 2-digit arXiv year.

    A naive ``int("YYMM")`` comparison is WRONG across the 2007-04 identifier
    change: old-style IDs start at ``9107`` (July 1991), so ``9107 > 2603``
    would let a 1991 paper through a 2026 cutoff — or, worse, block nothing.
    arXiv's identifier space runs 1991-2090, so ``yy >= 91`` is the 1900s.
    """
    if not 1 <= mm <= 12:
        return None
    year = 1900 + yy if yy >= 91 else 2000 + yy
    return year * 12 + (mm - 1)


def arxiv_month(arxiv_id: str) -> Optional[int]:
    """Absolute month index of ``arxiv_id``, or ``None`` if unparseable.

    ``None`` is a *drop* signal, never a pass signal — see ``_gate_arxiv``.
    """
    s = (arxiv_id or "").strip()
    if not s:
        return None
    for rx in (_RE_NEW, _RE_OLD):
        m = rx.match(s)
        if m:
            return _month_index(int(m.group(1)), int(m.group(2)))
    return None


def parse_cutoff(raw: str) -> Optional[int]:
    """Parse ``YYMM`` / ``YYYY-MM`` / ``YYYY/MM`` / ``YYYYMM`` to a month index."""
    s = (raw or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return y * 12 + (mo - 1) if 1 <= mo <= 12 else None
    if re.match(r"^\d{6}$", s):  # YYYYMM
        y, mo = int(s[:4]), int(s[4:])
        return y * 12 + (mo - 1) if 1 <= mo <= 12 else None
    if re.match(r"^\d{4}$", s):  # YYMM, arXiv style
        return _month_index(int(s[:2]), int(s[2:]))
    return None


def _normalize_id(arxiv_id: str) -> str:
    """Version-stripped, lowercased ID for source-paper equality checks."""
    s = (arxiv_id or "").strip().lower()
    s = re.sub(r"^arxiv:\s*", "", s)
    return re.sub(r"v\d+$", "", s)


# --------------------------------------------------------------------------- #
# configuration                                                                #
# --------------------------------------------------------------------------- #

class GateConfig:
    """Resolved gate configuration.  Built from env; explicit args win in tests."""

    def __init__(
        self,
        mode: Optional[str] = None,
        cutoff: Optional[str] = None,
        source_ids: Optional[str] = None,
        run_id: Optional[str] = None,
        audit_path: Optional[str] = None,
        overfetch: Optional[int] = None,
    ) -> None:
        env = os.environ.get
        raw_cutoff = cutoff if cutoff is not None else env("DANUS_EVAL_CUTOFF", "")
        self.cutoff_raw = (raw_cutoff or "").strip()
        self.cutoff = parse_cutoff(self.cutoff_raw)
        self.is_eval = bool(self.cutoff_raw)

        raw_mode = (mode if mode is not None else env("DANUS_RETRIEVAL_MODE", "")).strip().lower()
        if not raw_mode:
            # Production keeps its legacy behaviour; an eval run without an
            # explicit mode fails closed to zero retrieval rather than to open.
            raw_mode = "off" if self.is_eval else "open"
        self.mode = raw_mode

        raw_src = source_ids if source_ids is not None else env("DANUS_EVAL_SOURCE_ID", "")
        self.source_ids = {
            _normalize_id(p) for p in re.split(r"[,\s]+", raw_src or "") if p.strip()
        }
        self.run_id = (run_id if run_id is not None else env("DANUS_EVAL_RUN_ID", "")) or ""
        self.audit_path = audit_path if audit_path is not None else env("DANUS_RETRIEVAL_AUDIT", "")
        try:
            self.overfetch = int(
                overfetch if overfetch is not None else env("DANUS_RETRIEVAL_OVERFETCH", "5")
            )
        except (TypeError, ValueError):
            self.overfetch = 5
        if self.overfetch < 1:
            self.overfetch = 1

    def validate(self) -> Optional[str]:
        """Return a fatal misconfiguration message, or ``None`` if usable."""
        if self.mode not in _VALID_MODES:
            return f"invalid DANUS_RETRIEVAL_MODE {self.mode!r} (expected one of {_VALID_MODES})"
        if self.is_eval and self.cutoff is None:
            return f"unparseable DANUS_EVAL_CUTOFF {self.cutoff_raw!r} (expected YYMM / YYYY-MM)"
        if self.is_eval and self.mode == "open":
            return "DANUS_RETRIEVAL_MODE=open is refused while DANUS_EVAL_CUTOFF is set"
        if self.mode == "dated" and self.cutoff is None:
            return "mode=dated requires DANUS_EVAL_CUTOFF"
        return None


# --------------------------------------------------------------------------- #
# audit ledger                                                                 #
# --------------------------------------------------------------------------- #

def _audit(cfg: GateConfig, record: Dict[str, Any]) -> None:
    """Append one JSONL audit record.  Never raises — auditing must not be able
    to kill a run, and a lost line is visible as a gap against the run's own
    call counter."""
    if not cfg.audit_path:
        return
    record = dict(record)
    record["ts"] = datetime.now(timezone.utc).isoformat()
    record["run_id"] = cfg.run_id
    record["role"] = os.environ.get("DANUS_ROLE", "unknown")
    record["mode"] = cfg.mode
    record["cutoff"] = cfg.cutoff_raw
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(
            cfg.audit_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
            ):
                return
            raw = line.encode("utf-8")
            offset = 0
            while offset < len(raw):
                offset += os.write(fd, raw[offset:])
        finally:
            os.close(fd)
    except OSError:
        pass


def _digest(results: List[Dict[str, Any]]) -> str:
    """Content address over the kept results — lets a retrieval round be pinned
    into the run receipt / fact graph and replayed."""
    blob = json.dumps(results, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# transport                                                                    #
# --------------------------------------------------------------------------- #

def _post_json(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[Any, Optional[str]]:
    """POST JSON, return ``(data, error)``.  Never raises."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Both endpoints sit behind Cloudflare, which 403s urllib's default
            # bare request; an explicit User-Agent + Accept gets through.
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            # Read one byte past the cap so an exactly-at-cap body is not
            # mistaken for an over-cap one, and reject rather than truncate:
            # a truncated JSON body is a parse error anyway, and silently
            # accepting a prefix would let a hostile endpoint choose what we see.
            body = resp.read(_MAX_BODY_BYTES + 1)
        if len(body) > _MAX_BODY_BYTES:
            return None, f"response exceeds {_MAX_BODY_BYTES} bytes (cap; body rejected unread)"
        return json.loads(body.decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"network: {e.reason}"
    except (TimeoutError, json.JSONDecodeError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def _envelope(
    query: str,
    endpoint: str,
    mode: str,
    results: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    res = results or []
    out: Dict[str, Any] = {
        "query": query,
        "count": len(res),
        "results": res,
        "endpoint": endpoint,
        "mode": mode,
        "digest": _digest(res),
    }
    if error:
        out["error"] = error
    out.update(extra)
    return out


# --------------------------------------------------------------------------- #
# providers                                                                    #
# --------------------------------------------------------------------------- #

def _normalize_arxiv_item(item: Dict[str, Any]) -> Dict[str, Any]:
    def field(name: str) -> str:
        value = str(item.get(name, ""))
        if len(value) > _MAX_FIELD_CHARS:
            raise ValueError(f"arXiv result field {name} exceeds its character cap")
        return value

    return {
        "title": field("title"),
        "theorem": field("theorem"),
        "arxiv_id": field("arxiv_id"),
        "theorem_id": field("theorem_id"),
        "source_type": "arxiv",
    }


def _normalize_matlas_ai_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """matlas.ai returns a different shape than the arXiv index.

    Mapping: ``statement`` -> ``theorem``, ``entity_name`` -> ``theorem_id``.
    There is no arXiv ID in this corpus by construction, so ``arxiv_id`` is ""
    and the DOI carries provenance.  The write-paper reference verifier must
    therefore branch on ``source_type`` rather than assuming ``arxiv_id``.
    """
    def value(raw: Any, *, name: str) -> str:
        normalized = str(raw or "")
        if len(normalized) > _MAX_FIELD_CHARS:
            raise ValueError(f"Matlas result field {name} exceeds its character cap")
        return normalized

    return {
        "title": value(item.get("title"), name="title"),
        "theorem": value(item.get("statement"), name="statement"),
        "arxiv_id": "",
        "theorem_id": value(
            item.get("entity_name") or item.get("candidate_id"), name="theorem_id"
        ),
        "doi": value(item.get("doi"), name="doi"),
        "journal": value(item.get("journal"), name="journal"),
        "year": value(item.get("year"), name="year"),
        "authors": value(item.get("authors"), name="authors"),
        "source_type": value(item.get("type"), name="type") or "matlas",
    }


def _fetch_arxiv_index(query: str, n: int, timeout: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    data, err = _post_json(
        ARXIV_INDEX_URL, {"query": query, "task": _TASK, "num_results": n}, timeout
    )
    if err:
        return [], err
    if not isinstance(data, list):
        return [], f"theorem endpoint must return a JSON list, got {type(data).__name__}"
    try:
        return [
            _normalize_arxiv_item(i)
            for i in data[:_MAX_RESULTS]
            if isinstance(i, dict)
        ], None
    except ValueError as exc:
        return [], str(exc)


def _fetch_matlas_ai(query: str, n: int, timeout: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    n = max(_MATLAS_AI_MIN, min(_MATLAS_AI_MAX, n))
    data, err = _post_json(MATLAS_AI_URL, {"query": query, "num_results": n}, timeout)
    if err:
        return [], err
    if not isinstance(data, list):
        return [], f"matlas.ai must return a JSON list, got {type(data).__name__}"
    try:
        return [
            _normalize_matlas_ai_item(i)
            for i in data[:_MAX_RESULTS]
            if isinstance(i, dict)
        ], None
    except ValueError as exc:
        return [], str(exc)


# --------------------------------------------------------------------------- #
# gates                                                                        #
# --------------------------------------------------------------------------- #

def _gate_arxiv(
    raw: List[Dict[str, Any]], cfg: GateConfig
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Month-granularity arXiv gate.

    Drops, fail-closed:
      * ``month >= cutoff``  — the WHOLE cutoff month, not just the source ID.
        A same-month companion / longer version by the same authors is the
        single most common leak path and shares the source's ``YYMM``.
      * ``normalized id in source_ids`` — belt and braces, any month.
      * unparseable or missing ``arxiv_id`` — an ID we cannot date is an ID we
        cannot clear.
    """
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    assert cfg.cutoff is not None
    for item in raw:
        aid = item.get("arxiv_id", "")
        norm = _normalize_id(aid)
        month = arxiv_month(aid)
        if norm and norm in cfg.source_ids:
            dropped.append({"arxiv_id": aid, "reason": "source_paper"})
        elif month is None:
            dropped.append({"arxiv_id": aid, "reason": "undatable_id"})
        elif month >= cfg.cutoff:
            dropped.append({"arxiv_id": aid, "reason": "at_or_after_cutoff"})
        else:
            kept.append(item)
    return kept, dropped


def _gate_matlas_ai(
    raw: List[Dict[str, Any]], cfg: GateConfig
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Year guard on the matlas.ai corpus.

    The corpus is peer-reviewed journals through 2025 and excludes arXiv, so
    this cannot fire for a 2026-sourced benchmark.  It is kept because "the
    corpus cannot contain it" is an assumption about someone else's index that
    should be enforced locally, not trusted.  Outside an eval run it is a no-op.
    """
    if not cfg.is_eval or cfg.cutoff is None:
        return list(raw), []
    cutoff_year = cfg.cutoff // 12
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    for item in raw:
        y = str(item.get("year", "")).strip()
        m = re.search(r"\d{4}", y)
        if m:
            if int(m.group(0)) >= cutoff_year:
                dropped.append({"doi": str(item.get("doi", "")), "reason": "at_or_after_cutoff"})
            else:
                kept.append(item)
        elif str(item.get("source_type", "")).strip().lower() == "book":
            # Measured against the live endpoint: EVERY ``type: "book"`` row comes
            # back with an empty ``year`` (10/10 of the books in a 400-row sample),
            # while every ``type: "paper"`` row carries one.  Failing books closed
            # therefore deleted 100% of the textbook corpus — the single safest and
            # often most useful material (definitions, standard techniques) — for
            # zero contamination benefit: a published textbook cannot be the source
            # of a 2026 preprint's result.  Papers still fail closed.
            kept.append(item)
        else:
            dropped.append({"doi": str(item.get("doi", "")), "reason": "undatable_year"})
    return kept, dropped


# --------------------------------------------------------------------------- #
# public surface                                                               #
# --------------------------------------------------------------------------- #

def search(
    query: str,
    num_results: int = 10,
    timeout: int = _DEFAULT_TIMEOUT,
    config: Optional[GateConfig] = None,
) -> Dict[str, Any]:
    """Gated literature search.  Drop-in for ``matlas.search``; never raises."""
    cfg = config or GateConfig()
    q = (query or "").strip()

    fatal = cfg.validate()
    if fatal:
        _audit(cfg, {"event": "gate_misconfigured", "detail": fatal})
        return _envelope(query, "", cfg.mode, error=f"gate refused: {fatal}", gate_fatal=True)

    if not q:
        return _envelope(query, "", cfg.mode, error="empty query")

    try:
        parsed_results = int(num_results)
    except (TypeError, ValueError):
        parsed_results = 10
    n = min(_MAX_RESULTS, parsed_results if parsed_results > 0 else 10)

    if cfg.mode == "off":
        _audit(cfg, {"event": "search", "query_sha": hashlib.sha256(q.encode()).hexdigest()[:16],
                     "requested": n, "returned": 0, "kept": 0, "dropped": []})
        return _envelope(
            query, "", "off",
            error="retrieval is disabled for this run (mode=off); answer from the problem statement alone",
        )

    # Over-fetch so that post-filtering does not silently shrink k.  The
    # endpoints have no date parameter, so filtering must happen here, in this
    # process, before any text reaches the model's context.
    want = n if cfg.mode == "open" else min(_MAX_RESULTS, n * cfg.overfetch)

    if cfg.mode == "strict":
        endpoint, raw, err = MATLAS_AI_URL, *_fetch_matlas_ai(q, want, timeout)
    else:  # open | dated
        endpoint, raw, err = ARXIV_INDEX_URL, *_fetch_arxiv_index(q, want, timeout)

    if err:
        _audit(cfg, {"event": "search_error", "endpoint": endpoint, "detail": err})
        return _envelope(query, endpoint, cfg.mode, error=err)

    if cfg.mode == "open":
        kept, dropped = raw, []
    elif cfg.mode == "strict":
        kept, dropped = _gate_matlas_ai(raw, cfg)
    else:
        kept, dropped = _gate_arxiv(raw, cfg)

    # A violation is not a leak — the item was dropped.  It is the diagnostic
    # that the index *does* carry the paper, i.e. that an ungated arm of the
    # same experiment was contaminated.  Surface it loudly.
    violations = [d for d in dropped if d.get("reason") in ("source_paper", "at_or_after_cutoff")]

    truncated = len(kept) > n
    out_results = kept[:n]

    _audit(cfg, {
        "event": "search",
        "endpoint": endpoint,
        "query_sha": hashlib.sha256(q.encode()).hexdigest()[:16],
        "requested": n,
        "overfetched": want,
        "returned": len(raw),
        "kept": len(out_results),
        "dropped": dropped,
        "violations": len(violations),
        "digest": _digest(out_results),
    })

    # The envelope goes into the model's context, so it carries COUNTS ONLY.
    # Echoing the blocked ``arxiv_id`` back would hand the agent the exact
    # identifier of the source paper — a smaller leak than the theorem text,
    # but the same kind, and pointless.  The IDs live in the audit ledger,
    # which the agent cannot read.
    extra: Dict[str, Any] = {"dropped_by_gate": len(dropped)}
    if violations:
        extra["gate_violations"] = len(violations)
        extra["run_invalid"] = True
    if not truncated and len(out_results) < n and cfg.mode != "open":
        # Every over-fetched candidate was consumed; effective k is below the
        # requested k.  Arms compared at different effective k are not comparable.
        extra["recall_saturated"] = True
    return _envelope(query, endpoint, cfg.mode, out_results, **extra)


if __name__ == "__main__":  # smoke: python3 -m danus.integrations.gated_search "stmt"
    import sys

    out = search(sys.argv[1] if len(sys.argv) > 1
                 else "compact Kahler manifold with nef canonical bundle")
    print(f"mode={out['mode']} count={out['count']} dropped={out.get('dropped_by_gate')} "
          f"error={out.get('error')} digest={out['digest'][:12]}")
    for r in out["results"][:3]:
        print(f"- {r.get('arxiv_id') or r.get('doi')} {r['theorem_id']} | "
              f"{r['title'][:50]}: {r['theorem'][:90]}")
