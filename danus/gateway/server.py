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
import math
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus._mcp import FastMCP
from danus.core import (
    FactGraph,
    FactPromotionOutcomeUnknown,
    GlobalMemory,
    VERIFICATION_OUTPUT_PROTOCOL_VERSION,
    compute_fact_id,
    validate_verification_output,
)
from danus.integrations import search as _arxiv_search
from danus.redaction import redact_external_error

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_VERIFY_HTTP_ERROR_BODY_MAX_BYTES = 4096
_VERIFY_HTTP_ERROR_DETAIL_MAX_CHARS = 1024
_VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES = 8 * 1024 * 1024
_VERIFY_HEALTH_BODY_MAX_BYTES = 4096
_VERIFY_HEALTH_TIMEOUT_SECONDS = 10
_GATEWAY_EXCEPTION_DETAIL_MAX_CHARS = 1024


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
    if project is not None and _role() not in {"main", "all"}:
        raise RuntimeError("only the main role may select another project")
    agents_root = os.environ.get("DANUS_AGENTS_ROOT", "")
    project_dir = os.environ.get("DANUS_PROJECT_DIR", "")
    if project:
        if not agents_root:
            raise RuntimeError("DANUS_AGENTS_ROOT is not set; cannot resolve a project by name")
        if not _PROJECT_NAME_RE.match(project):
            raise RuntimeError(f"invalid project name: {project!r}")
        try:
            root = Path(agents_root).resolve(strict=True)
            pdir = root / project
            info = os.lstat(pdir)
            resolved = pdir.resolve(strict=True)
        except (FileNotFoundError, OSError):
            raise RuntimeError(f"no such project: {project!r} (under {agents_root})")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"unsafe project path: {project!r}")
        if resolved.parent != root:
            raise RuntimeError(f"project escapes agents root: {project!r}")
        return resolved
    if not project_dir:
        raise RuntimeError("DANUS_PROJECT_DIR is not set and no project was given")
    pinned = Path(project_dir)
    try:
        info = os.lstat(pinned)
        resolved = pinned.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("DANUS_PROJECT_DIR is not a safe existing project") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("DANUS_PROJECT_DIR must name a real directory")
    return resolved


def _gm(project: Optional[str] = None) -> GlobalMemory:
    return GlobalMemory(_project(project))


def _fg(project: Optional[str] = None) -> FactGraph:
    return FactGraph(_project(project))


def _conversation_frontier_at_action() -> Optional[Dict[str, Any]]:
    """Best-effort, body-free provenance for the owner-guidance frontier.

    The hot-join module is imported lazily so the read-only verifier process
    never imports or opens the control store.  This metadata is observability,
    not part of mathematical correctness: an unavailable ledger is recorded
    honestly but cannot disable the independent verifier/write gate.
    """
    if os.environ.get("DANUS_HOTJOIN_ENABLED") != "1":
        return None
    target = os.environ.get("DANUS_HOTJOIN_TARGET") or _author()
    try:
        if _role() not in {"worker", "all"}:
            raise RuntimeError("hot-join provenance is only valid for worker roles")
        from danus.hotjoin import HotJoinStore

        frontier = HotJoinStore(_project()).frontier(target)
        return {"status": "available", **frontier}
    except Exception as exc:
        # Do not include exception text: it may contain host paths.  The error
        # class is enough to distinguish unavailable provenance from a worker
        # launched without hot-join support.
        return {
            "schema_version": 1,
            "status": "unavailable",
            "target": target,
            "error_type": type(exc).__name__,
        }


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
    if timeout <= 0:
        timeout = 3600

    # Fail closed before constructing or sending the paid POST.  An old service
    # exposes only status/pid and is rejected here; a service restart between
    # GET and POST is caught when the POST echoes this exact bundle digest.
    bundle_digest = _verify_service_health_preflight(verify_url, timeout=timeout)

    payload: Dict[str, Any] = {
        "expected_output_protocol_version": VERIFICATION_OUTPUT_PROTOCOL_VERSION,
        "expected_verifier_bundle_digest": bundle_digest,
        "statement": statement,
        "proof": proof,
    }
    if fact_context is not None:
        payload["fact_context"] = fact_context
    if glossary_introduces is not None:
        payload["glossary_introduces"] = glossary_introduces
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        verify_url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 (trusted configured verifier URL)
            req, timeout=timeout
        ) as resp:
            raw = resp.read(_VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES + 1)
        if len(raw) > _VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES:
            raise RuntimeError("verify service success response is too large")
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # FastAPI's bounded string ``detail`` carries actionable preflight errors
        # (for example, one mistyped fact citation).  urllib otherwise discards
        # it and leaves only "Bad Request".  Never persist arbitrary HTML,
        # structured validation input, or an unbounded service response.
        try:
            raw = exc.read(_VERIFY_HTTP_ERROR_BODY_MAX_BYTES + 1)
        finally:
            exc.close()
        detail: Optional[str] = None
        if len(raw) <= _VERIFY_HTTP_ERROR_BODY_MAX_BYTES:
            try:
                response = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = None
            if isinstance(response, dict) and isinstance(response.get("detail"), str):
                candidate = " ".join(response["detail"].split())
                if candidate:
                    detail = candidate[:_VERIFY_HTTP_ERROR_DETAIL_MAX_CHARS]
        suffix = f": {detail}" if detail is not None else ""
        raise RuntimeError(f"verify service HTTP {exc.code}{suffix}") from exc


