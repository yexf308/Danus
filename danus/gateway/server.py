#!/usr/bin/env python3
"""Danus gateway — the role-gated MCP server.

A thin MCP wrapper over ``danus.core`` (the truth stores) + one external
integration (``danus.integrations`` arXiv search). It exposes only the verbs an
LLM can't do reliably itself (content-addressed writes, cascade integrity, the
verifier-gated fact write, BM25, and bounded lazy fact hydration). Local-memory
reads and novelty judgment remain agent operations; verified-fact retrieval uses
full-text search with statement-only results followed by explicit-id
``fact_context``.

The permission model (which tools each role sees) lives in ``roles.py``. The
``fact_submit`` tool is the ONLY fact-write path: it runs the glossary-coverage
check, calls the verify service, then rechecks context and adds under the graph
lock after a ``correct`` verdict. It ALWAYS traces the verdict to global memory
(kind ``verification``) — accept,
reject, or accept-but-write-failed — so a verdict is never stored by nobody (the
verify service is stateless).

Config is read from the environment at CALL time (not import time) so the server
is testable and reconfigurable:
  DANUS_PROJECT_DIR   the project dir a worker is pinned to (fallback for main)
  DANUS_AGENTS_ROOT   root holding all projects (<root>/<project>); lets main
                      address any project by name via the ``project`` arg
  DANUS_AUTHOR        this agent's id, for attribution
  DANUS_ROLE          worker | main | verifier | all  (selects exposed tools;
                      unset falls back to the read-only verifier set — fail-closed)
  DANUS_VERIFY_URL    verify-service endpoint for fact_submit
  DANUS_VERIFY_CONTEXT_MAX_CHARS
                      required predecessor context budget (default: 200000)
  DANUS_PROBLEM_ID    problem id stamped on written facts (default: project name)
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus._mcp import FastMCP
from danus.core import FactGraph, GlobalMemory, validate_verification_output
from danus.integrations import search as _arxiv_search

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# config resolution (env read at call time — testable / reconfigurable)       #
# --------------------------------------------------------------------------- #

def _author() -> str:
    return os.environ.get("DANUS_AUTHOR", "unknown")


def _role() -> str:
    # Fail-closed: an UNSET role gets the most-restrictive read-only set, same as
    # a mis-typed one (roles.tools_for). Dev use of the full set is explicit:
    # DANUS_ROLE=all.
    return os.environ.get("DANUS_ROLE", "verifier")


def _project(project: Optional[str] = None) -> Path:
    """Resolve the project dir to operate on.

    ``project`` (the main agent's per-call selector) wins: it names a project
    under ``DANUS_AGENTS_ROOT`` (``<root>/<project>``), so one session can touch
    several projects. With no ``project`` we fall back to ``DANUS_PROJECT_DIR``
    (a worker is always pinned this way). The name is validated to a single path
    segment — no ``/`` or ``..`` — so it can never escape the agents root."""
    agents_root = os.environ.get("DANUS_AGENTS_ROOT", "")
    project_dir = os.environ.get("DANUS_PROJECT_DIR", "")
    if project:
        if not agents_root:
            raise RuntimeError("DANUS_AGENTS_ROOT is not set; cannot resolve a project by name")
        if not _PROJECT_NAME_RE.match(project):
            raise RuntimeError(f"invalid project name: {project!r}")
        pdir = Path(agents_root) / project
        if not pdir.is_dir():
            raise RuntimeError(f"no such project: {project!r} (under {agents_root})")
        return pdir
    if not project_dir:
        raise RuntimeError("DANUS_PROJECT_DIR is not set and no project was given")
    return Path(project_dir)


def _gm(project: Optional[str] = None) -> GlobalMemory:
    return GlobalMemory(_project(project))


def _fg(project: Optional[str] = None) -> FactGraph:
    return FactGraph(_project(project))


def _verify(
    statement: str,
    proof: str,
    fact_context: Optional[Dict[str, Any]] = None,
    glossary_introduces: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """POST a candidate and optional fact context to the verify service."""
    verify_url = os.environ.get("DANUS_VERIFY_URL", "")
    if not verify_url:
        raise RuntimeError("DANUS_VERIFY_URL is not set (verify service not wired yet)")
    try:
        timeout = int(os.environ.get("DANUS_VERIFY_TIMEOUT", "3600"))
    except ValueError:
        timeout = 3600
    payload: Dict[str, Any] = {"statement": statement, "proof": proof}
    if fact_context is not None:
        payload["fact_context"] = fact_context
    if glossary_introduces is not None:
        payload["glossary_introduces"] = glossary_introduces
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        verify_url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8"))


def _verify_context_max_chars() -> int:
    raw = os.environ.get("DANUS_VERIFY_CONTEXT_MAX_CHARS", "200000")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"DANUS_VERIFY_CONTEXT_MAX_CHARS must be a non-negative integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise ValueError("DANUS_VERIFY_CONTEXT_MAX_CHARS must be a non-negative integer")
    return value


def _verification_context_error(context: Dict[str, Any]) -> str:
    reasons: List[str] = []
    missing = context.get("missing_fact_ids") or []
    revoked = context.get("revoked_fact_ids") or []
    omitted = context.get("omitted_fact_ids") or []
    omitted_glossary = context.get("omitted_glossary_terms") or []
    if missing:
        reasons.append("missing predecessor fact_ids: " + ", ".join(str(x) for x in missing))
    if revoked:
        reasons.append("revoked predecessor fact_ids: " + ", ".join(str(x) for x in revoked))
    if omitted:
        reasons.append(
            "required context exceeds character budget "
            f"{context.get('character_budget')}; omitted fact_ids: "
            + ", ".join(str(x) for x in omitted)
        )
    elif context.get("truncated"):
        reasons.append("required context is truncated")
    if omitted_glossary:
        reasons.append(
            "required glossary context exceeds character budget; omitted terms: "
            + ", ".join(str(x) for x in omitted_glossary)
        )
    if not context.get("complete") and not reasons:
        reasons.append("required context is incomplete")
    return "verification context error: " + "; ".join(reasons)


# --------------------------------------------------------------------------- #
# global memory                                                               #
# --------------------------------------------------------------------------- #

def gm_add(
    kind: str,
    claim: str,
    evidence: str = "",
    verifiable: Optional[bool] = None,
    glossary: Optional[Dict[str, str]] = None,
    links: Optional[Dict[str, Any]] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish a finding to shared global memory (claim + evidence). Verifiable
    kinds (conclusion/example/counterexample/proof_attempt) require explicit
    evidence; judgments (plan/direction/obstacle/master_guidance/elaboration) do
    not. Define your symbols in ``glossary`` and reuse project terminology.

    Main agent: pass ``project`` to target one of several projects by name;
    workers omit it (pinned to their own project)."""
    entry_id = _gm(project).append(
        kind, claim=claim, evidence=evidence, author=_author(),
        verifiable=verifiable, glossary=glossary, links=links,
    )
    return {"id": entry_id, "kind": kind}


def gm_search(query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10,
              project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 over shared global-memory findings. Use to reuse others' results,
    avoid duplicate work, and learn which paths already died. Main agent: pass
    ``project`` to search a specific project; workers omit it."""
    return _gm(project).search(query, kinds=kinds, limit_per_kind=limit_per_kind)


# --------------------------------------------------------------------------- #
# fact graph                                                                  #
# --------------------------------------------------------------------------- #

def fact_submit(
    statement: str,
    proof: str,
    predecessors: Optional[List[str]] = None,
    glossary_introduces: Optional[Dict[str, str]] = None,
    intuition: str = "",
    source_id: Optional[str] = None,
    external_refs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The only way to write a fact. Runs the glossary-coverage check, builds
    complete declared-predecessor context, calls the verifier, and, after a
    ``correct`` verdict, attempts the locked context-CAS/add. Context errors block
    before verification; stale context returns ``write_error`` without a write.
    On reject, returns repair hints and writes nothing. Cite a returned ``fact_id``
    downstream.

    Once a verdict exists, the gateway durably attempts to record the outcome in
    global memory (kind ``verification``). If that independent audit append
    fails, the response includes ``trace_error`` while preserving any already
    written ``fact_id``. ``source_id`` optionally links to the global-memory
    finding being promoted.

    When your proof cites an external (published) result, pass it in
    ``external_refs`` as a structured entry — e.g.
    ``{"key": "HL26", "authors": ["Han", "Liu"], "title": "...",
    "arxiv": "2603.03817", "year": 2026, "cited_for": "Theorem 1.2"}`` (ground it
    with ``search_arxiv_theorems``). This is captured on the fact so the paper
    pipeline can cite it without re-deriving; it is mutable metadata and does not
    affect the ``fact_id``."""
    fg = _fg()
    gm = _gm()
    problem_id = os.environ.get("DANUS_PROBLEM_ID", Path(_project()).name)
    predecessors = list(dict.fromkeys(p for p in (predecessors or []) if p))
    glossary_introduces = dict(glossary_introduces or {})
    glossary_context_texts = [
        statement,
        proof,
        intuition,
        *(str(definition) for definition in glossary_introduces.values()),
    ]
    glossary_context_exclude_terms = [str(symbol) for symbol in glossary_introduces]

    # glossary coverage is advisory — never let a heuristic bug block submission
    try:
        undefined = fg.undefined_symbols(
            statement=statement, proof=proof, intuition=intuition,
            predecessors=predecessors, glossary_introduces=glossary_introduces,
        )
    except Exception:
        undefined = []

    # 1) Build the complete declared dependency context before the verifier is
    # contacted. Direct predecessors are premise cards. Ancestors contribute a
    # digest/count commitment to the validated closure and every inherited
    # fact-local definition; ancestor statements, the closure edge skeleton, and
    # every predecessor proof stay out of the model prompt. Missing/revoked/omitted
    # input is a gate error.
    try:
        verification_context = fg.verification_context(
            predecessors,
            max_chars=_verify_context_max_chars(),
            glossary_texts=glossary_context_texts,
            glossary_exclude_terms=glossary_context_exclude_terms,
        )
    except Exception as exc:
        return {
            "accepted": False,
            "verdict": "error",
            "error": f"verification context error: {exc}",
            "undefined_symbols": undefined,
        }
    if (
        not verification_context.get("complete")
        or verification_context.get("truncated")
        or verification_context.get("omitted_fact_ids")
        or verification_context.get("missing_fact_ids")
        or verification_context.get("revoked_fact_ids")
        or verification_context.get("omitted_glossary_terms")
    ):
        return {
            "accepted": False,
            "verdict": "error",
            "error": _verification_context_error(verification_context),
            "undefined_symbols": undefined,
        }

    # 2) Verify. If the verify service errors, no verdict exists yet: return a
    #    clean error so the worker retries. Nothing is lost.
    try:
        result = _verify(
            statement,
            proof,
            fact_context=verification_context,
            glossary_introduces=glossary_introduces or {},
        )
    except Exception as e:
        return {"accepted": False, "verdict": "error", "error": str(e),
                "undefined_symbols": undefined}
    # A successful call that returned a non-dict body (e.g. a bare list) would make
    # the .get() below throw uncaught; treat it as a verify error (clean retry
    # envelope, no verdict to store) rather than leaking a stack trace to the worker.
    if not isinstance(result, dict):
        return {"accepted": False, "verdict": "error",
                "error": f"verify service returned a non-dict body ({type(result).__name__})",
                "undefined_symbols": undefined}
    service_fields = {
        "verdict",
        "repair_hints",
        "verification_report",
        "verification_context_digest",
    }
    if set(result) == service_fields - {"verification_context_digest"}:
        return {
            "accepted": False,
            "verdict": "error",
            "error": (
                "verify service did not attest the supplied fact context digest; "
                "refusing a possibly context-free verdict"
            ),
            "undefined_symbols": undefined,
        }
    if set(result) != service_fields:
        return {
            "accepted": False,
            "verdict": "error",
            "error": (
                "verify service returned an invalid response envelope; expected "
                + ", ".join(sorted(service_fields))
            ),
            "undefined_symbols": undefined,
        }
    try:
        validate_verification_output({
            "verdict": result["verdict"],
            "repair_hints": result["repair_hints"],
            "verification_report": result["verification_report"],
        })
    except ValueError as exc:
        return {
            "accepted": False,
            "verdict": "error",
            "error": f"verify service returned an invalid verdict payload: {exc}",
            "undefined_symbols": undefined,
        }
    if result.get("verification_context_digest") != verification_context.get("digest"):
        return {
            "accepted": False,
            "verdict": "error",
            "error": (
                "verify service did not attest the supplied fact context digest; "
                "refusing a possibly context-free verdict"
            ),
            "undefined_symbols": undefined,
        }
    verdict = result.get("verdict")
    accepted = verdict == "correct"

    # 3) An accepted verdict authorizes the locked context-CAS/add. Catch write
    #    failures (e.g. a revoked predecessor) so they do NOT skip the trace below.
    fact_id = None
    write_error = None
    if accepted:
        try:
            # A verification may run for minutes. Compare + add under the same
            # cross-process mutation lock used by revoke, closing the race between
            # the post-verification snapshot and the fact write.
            fact_id = fg.add_if_context_unchanged(
                expected_context=verification_context,
                context_max_chars=_verify_context_max_chars(),
                context_glossary_texts=glossary_context_texts,
                context_glossary_exclude_terms=glossary_context_exclude_terms,
                problem_id=problem_id, author=_author(), statement=statement, proof=proof,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
                intuition=intuition, external_refs=external_refs,
            )
        except Exception as e:
            write_error = str(e)

    # 4) Record the outcome. A trace I/O failure must not hide an accepted fact id.
    trace_error = None
    try:
        gm.append(
            "verification",
            claim=statement,
            evidence=(
                "verdict: correct"
                if accepted
                else (result.get("repair_hints") or "verdict: wrong")
            ),
            author=_author(),
            verifiable=False,
            links={"source_id": source_id, "predecessors": predecessors or []},
            verdict=verdict,
            fact_id=fact_id,
            write_error=write_error,
            verification_report=result.get("verification_report"),
            verification_context_digest=verification_context.get("digest"),
        )
    except Exception as exc:  # the caller must not lose an already-written fact id
        trace_error = str(exc)

    # 5) Return.
    if not accepted:
        response = {
            "accepted": False,
            "verdict": verdict,
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
            "undefined_symbols": undefined,
        }
        if trace_error:
            response["trace_error"] = trace_error
        return response
    if write_error:
        response = {"accepted": True, "fact_id": None, "write_error": write_error,
                    "undefined_symbols": undefined}
        if trace_error:
            response["trace_error"] = trace_error
        return response
    response = {"accepted": True, "fact_id": fact_id, "undefined_symbols": undefined}
    if trace_error:
        response["trace_error"] = trace_error
    return response


def fact_search(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 search over the verified fact graph (statement + proof + glossary),
    the derived fact index rebuilt on demand from the fact files — the fact graph
    stays the single source of truth. Use it **before proving** to check whether a
    fact like yours already exists, and to find the verified facts that bear on
    your subgoal so you can cite their ``fact_id``. Returns ranked ``{fact_id,
    score, statement}``. Main agent: pass ``project`` to search a specific
    project's graph; workers omit it."""
    return {"query": query, "results": _fg(project).search(query, limit=limit)}


def fact_context(
    fact_ids: List[str],
    predecessor_depth: Optional[int] = 0,
    proof_mode: str = "none",
    max_chars: Optional[int] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Read deterministic context for explicit verified ``fact_ids``. By
    default, return each requested statement, its declared predecessor edges,
    fact-local definitions, and only referenced project/global definitions. Set
    ``predecessor_depth`` to a non-negative hop count or ``null`` for the full
    closure. Proof hydration is explicit: ``none`` (default),
    ``selected`` (requested roots only), or ``all``. ``max_chars`` never slices a
    fact: complete lower-priority records are omitted and reported instead.

    Main agent: pass ``project``; workers omit it (pinned to their project)."""
    return _fg(project).context(
        fact_ids,
        predecessor_depth=predecessor_depth,
        proof_mode=proof_mode,
        max_chars=max_chars,
    )


def fact_revoke(fact_id: str, reason: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Cascade-revoke a wrong fact and everything that depends on it. Destructive;
    operator / main-agent only. Main agent: pass ``project`` to target the project
    that owns the fact."""
    revoked = _fg(project).revoke(fact_id, reason=reason)
    return {"revoked": revoked}


# --------------------------------------------------------------------------- #
# arXiv theorem search (external integration)                                 #
# --------------------------------------------------------------------------- #

def search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
    """Semantic search over arXiv theorem statements (Matlas). Returns
    **verbatim, as-published** theorem / lemma / definition statements — statement
    fidelity matters for math reasoning and citation checking. Phrase the query as
    a *complete mathematical statement* when possible. Returns ranked results,
    each with ``title``, the full ``theorem`` text, ``arxiv_id``, and the in-paper
    ``theorem_id``. External HTTP, no auth; on outage returns an ``error`` and
    empty ``results`` (retry / fall back to built-in web search)."""
    return _arxiv_search(query, num_results=num_results)


# --------------------------------------------------------------------------- #
# role-based registration                                                     #
# --------------------------------------------------------------------------- #

_TOOLS = {
    "gm_add": gm_add,
    "gm_search": gm_search,
    "fact_submit": fact_submit,
    "fact_search": fact_search,
    "fact_context": fact_context,
    "fact_revoke": fact_revoke,
    "search_arxiv_theorems": search_arxiv_theorems,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    """Build the stdio MCP app exposing exactly the tools ``role`` may use.
    ``role`` defaults to ``DANUS_ROLE`` (env); unset falls back to the read-only
    verifier set (fail-closed)."""
    app = FastMCP("danus-core")
    for name in tools_for(role if role is not None else _role()):
        app.tool(name=name)(_TOOLS[name])
    return app
