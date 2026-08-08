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
  DANUS_VERIFY_MAX_EXPANSION_ROUNDS
                      proof-hydration rounds after round zero (default: 2)
  DANUS_VERIFY_MAX_EXPANDED_PROOFS
                      cumulative strict-ancestor proof records (default: 8)
  DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS
                      canonical expanded-record budget (default: 200000)
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
from danus.core import (
    FactGraph,
    GlobalMemory,
    compute_fact_id,
    validate_verification_output,
)
from danus.integrations import search as _arxiv_search

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


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


def _nonnegative_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _max_expansion_rounds() -> int:
    return _nonnegative_int_env("DANUS_VERIFY_MAX_EXPANSION_ROUNDS", 2)


def _max_expanded_proofs() -> int:
    return _nonnegative_int_env("DANUS_VERIFY_MAX_EXPANDED_PROOFS", 8)


def _max_expanded_proof_chars() -> int:
    return _nonnegative_int_env("DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS", 200000)


def _verification_context_error(context: Dict[str, Any]) -> str:
    reasons: List[str] = []
    missing = context.get("missing_fact_ids") or []
    revoked = context.get("revoked_fact_ids") or []
    omitted = context.get("omitted_fact_ids") or []
    omitted_glossary = context.get("omitted_glossary_terms") or []
    omitted_expanded = context.get("omitted_expanded_proof_ids") or []
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
    if omitted_glossary:
        reasons.append(
            "required glossary context exceeds character budget; omitted terms: "
            + ", ".join(str(x) for x in omitted_glossary)
        )
    if omitted_expanded:
        reasons.append(
            "requested whole proof records exceed an adaptive context budget; "
            "omitted expanded proof fact_ids: "
            + ", ".join(str(x) for x in omitted_expanded)
        )
    if context.get("truncated") and not (
        omitted or omitted_glossary or omitted_expanded
    ):
        reasons.append("required context is truncated")
    if not context.get("complete") and not reasons:
        reasons.append("required context is incomplete")
    return "verification context error: " + "; ".join(reasons)


_SERVICE_RESULT_FIELDS = {
    "output_schema_version",
    "verification_status",
    "verification_report",
    "verdict",
    "needs_expanded_proofs",
    "repair_hints",
    "verification_context_digest",
}
_OUTPUT_RESULT_FIELDS = _SERVICE_RESULT_FIELDS - {"verification_context_digest"}
_METRICS_FIELDS = {
    "model",
    "effort",
    "elapsed_seconds",
    "tokens_used",
    "context_round",
    "expanded_proof_ids",
}