def _verify_health_url(verify_url: str) -> str:
    parsed = urllib.parse.urlsplit(verify_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("DANUS_VERIFY_URL must be an absolute HTTP(S) /verify URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/verify"):
        raise RuntimeError("DANUS_VERIFY_URL path must end in /verify")
    health_path = path[: -len("/verify")] + "/health"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, health_path, "", "")
    )


def _verify_service_health_preflight(verify_url: str, *, timeout: int) -> str:
    """Attest service protocol and return its exact import-time bundle digest."""
    request = urllib.request.Request(
        _verify_health_url(verify_url),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 (trusted configured verifier URL)
            request,
            timeout=min(timeout, _VERIFY_HEALTH_TIMEOUT_SECONDS),
        ) as response:
            raw = response.read(_VERIFY_HEALTH_BODY_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            code = exc.code
        finally:
            exc.close()
        raise RuntimeError(f"verify service health HTTP {code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("verify service health preflight failed") from exc

    if len(raw) > _VERIFY_HEALTH_BODY_MAX_BYTES:
        raise RuntimeError("verify service health response is too large")
    try:
        health = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("verify service health returned invalid JSON") from exc
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise RuntimeError("verify service health did not report status=ok")

    protocol = health.get("output_protocol_version")
    if (
        isinstance(protocol, bool)
        or protocol != VERIFICATION_OUTPUT_PROTOCOL_VERSION
    ):
        raise RuntimeError(
            "verify service output protocol mismatch: expected "
            f"{VERIFICATION_OUTPUT_PROTOCOL_VERSION}, got {protocol!r}"
        )
    digest = health.get("verifier_bundle_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("verify service health omitted a valid bundle digest")
    return digest


def _bounded_exception_detail(exc: Exception) -> str:
    """Return a non-empty, bounded diagnostic without falling back to repr.

    Some storage exceptions (notably a bare ``OSError`` or ``MemoryError``) have
    an empty string representation.  Callers use the returned text only for
    diagnostics, never as a success/failure predicate.
    """
    detail = " ".join(redact_external_error(exc).split())
    if not detail:
        detail = type(exc).__name__ or "Exception"
    return detail[:_GATEWAY_EXCEPTION_DETAIL_MAX_CHARS]


def _redact_verifier_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Project validated verifier text through the external-secret redactor."""
    safe = dict(result)
    safe["repair_hints"] = redact_external_error(result["repair_hints"])
    safe["needs_expanded_proofs"] = [
        {
            "id": request["id"],
            "reason": redact_external_error(request["reason"]),
        }
        for request in result["needs_expanded_proofs"]
    ]
    report = result["verification_report"]

    def finding(value: Dict[str, Any]) -> Dict[str, Any]:
        evidence = value["candidate_evidence"]
        return {
            "location": redact_external_error(value["location"]),
            "issue": redact_external_error(value["issue"]),
            "candidate_evidence": {
                "source": evidence["source"],
                "line": evidence["line"],
                "exact_line": redact_external_error(evidence["exact_line"]),
            },
        }

    safe["verification_report"] = {
        "summary": redact_external_error(report["summary"]),
        "critical_errors": [finding(item) for item in report["critical_errors"]],
        "gaps": [finding(item) for item in report["gaps"]],
    }
    metrics = result.get("verification_metrics")
    if metrics is not None:
        safe_metrics = dict(metrics)
        safe_metrics["model"] = redact_external_error(metrics["model"])
        safe_metrics["effort"] = redact_external_error(metrics["effort"])
        safe["verification_metrics"] = safe_metrics
    return safe


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
    result: Any,
    context: Dict[str, Any],
    *,
    statement: str,
    proof: str,
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
        validate_verification_output(
            verdict_payload,
            statement=statement,
            proof=proof,
        )
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
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(float(elapsed))
            or elapsed < 0
        ):
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
    return _redact_verifier_result(result)


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
    downstream. ``accepted`` is retained as the verifier-acceptance compatibility
    field; use ``promoted`` (or ``submission_status == "promoted"``) to decide
    whether the fact reached the graph. ``verification_verdict`` preserves the
    mathematical verdict independently of promotion.

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
    conversation_frontier = _conversation_frontier_at_action()

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
            "promoted": False,
            "submission_status": "error",
            "verification_verdict": None,
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
    verification_call_count = 0

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
            message = "verification context error: " + _bounded_exception_detail(exc)
            if not verification_rounds:
                return {
                    "accepted": False,
                    "promoted": False,
                    "submission_status": "error",
                    "verification_verdict": None,
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
                    "promoted": False,
                    "submission_status": "error",
                    "verification_verdict": None,
                    "verdict": "error",
                    "error": message,
                    "undefined_symbols": undefined,
                }
            protocol_error = message
            break

        try:
            verification_call_count += 1
            raw_result = _verify(
                statement,
                proof,
                fact_context=verification_context,
                glossary_introduces=glossary_introduces,
            )
            result = _validate_service_result(
                raw_result,
                verification_context,
                statement=statement,
                proof=proof,
            )
        except Exception as exc:
            message = _bounded_exception_detail(exc)
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
                    "verification_status": "error",
                    "error_stage": "verify_call",
                    "error": message,
                    "needs_expanded_proofs": [],
                    "verification_metrics": None,
                }
            )
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
        "verification_calls": verification_call_count,
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
                promoted=False,
                submission_status="error",
                verification_verdict=None,
                verification_report=None,
                verification_context_digest=(
                    verification_context.get("digest")
                    if verification_context is not None
                    else None
                ),
                verification_rounds=verification_rounds,
                final_math_verdict=None,
                conversation_frontier_at_action=conversation_frontier,
            )
        except Exception as exc:
            trace_error = _bounded_exception_detail(exc)
        response = {
            "accepted": False,
            "promoted": False,
            "submission_status": "error",
            "verification_verdict": None,
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
    promotion_unknown = False
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
        except FactPromotionOutcomeUnknown as e:
            promotion_unknown = True
            write_error = _bounded_exception_detail(e)
        except Exception as e:
            write_error = _bounded_exception_detail(e)

    promoted: Optional[bool] = None if promotion_unknown else fact_id is not None
    if accepted and promoted is False and write_error is None:
        write_error = "fact graph write returned no fact_id"
    if promotion_unknown:
        submission_status = "promotion_unknown"
    elif promoted:
        submission_status = "promoted"
    elif accepted:
        submission_status = "verified_not_promoted"
    else:
        submission_status = "rejected"

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
            promoted=promoted,
            submission_status=submission_status,
            verification_verdict=verdict,
            verification_report=result.get("verification_report"),
            verification_context_digest=verification_context.get("digest"),
            verification_rounds=verification_rounds,
            final_math_verdict=verdict,
            expanded_proof_ids=list(expanded_proof_ids),
            conversation_frontier_at_action=conversation_frontier,
        )
    except Exception as exc:  # the caller must not lose an already-written fact id
        trace_error = _bounded_exception_detail(exc)

    # 5) Return.
    if not accepted:
        response = {
            "accepted": False,
            "promoted": False,
            "submission_status": "rejected",
            "verification_verdict": verdict,
            "verdict": verdict,
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
            "undefined_symbols": undefined,
            **adaptive_metadata,
        }
        if trace_error:
            response["trace_error"] = trace_error
        return response
    if promotion_unknown:
        response = {
            "accepted": True,
            "promoted": None,
            "submission_status": "promotion_unknown",
            "verification_verdict": verdict,
            "fact_id": None,
            "write_error": write_error,
            "undefined_symbols": undefined,
            **adaptive_metadata,
        }
        if trace_error:
            response["trace_error"] = trace_error
        return response
    if not promoted:
        response = {
            "accepted": True,
            "promoted": False,
            "submission_status": "verified_not_promoted",
            "verification_verdict": verdict,
            "fact_id": None,
            "write_error": write_error,
            "undefined_symbols": undefined,
            **adaptive_metadata,
        }
        if trace_error:
            response["trace_error"] = trace_error
        return response
    response = {
        "accepted": True,
        "promoted": True,
        "submission_status": "promoted",
        "verification_verdict": verdict,
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