def _validate_service_result(
    result: Any, context: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate both the service envelope and the strict production semantics."""
    if not isinstance(result, dict):
        raise ValueError(
            f"verify service returned a non-dict body ({type(result).__name__})"
        )
    actual_fields = set(result)
    allowed_fields = (
        _SERVICE_RESULT_FIELDS,
        _SERVICE_RESULT_FIELDS | {"verification_metrics"},
    )
    if actual_fields in (
        _OUTPUT_RESULT_FIELDS,
        _OUTPUT_RESULT_FIELDS | {"verification_metrics"},
    ):
        raise ValueError(
            "verify service did not attest the supplied fact context digest; "
            "refusing a possibly context-free verdict"
        )
    if actual_fields not in allowed_fields:
        raise ValueError(
            "verify service returned an invalid response envelope; expected "
            + ", ".join(sorted(_SERVICE_RESULT_FIELDS))
        )
    verdict_payload = {key: result[key] for key in _OUTPUT_RESULT_FIELDS}
    try:
        validate_verification_output(verdict_payload)
    except ValueError as exc:
        raise ValueError(
            f"verify service returned an invalid verdict payload: {exc}"
        ) from exc
    if result.get("verification_context_digest") != context.get("digest"):
        raise ValueError(
            "verify service did not attest the supplied fact context digest; "
            "refusing a possibly context-free verdict"
        )
    metrics = result.get("verification_metrics")
    if metrics is not None:
        if not isinstance(metrics, dict) or set(metrics) != _METRICS_FIELDS:
            raise ValueError("verify service returned invalid verification_metrics")
        if not isinstance(metrics.get("model"), str) or not metrics["model"]:
            raise ValueError("verification_metrics.model must be non-empty")
        if not isinstance(metrics.get("effort"), str) or not metrics["effort"]:
            raise ValueError("verification_metrics.effort must be non-empty")
        elapsed = metrics.get("elapsed_seconds")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise ValueError("verification_metrics.elapsed_seconds must be non-negative")
        tokens = metrics.get("tokens_used")
        if tokens is not None and (
            isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
        ):
            raise ValueError("verification_metrics.tokens_used must be non-negative or null")
        scope = context.get("scope", {})
        if metrics.get("context_round") != scope.get("expansion_round"):
            raise ValueError("verification_metrics.context_round does not match context")
        if metrics.get("expanded_proof_ids") != scope.get("expanded_proof_ids"):
            raise ValueError(
                "verification_metrics.expanded_proof_ids does not match context"
            )
    return result


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

    try:
        context_max_chars = _verify_context_max_chars()
        max_expansion_rounds = _max_expansion_rounds()
        max_expanded_proofs = _max_expanded_proofs()
        max_expanded_proof_chars = _max_expanded_proof_chars()
    except ValueError as exc:
        return {
            "accepted": False,
            "verdict": "error",
            "error": f"adaptive verification configuration error: {exc}",
            "undefined_symbols": undefined,
        }

    candidate_fact_id = compute_fact_id(
        problem_id=problem_id,
        predecessors=predecessors,
        glossary_introduces=glossary_introduces,
        statement=statement,
        proof=proof,
    )
    expanded_proof_ids: List[str] = []
    expansion_round = 0
    verification_rounds: List[Dict[str, Any]] = []
    verification_context: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    protocol_error: Optional[str] = None

    # Each iteration reconstructs a canonical truth-layer snapshot and each
    # service call cold-starts a fresh verifier session. Round zero sends the
    # entire statement/edge/definition closure and exactly zero ancestor proofs.
    while True:
        try:
            verification_context = fg.verification_context(
                predecessors,
                max_chars=context_max_chars,
                candidate_fact_id=candidate_fact_id,
                expanded_proof_ids=expanded_proof_ids,
                expansion_round=expansion_round,
                expanded_proof_max_chars=max_expanded_proof_chars,
                glossary_texts=glossary_context_texts,
                glossary_exclude_terms=glossary_context_exclude_terms,
            )
        except Exception as exc:
            message = f"verification context error: {exc}"
            if not verification_rounds:
                return {
                    "accepted": False,
                    "verdict": "error",
                    "error": message,
                    "undefined_symbols": undefined,
                }
            protocol_error = message
            break

        if (
            not verification_context.get("complete")
            or verification_context.get("truncated")
            or verification_context.get("omitted_fact_ids")
            or verification_context.get("missing_fact_ids")
            or verification_context.get("revoked_fact_ids")
            or verification_context.get("omitted_glossary_terms")
            or verification_context.get("omitted_expanded_proof_ids")
        ):
            message = _verification_context_error(verification_context)
            if not verification_rounds:
                return {
                    "accepted": False,
                    "verdict": "error",
                    "error": message,
                    "undefined_symbols": undefined,
                }
            protocol_error = message
            break

        try:
            raw_result = _verify(
                statement,
                proof,
                fact_context=verification_context,
                glossary_introduces=glossary_introduces,
            )
            result = _validate_service_result(raw_result, verification_context)
        except Exception as exc:
            message = str(exc)
            if not verification_rounds:
                return {
                    "accepted": False,
                    "verdict": "error",
                    "error": message,
                    "undefined_symbols": undefined,
                }
            protocol_error = message
            break

        requests = result["needs_expanded_proofs"]
        verification_rounds.append(
            {
                "round": expansion_round,
                "context_fact_ids": list(
                    verification_context["scope"]["closure_fact_ids"]
                ),
                "context_digest": verification_context["digest"],
                "expanded_proof_ids": list(
                    verification_context["scope"]["expanded_proof_ids"]
                ),
                "verification_status": result["verification_status"],
                "verdict": result["verdict"],
                "needs_expanded_proofs": [dict(request) for request in requests],
                "verification_metrics": result.get("verification_metrics"),
            }
        )

        if result["verification_status"] == "final":
            break

        closure_ids = list(verification_context["scope"]["closure_fact_ids"])
        closure_set = set(closure_ids)
        already_expanded = set(expanded_proof_ids)
        request_ids = [str(request["id"]) for request in requests]
        invalid_reason: Optional[str] = None
        for requested_id in request_ids:
            if requested_id == candidate_fact_id:
                invalid_reason = (
                    "adaptive context protocol error: verifier requested the "
                    f"current candidate fact {requested_id}"
                )
                break
            if not _FACT_ID_RE.fullmatch(requested_id):
                invalid_reason = (
                    "adaptive context protocol error: verifier requested unknown "
                    f"fact_id {requested_id!r}"
                )
                break
            if requested_id not in closure_set:
                try:
                    active_elsewhere = fg.exists(requested_id)
                except Exception as exc:
                    invalid_reason = (
                        "adaptive context protocol error: could not authenticate "
                        f"requested fact_id {requested_id}: {exc}"
                    )
                    break
                kind = "non-ancestor" if active_elsewhere else "unknown"
                invalid_reason = (
                    "adaptive context protocol error: verifier requested "
                    f"{kind} fact_id {requested_id}"
                )
                break
            if requested_id in already_expanded:
                invalid_reason = (
                    "adaptive context protocol error: verifier re-requested already "
                    f"expanded fact_id {requested_id}; no progress"
                )
                break
        if invalid_reason is not None:
            protocol_error = invalid_reason
            break
        if expansion_round >= max_expansion_rounds:
            protocol_error = (
                "adaptive context protocol error: maximum expansion rounds "
                f"({max_expansion_rounds}) exceeded"
            )
            break

        requested_set = set(request_ids)
        next_expanded_ids = [
            fact_id
            for fact_id in closure_ids
            if fact_id in already_expanded or fact_id in requested_set
        ]
        if len(next_expanded_ids) > max_expanded_proofs:
            protocol_error = (
                "adaptive context protocol error: maximum expanded proofs "
                f"({max_expanded_proofs}) exceeded"
            )
            break
        if next_expanded_ids == expanded_proof_ids:
            protocol_error = (
                "adaptive context protocol error: expansion request made no progress"
            )
            break
        expanded_proof_ids = next_expanded_ids
        expansion_round += 1

    adaptive_metadata = {
        "adaptive_rounds": expansion_round,
        "verification_calls": len(verification_rounds),
        "expanded_proof_ids": list(expanded_proof_ids),
        "verification_metrics": [
            round_trace["verification_metrics"]
            for round_trace in verification_rounds
            if round_trace.get("verification_metrics") is not None
        ],
    }

    if protocol_error is not None:
        trace_error = None
        try:
            gm.append(
                "verification",
                claim=statement,
                evidence=protocol_error,
                author=_author(),
                verifiable=False,
                links={"source_id": source_id, "predecessors": predecessors},
                verdict="error",
                fact_id=None,
                write_error=None,
                verification_report=None,
                verification_context_digest=(
                    verification_context.get("digest")
                    if verification_context is not None
                    else None
                ),
                verification_rounds=verification_rounds,
                final_math_verdict=None,
            )
        except Exception as exc:
            trace_error = str(exc)
        response = {
            "accepted": False,
            "verdict": "error",
            "error": protocol_error,
            "undefined_symbols": undefined,
            **adaptive_metadata,
        }
        if trace_error:
            response["trace_error"] = trace_error
        return response

    assert result is not None
    assert verification_context is not None
    verdict = result["verdict"]
    accepted = verdict == "correct"

    # Only a final/correct response authorizes the locked context-CAS/add. Catch write
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
                context_max_chars=context_max_chars,
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
            verification_rounds=verification_rounds,
            final_math_verdict=verdict,
            expanded_proof_ids=list(expanded_proof_ids),
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
            **adaptive_metadata,
        }
        if trace_error:
            response["trace_error"] = trace_error
        return response
    if write_error:
        response = {"accepted": True, "fact_id": None, "write_error": write_error,
                    "undefined_symbols": undefined, **adaptive_metadata}
        if trace_error:
            response["trace_error"] = trace_error
        return response
    response = {
        "accepted": True,
        "fact_id": fact_id,
        "undefined_symbols": undefined,
        **adaptive_metadata,
    }
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
