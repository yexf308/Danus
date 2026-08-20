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

import hashlib
import json
import math
import os
import re
import sqlite3
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
    canonical_global_memory_record,
    compute_fact_id,
    fact_identity_from_verification_context,
    validate_verification_output,
)
from danus.integrations import search as _arxiv_search
from danus.redaction import redact_external_error
from danus.verification_prompt import verification_prompt_bytes
from danus.core.schema import (
    GLOBAL_KINDS,
    clean_consult_provenance,
    validate_advisor_checkpoint,
)

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_COORDINATION_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VERIFY_HTTP_ERROR_BODY_MAX_BYTES = 4096
_VERIFY_HTTP_ERROR_DETAIL_MAX_CHARS = 1024
_VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES = 8 * 1024 * 1024
_VERIFY_HEALTH_BODY_MAX_BYTES = 4096
_VERIFY_HEALTH_TIMEOUT_SECONDS = 10
_GATEWAY_EXCEPTION_DETAIL_MAX_CHARS = 1024
_VERIFY_SCHEDULER_METADATA_FIELD = "_danus_gateway_scheduler"
_VERIFY_SCHEDULER_WAIT_MAX_MS = 2_147_483_647
_VERIFY_HEALTH_FIELDS = {
    "status",
    "pid",
    "instance_nonce",
    "output_protocol_version",
    "verifier_bundle_digest",
}
_VERIFY_SCHEDULER_SOURCES = {"launched", "coalesced", "cache_hit", "rejected"}
_VERIFY_SCHEDULER_REJECTIONS = {
    "per_key_waiters_full",
    "total_waiters_full",
    "distinct_queue_full",
    "queue_wait_timeout",
}


class _VerifierRequestError(RuntimeError):
    """Bounded verify transport failure with optional safe scheduler metadata."""

    def __init__(
        self, message: str, scheduler: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(message)
        self.scheduler = scheduler


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
            raise RuntimeError(
                "DANUS_AGENTS_ROOT is not set; cannot resolve a project by name"
            )
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

    prompt_bytes = verification_prompt_bytes(
        run_id="x" * 64,
        statement=statement,
        proof=proof,
        fact_context=fact_context,
        glossary_introduces=glossary_introduces,
    )
    max_prompt_bytes = _verify_max_prompt_bytes()
    if prompt_bytes > max_prompt_bytes:
        raise ValueError(
            "serialized verification prompt exceeds "
            f"DANUS_VERIFY_MAX_PROMPT_BYTES ({prompt_bytes} bytes > "
            f"{max_prompt_bytes}); reduce the candidate or hydrated context"
        )

    # Fail closed before constructing or sending the paid POST.  The exact
    # instance nonce prevents a restart between this GET and the POST from
    # inheriting the previous process's preflight authorization.
    instance_nonce, bundle_digest = _verify_service_health_preflight(
        verify_url, timeout=timeout
    )

    payload: Dict[str, Any] = {
        "expected_verifier_instance_nonce": instance_nonce,
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
            scheduler = _parse_verify_scheduler_headers(
                getattr(resp, "headers", None), allow_rejected=False
            )
        if len(raw) > _VERIFY_HTTP_SUCCESS_BODY_MAX_BYTES:
            raise RuntimeError("verify service success response is too large")
        decoded = json.loads(raw.decode("utf-8"))
        if isinstance(decoded, dict) and scheduler is not None:
            if _VERIFY_SCHEDULER_METADATA_FIELD in decoded:
                raise RuntimeError("verify service returned a reserved gateway field")
            decoded[_VERIFY_SCHEDULER_METADATA_FIELD] = scheduler
        return decoded
    except urllib.error.HTTPError as exc:
        # FastAPI's bounded string ``detail`` carries actionable preflight errors
        # (for example, one mistyped fact citation).  urllib otherwise discards
        # it and leaves only "Bad Request".  Never persist arbitrary HTML,
        # structured validation input, or an unbounded service response.
        scheduler = _parse_verify_scheduler_headers(
            getattr(exc, "headers", None), allow_rejected=True
        )
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
        raise _VerifierRequestError(
            f"verify service HTTP {exc.code}{suffix}", scheduler=scheduler
        ) from exc


def _verify_health_url(verify_url: str) -> str:
    parsed = urllib.parse.urlsplit(verify_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("DANUS_VERIFY_URL must be an absolute HTTP(S) /verify URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/verify"):
        raise RuntimeError("DANUS_VERIFY_URL path must end in /verify")
    health_path = path[: -len("/verify")] + "/health"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))


def _verify_service_health_preflight(
    verify_url: str, *, timeout: int
) -> tuple[str, str]:
    """Attest one exact service instance; return ``(nonce, bundle_digest)``."""
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

    pid = health.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("verify service health omitted an exact positive pid")
    instance_nonce = health.get("instance_nonce")
    if not isinstance(instance_nonce, str) or not (
        instance_nonce == "standalone"
        or re.fullmatch(r"[0-9a-f]{32}", instance_nonce) is not None
    ):
        raise RuntimeError("verify service health omitted a valid instance nonce")

    protocol = health.get("output_protocol_version")
    if isinstance(protocol, bool) or protocol != VERIFICATION_OUTPUT_PROTOCOL_VERSION:
        raise RuntimeError(
            "verify service output protocol mismatch: expected "
            f"{VERIFICATION_OUTPUT_PROTOCOL_VERSION}, got {protocol!r}"
        )
    digest = health.get("verifier_bundle_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("verify service health omitted a valid bundle digest")
    if set(health) != _VERIFY_HEALTH_FIELDS:
        raise RuntimeError("verify service health returned an inexact contract")
    return instance_nonce, digest


def _parse_verify_scheduler_headers(
    headers: Any, *, allow_rejected: bool
) -> Optional[Dict[str, Any]]:
    """Parse only the scheduler's bounded digest/count telemetry headers."""

    if headers is None or not hasattr(headers, "get"):
        return None
    source = headers.get("X-Danus-Verify-Scheduler")
    key = headers.get("X-Danus-Verify-Key")
    wait = headers.get("X-Danus-Verify-Wait-Ms")
    rejection = headers.get("X-Danus-Verify-Rejection")
    if source is None and key is None and wait is None and rejection is None:
        return None
    if (
        source not in _VERIFY_SCHEDULER_SOURCES
        or (source == "rejected" and not allow_rejected)
        or not isinstance(key, str)
        or re.fullmatch(r"[0-9a-f]{64}", key) is None
        or not isinstance(wait, str)
        or re.fullmatch(r"0|[1-9][0-9]{0,9}", wait) is None
    ):
        raise RuntimeError("verify service returned invalid scheduler headers")
    wait_ms = int(wait)
    if wait_ms > _VERIFY_SCHEDULER_WAIT_MAX_MS:
        raise RuntimeError("verify service returned invalid scheduler wait telemetry")
    if source == "rejected":
        if rejection not in _VERIFY_SCHEDULER_REJECTIONS or wait_ms != 0:
            raise RuntimeError(
                "verify service returned inconsistent scheduler rejection telemetry"
            )
    elif rejection is not None:
        raise RuntimeError(
            "verify service returned scheduler rejection telemetry for "
            "non-rejected work"
        )
    if source == "cache_hit" and (wait_ms != 0 or allow_rejected):
        raise RuntimeError(
            "verify service returned inconsistent scheduler cache telemetry"
        )
    if source == "cache_hit":
        outcome = "cache"
    elif source == "coalesced":
        outcome = "coalesced"
    elif source == "rejected":
        outcome = "rejected"
    elif wait_ms > 0:
        outcome = "queued"
    else:
        outcome = "launched"
    parsed: Dict[str, Any] = {
        "outcome": outcome,
        "source": source,
        "request_key_sha256": key,
        "wait_ms": wait_ms,
    }
    if rejection is not None:
        parsed["rejection"] = rejection
    return parsed


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
        raise ValueError(
            "DANUS_VERIFY_CONTEXT_MAX_CHARS must be a non-negative integer"
        )
    return value


def _verify_max_prompt_bytes() -> int:
    raw = os.environ.get("DANUS_VERIFY_MAX_PROMPT_BYTES", "1000000")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"DANUS_VERIFY_MAX_PROMPT_BYTES must be a positive integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError("DANUS_VERIFY_MAX_PROMPT_BYTES must be a positive integer")
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
        reasons.append(
            "missing predecessor fact_ids: " + ", ".join(str(x) for x in missing)
        )
    if revoked:
        reasons.append(
            "revoked predecessor fact_ids: " + ", ".join(str(x) for x in revoked)
        )
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


def _reconcile_exact_reuse_candidate(
    *,
    graph: FactGraph,
    project_dir: Path,
    author: str,
    candidate_fact_id: str,
    candidate_fact_identity: str,
    source_id: Optional[str],
    statement: str,
    proof: str,
    predecessors: List[str],
    glossary_introduces: Dict[str, str],
) -> Dict[str, Any]:
    """Close only the exact candidate overlay left by a post-add crash."""

    if _role() != "worker":
        return {}
    try:
        from danus.coordination import (
            CoordinationStore,
            candidate_receipt_id,
            coordination_config,
        )
        from danus.coordination.store import load_project_metadata

        config = coordination_config(load_project_metadata(project_dir))
        if not config.reasoning_first:
            return {}
        store = CoordinationStore.open_existing(project_dir)
        if store is None:
            raise RuntimeError(
                "reasoning-first project has no canonical coordination store"
            )
        active = store.project_status().get("candidate")
        matching_active = (
            isinstance(active, dict)
            and active.get("state") == "active"
            and active.get("worker") == author
            and active.get("candidate_fact_id") == candidate_fact_id
            and active.get("candidate_fact_identity") == candidate_fact_identity
            and active.get("source_id") == source_id
            and isinstance(active.get("slot_id"), str)
        )
        if matching_active:
            bare_context_digest = active.get("context_digest")
            if (
                not isinstance(bare_context_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", bare_context_digest) is None
            ):
                raise RuntimeError("candidate receipt has no canonical bound context")
            receipt = candidate_receipt_id(
                slot_id=active["slot_id"],
                candidate_fact_id=candidate_fact_id,
                candidate_fact_identity=candidate_fact_identity,
                source_id=source_id,
                context_digest=bare_context_digest,
            )
            if active.get("candidate_receipt_id") != receipt:
                return {}
            slot_id = active["slot_id"]
        else:
            provenance = store.paid_slot_provenance(author)
            if provenance is None or not isinstance(provenance.get("slot_id"), str):
                return {}
            slot_id = provenance["slot_id"]
        # Re-attest the short and full active identity while retaining the graph
        # snapshot through the coordination transition. A concurrent revoke,
        # semantic drift, or forced short-id collision cannot release another
        # candidate overlay.
        with graph.locked_active_exact_identity(
            problem_id=os.environ.get("DANUS_PROBLEM_ID", project_dir.name),
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
        ) as active_identity:
            if active_identity != (candidate_fact_id, candidate_fact_identity):
                return {}
            if matching_active:
                terminal = store.terminalize_candidate(
                    author,
                    receipt,
                    slot_id=slot_id,
                    outcome="correct",
                )
            else:
                terminal = store.record_exact_fact_reuse(
                    author,
                    slot_id=slot_id,
                    candidate_fact_id=candidate_fact_id,
                    candidate_fact_identity=candidate_fact_identity,
                    source_id=source_id,
                )
        if terminal.get("state") != "terminal" or terminal.get("outcome") != "correct":
            raise RuntimeError("candidate reconciliation did not become terminal")
        return {
            "candidate_receipt_id": terminal["candidate_receipt_id"],
            "candidate_outcome": "correct",
        }
    except Exception as exc:
        return {
            "candidate_terminalization_error": _bounded_exception_detail(exc),
        }


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
    wire_result = dict(result)
    scheduler = wire_result.pop(_VERIFY_SCHEDULER_METADATA_FIELD, None)
    if scheduler is not None:
        required = {"outcome", "source", "request_key_sha256", "wait_ms"}
        allowed = required | {"rejection"}
        if (
            not isinstance(scheduler, dict)
            or not required.issubset(scheduler)
            or not set(scheduler).issubset(allowed)
            or scheduler.get("outcome")
            not in {"launched", "queued", "coalesced", "cache", "rejected"}
            or scheduler.get("source") not in _VERIFY_SCHEDULER_SOURCES
            or not isinstance(scheduler.get("request_key_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(scheduler["request_key_sha256"]))
            is None
            or isinstance(scheduler.get("wait_ms"), bool)
            or not isinstance(scheduler.get("wait_ms"), int)
            or not 0 <= scheduler["wait_ms"] <= _VERIFY_SCHEDULER_WAIT_MAX_MS
            or (
                "rejection" in scheduler
                and scheduler["rejection"] not in _VERIFY_SCHEDULER_REJECTIONS
            )
        ):
            raise ValueError("verify service returned invalid scheduler metadata")
    actual_fields = set(wire_result)
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
    verdict_payload = {key: wire_result[key] for key in _OUTPUT_RESULT_FIELDS}
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
    if wire_result.get("verification_context_digest") != context.get("digest"):
        raise ValueError(
            "verify service did not attest the supplied fact context digest; "
            "refusing a possibly context-free verdict"
        )
    metrics = wire_result.get("verification_metrics")
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
            raise ValueError(
                "verification_metrics.elapsed_seconds must be non-negative"
            )
        tokens = metrics.get("tokens_used")
        if tokens is not None and (
            isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
        ):
            raise ValueError(
                "verification_metrics.tokens_used must be non-negative or null"
            )
        scope = context.get("scope", {})
        if metrics.get("context_round") != scope.get("expansion_round"):
            raise ValueError(
                "verification_metrics.context_round does not match context"
            )
        if metrics.get("expanded_proof_ids") != scope.get("expanded_proof_ids"):
            raise ValueError(
                "verification_metrics.expanded_proof_ids does not match context"
            )
    safe = _redact_verifier_result(wire_result)
    if scheduler is not None:
        safe[_VERIFY_SCHEDULER_METADATA_FIELD] = dict(scheduler)
    return safe


# --------------------------------------------------------------------------- #
# global memory                                                               #
# --------------------------------------------------------------------------- #


def _append_global_memory_guarded(
    memory: GlobalMemory,
    project_dir: Path,
    kind: str,
    *,
    claim: str,
    evidence: str,
    author: str,
    verifiable: Optional[bool] = None,
    glossary: Optional[Dict[str, str]] = None,
    links: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> str:
    """The sanctioned gateway seam for non-``gm_add`` GM publications."""

    from danus.strategy.browser_advisor import BrowserAdvisorBroker

    durable_fields: dict[str, object] = {
        "kind": kind,
        "claim": claim,
        "evidence": evidence,
        "author": author,
        "glossary": glossary,
        "links": links,
        **extra,
    }
    with BrowserAdvisorBroker.project_memory_fence(project_dir):
        BrowserAdvisorBroker.reject_raw_project_text_locked(
            project_dir, fields=durable_fields
        )
        return memory.append(
            kind,
            claim=claim,
            evidence=evidence,
            author=author,
            verifiable=verifiable,
            glossary=glossary,
            links=links,
            **extra,
        )


def _validate_bounded_review_memory_record(
    kind: str,
    *,
    claim: str,
    evidence: str,
    author: str,
    verifiable: Optional[bool],
    glossary: Optional[Dict[str, str]],
    links: Optional[Dict[str, Any]],
    extra: Dict[str, Any],
) -> None:
    """Bound the exact review-sensitive GM record before durable append."""

    from danus.coordination.policy import MAX_REVIEW_RECORD_BYTES

    effective_verifiable = GLOBAL_KINDS[kind] if verifiable is None else verifiable
    projected = {
        "id": "0" * 16,
        "timestamp_utc": "9999-12-31T23:59:59.999999+00:00",
        "author": author,
        "kind": kind,
        "claim": claim,
        "evidence": evidence,
        "verifiable": effective_verifiable,
        "status": "unverified" if effective_verifiable else "open",
        "fact_id": None,
        "links": links or {},
        "glossary": glossary or {},
        **extra,
    }
    try:
        encoded = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("review memory record is not strict canonical JSON") from exc
    if len(encoded) > MAX_REVIEW_RECORD_BYTES:
        raise ValueError("review memory record exceeds its 16 KiB hard limit")


def _strict_canonical_json(value: object, *, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{label} is not strict canonical JSON") from exc


def _reasoning_sensitive_memory_replay(
    memory: GlobalMemory,
    publication: Dict[str, Any],
    *,
    kind: str,
    claim: str,
    evidence: str,
    author: str,
    verifiable: Optional[bool],
    glossary: Optional[Dict[str, str]],
    links: Optional[Dict[str, Any]],
    extra: Dict[str, Any],
) -> Optional[str]:
    """Reuse one exact GM orphan or authorize the sole new sensitive append."""

    expected = {
        "author": author,
        "kind": kind,
        "claim": claim,
        "evidence": evidence,
        "verifiable": GLOBAL_KINDS[kind] if verifiable is None else verifiable,
        "links": links or {},
        "glossary": glossary or {},
        **extra,
    }
    expected_bytes = _strict_canonical_json(
        expected,
        label="sensitive global-memory replay identity",
    )
    expected_provenance = {
        "slot_id": publication["slot_id"],
        "generation": publication["generation"],
        "lane": publication["lane"],
    }
    matches: list[Dict[str, Any]] = []
    for evidence_kind in ("obstacle", "dead_end"):
        for entry in memory.read(evidence_kind):
            if not isinstance(entry, dict) or entry.get("author") != author:
                continue
            entry_links = entry.get("links")
            if (
                not isinstance(entry_links, dict)
                or entry_links.get("coordination") != expected_provenance
            ):
                continue
            if (
                publication["lane"] == "critic"
                and entry_links.get("confirms_entry_id") is None
            ):
                continue
            matches.append(entry)
            if len(matches) > 1:
                raise RuntimeError(
                    "sensitive global-memory slot has multiple orphan entries"
                )

    if not matches:
        if publication["requires_existing_replay"]:
            raise RuntimeError(
                "coordination registration has no exact durable memory entry"
            )
        return None

    match = matches[0]
    entry_id = match.get("id")
    if not isinstance(entry_id, str) or _FACT_ID_RE.fullmatch(entry_id) is None:
        raise RuntimeError("sensitive global-memory orphan has invalid entry id")
    registered_entry_id = publication.get("registered_entry_id")
    if registered_entry_id is not None and registered_entry_id != entry_id:
        raise RuntimeError(
            "coordination registration conflicts with durable memory entry id"
        )
    observed = {
        key: value
        for key, value in match.items()
        if key not in {"id", "timestamp_utc", "status", "fact_id"}
    }
    observed_bytes = _strict_canonical_json(
        observed,
        label="durable sensitive global-memory orphan",
    )
    if observed_bytes != expected_bytes:
        raise RuntimeError(
            "sensitive global-memory retry conflicts with the durable orphan"
        )
    return entry_id


def _bind_advisor_checkpoint_recommendation(
    project_dir: Path,
    links: Optional[Dict[str, Any]],
    *,
    require_current: bool,
) -> tuple[Dict[str, Any], Optional[str]]:
    """Parse, and optionally attest, a checkpoint recommendation binding."""

    from danus.coordination import CoordinationStore, coordination_config
    from danus.coordination.store import load_project_metadata

    protected_links = dict(links or {})
    config = coordination_config(load_project_metadata(project_dir))
    if not config.reasoning_first:
        if "recommendation_id" in protected_links:
            raise RuntimeError(
                "legacy advisor_checkpoint cannot claim a coordinator recommendation"
            )
        return protected_links, None
    recommendation_id = protected_links.get("recommendation_id")
    if (
        not isinstance(recommendation_id, str)
        or _COORDINATION_IDENTIFIER_RE.fullmatch(recommendation_id) is None
    ):
        raise RuntimeError(
            "reasoning-first advisor_checkpoint requires an exact current "
            "recommendation id"
        )
    if not require_current:
        return protected_links, recommendation_id
    store = CoordinationStore.open_existing(project_dir)
    if store is None:
        raise RuntimeError(
            "reasoning-first project has no canonical coordination store"
        )
    try:
        recommendation = store.validate_open_recommendation(recommendation_id)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        raise RuntimeError(
            "reasoning-first advisor_checkpoint requires the exact current ready "
            "recommendation"
        ) from exc
    if (
        recommendation.get("recommendation_id") != recommendation_id
        or recommendation.get("state") != "owner_action_required"
        or recommendation.get("ready") is not True
        or recommendation.get("browser_dispatch_authorized") is not False
        or recommendation.get("advisor_request_id") is not None
    ):
        raise RuntimeError(
            "reasoning-first advisor_checkpoint recommendation binding is inexact"
        )
    # The caller must name the exact recommendation, while the gateway owns the
    # canonical durable link that reaches GlobalMemory.append.
    protected_links["recommendation_id"] = recommendation_id
    return protected_links, recommendation_id


def _advisor_checkpoint_replay(
    memory: GlobalMemory,
    *,
    recommendation_id: Optional[str],
    claim: str,
    evidence: str,
    author: str,
    verifiable: Optional[bool],
    glossary: Optional[Dict[str, str]],
    links: Dict[str, Any],
    extra: Dict[str, Any],
) -> Optional[str]:
    """Reuse one exact checkpoint for a recommendation or fail closed."""

    expected = {
        "author": author,
        "kind": "advisor_checkpoint",
        "claim": claim,
        "evidence": evidence,
        "verifiable": (
            GLOBAL_KINDS["advisor_checkpoint"] if verifiable is None else verifiable
        ),
        "links": links,
        "glossary": glossary or {},
        **extra,
    }
    expected_bytes = _strict_canonical_json(
        expected,
        label="advisor checkpoint replay identity",
    )
    matches: list[Dict[str, Any]] = []
    for projected in memory.iter_immutable("advisor_checkpoint"):
        projected_links = projected.get("links")
        if not isinstance(projected_links, dict):
            raise RuntimeError("advisor checkpoint has invalid durable links")
        if recommendation_id is None:
            if "recommendation_id" in projected_links:
                raise RuntimeError(
                    "legacy advisor checkpoint has an invalid recommendation link"
                )
        elif projected_links.get("recommendation_id") != recommendation_id:
            continue
        entry_id = projected.get("id")
        if not isinstance(entry_id, str) or _FACT_ID_RE.fullmatch(entry_id) is None:
            raise RuntimeError("advisor checkpoint has an invalid durable entry id")
        observed = {
            key: value
            for key, value in projected.items()
            if key not in {"id", "timestamp_utc", "status", "fact_id"}
        }
        observed_bytes = _strict_canonical_json(
            observed,
            label="durable advisor checkpoint replay",
        )
        if recommendation_id is not None and observed_bytes != expected_bytes:
            raise RuntimeError(
                "advisor checkpoint retry conflicts with the durable checkpoint"
            )
        if observed_bytes == expected_bytes:
            matches.append(projected)
            if len(matches) > 1:
                raise RuntimeError(
                    "advisor checkpoint has multiple exact durable replays"
                )

    if not matches:
        return None
    match = matches[0]
    return str(match["id"])


def _validate_advisor_checkpoint_projected_record(
    *,
    claim: str,
    evidence: str,
    author: str,
    verifiable: Optional[bool],
    glossary: Optional[Dict[str, str]],
    links: Dict[str, Any],
    extra: Dict[str, Any],
) -> None:
    """Reject an unbindable checkpoint before the append can become durable."""

    effective_verifiable = (
        GLOBAL_KINDS["advisor_checkpoint"] if verifiable is None else verifiable
    )
    projected = {
        "id": "0" * 16,
        "timestamp_utc": "9999-12-31T23:59:59.999999+00:00",
        "author": author,
        "kind": "advisor_checkpoint",
        "claim": claim,
        "evidence": evidence,
        "verifiable": effective_verifiable,
        "status": "unverified" if effective_verifiable else "open",
        "fact_id": None,
        "links": links,
        "glossary": glossary or {},
        **extra,
    }
    canonical_global_memory_record(projected)


def gm_add(
    kind: str,
    claim: str,
    evidence: str = "",
    verifiable: Optional[bool] = None,
    glossary: Optional[Dict[str, str]] = None,
    links: Optional[Dict[str, Any]] = None,
    consult_provenance: Optional[Dict[str, Any]] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish a finding to shared global memory (claim + evidence). Verifiable
    kinds (conclusion/example/counterexample/proof_attempt) require explicit
    evidence; judgments (plan/direction/obstacle/master_guidance/elaboration) do
    not. Define your symbols in ``glossary`` and reuse project terminology.

    Main agent: pass ``project`` to target one of several projects by name;
    workers omit it (pinned to their own project)."""
    role = _role()
    if kind in {
        "master_guidance",
        "elaboration",
        "advisor_checkpoint",
    } and role not in {
        "main",
        "all",
    }:
        raise RuntimeError(f"only the main role may create {kind}")
    target_project_dir = _project(project)

    checkpoint_project_dir: Optional[Path] = None
    checkpoint_fact_ids: list[str] = []
    checkpoint_recommendation_id: Optional[str] = None
    checkpoint_identity: Optional[Dict[str, Any]] = None
    if kind == "advisor_checkpoint":
        validate_advisor_checkpoint(claim, evidence, links)
        if verifiable is not None and verifiable is not False:
            raise ValueError("advisor_checkpoint must remain non-verifiable strategy")
        checkpoint_project_dir = target_project_dir
        checkpoint_fact_ids = list((links or {}).get("fact_ids", []))

    has_consult_metrics = any(
        value is not None for value in (input_tokens, output_tokens, cost_usd)
    )
    if (
        consult_provenance is not None or has_consult_metrics
    ) and kind != "master_guidance":
        raise ValueError(
            "consult provenance/metrics are valid only for master_guidance"
        )
    for label, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{label} must be a non-negative integer or null")
    if cost_usd is not None and (
        isinstance(cost_usd, bool)
        or not isinstance(cost_usd, (int, float))
        or not math.isfinite(float(cost_usd))
        or float(cost_usd) < 0
    ):
        raise ValueError("cost_usd must be a finite non-negative number or null")
    provenance = (
        clean_consult_provenance(consult_provenance)
        if consult_provenance is not None
        else None
    )
    from danus.strategy.browser_advisor import BrowserAdvisorBroker

    author = _author()
    extra = {"consult_provenance": provenance} if provenance is not None else {}
    if has_consult_metrics:
        extra.update(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": None if cost_usd is None else float(cost_usd),
            }
        )
    memory = GlobalMemory(target_project_dir)
    coordination_store: Any = None
    coordination_slot: Optional[Dict[str, Any]] = None
    coordination_publication: Optional[Dict[str, Any]] = None
    coordination_pending_reconciliation = False

    def append_memory() -> str:
        return memory.append(
            kind,
            claim=claim,
            evidence=evidence,
            author=author,
            verifiable=verifiable,
            glossary=glossary,
            links=links,
            **extra,
        )

    # Lock order is supervisor browser-output fence -> optional FactGraph
    # snapshot -> one GlobalMemory JSONL lock.  Browser completion/clarification
    # uses the same outer fence before registering its digest, making the raw
    # output check and append linearizable across processes.
    with BrowserAdvisorBroker.project_memory_fence(target_project_dir):
        if checkpoint_project_dir is not None:
            links, checkpoint_recommendation_id = (
                _bind_advisor_checkpoint_recommendation(
                    checkpoint_project_dir,
                    links,
                    require_current=False,
                )
            )
        if role == "worker":
            from danus.coordination import CoordinationStore, coordination_config
            from danus.coordination.store import load_project_metadata

            config = coordination_config(load_project_metadata(target_project_dir))
            if config.reasoning_first:
                coordination_store = CoordinationStore.open_existing(target_project_dir)
                if coordination_store is None:
                    raise RuntimeError(
                        "reasoning-first project has no canonical coordination store"
                    )
                coordination_slot = coordination_store.paid_slot_provenance(author)
                if coordination_slot is None:
                    raise RuntimeError(
                        "worker has no canonical active reasoning-first paid slot"
                    )
                if links is not None and not isinstance(links, dict):
                    raise ValueError("links must be an object")
                protected_links = dict(links or {})
                supplied = protected_links.get("coordination")
                if supplied is not None and supplied != coordination_slot:
                    raise ValueError(
                        "links.coordination is protected canonical provenance"
                    )
                protected_links["coordination"] = dict(coordination_slot)
                links = protected_links
            elif isinstance(links, dict) and "coordination" in links:
                raise ValueError("links.coordination is protected canonical provenance")
        elif isinstance(links, dict) and "coordination" in links:
            raise ValueError("links.coordination is protected canonical provenance")

        confirms_entry_id = (
            links.get("confirms_entry_id") if isinstance(links, dict) else None
        )
        if confirms_entry_id is not None and (
            not isinstance(confirms_entry_id, str)
            or _COORDINATION_IDENTIFIER_RE.fullmatch(confirms_entry_id) is None
        ):
            raise ValueError("links.confirms_entry_id must be a bounded entry id")

        if coordination_store is not None and coordination_slot is not None:
            coordination_publication = coordination_store.validate_memory_publication(
                author,
                slot_id=coordination_slot["slot_id"],
                kind=kind,
                confirms_entry_id=confirms_entry_id,
            )
            if coordination_publication["bounded_review_record"]:
                _validate_bounded_review_memory_record(
                    kind,
                    claim=claim,
                    evidence=evidence,
                    author=author,
                    verifiable=verifiable,
                    glossary=glossary,
                    links=links,
                    extra=extra,
                )

        checkpoint_replay_entry_id = None
        if checkpoint_project_dir is not None:
            assert isinstance(links, dict)
            _validate_advisor_checkpoint_projected_record(
                claim=claim,
                evidence=evidence,
                author=author,
                verifiable=verifiable,
                glossary=glossary,
                links=links,
                extra=extra,
            )
            checkpoint_replay_entry_id = _advisor_checkpoint_replay(
                memory,
                recommendation_id=checkpoint_recommendation_id,
                claim=claim,
                evidence=evidence,
                author=author,
                verifiable=verifiable,
                glossary=glossary,
                links=links,
                extra=extra,
            )
            if checkpoint_replay_entry_id is None:
                links, checkpoint_recommendation_id = (
                    _bind_advisor_checkpoint_recommendation(
                        checkpoint_project_dir,
                        links,
                        require_current=True,
                    )
                )

        browser_adopted = False
        if provenance is not None and provenance["transport"] == "chatgpt_pro_browser":
            BrowserAdvisorBroker.validate_adopted_master_guidance(
                target_project_dir,
                provenance=provenance,
                evidence=evidence,
            )
            browser_adopted = True
        raw_fence_fields: dict[str, object] = {
            "kind": kind,
            "claim": claim,
            "glossary": glossary,
            "links": links,
            "author": author,
            **extra,
        }
        if not browser_adopted:
            raw_fence_fields["evidence"] = evidence
        if checkpoint_replay_entry_id is None:
            BrowserAdvisorBroker.reject_raw_project_text_locked(
                target_project_dir,
                fields=raw_fence_fields,
            )

        replay_entry_id = None
        if (
            coordination_publication is not None
            and coordination_publication["bounded_review_record"]
        ):
            replay_entry_id = _reasoning_sensitive_memory_replay(
                memory,
                coordination_publication,
                kind=kind,
                claim=claim,
                evidence=evidence,
                author=author,
                verifiable=verifiable,
                glossary=glossary,
                links=links,
                extra=extra,
            )

        if replay_entry_id is not None:
            entry_id = replay_entry_id
        elif checkpoint_replay_entry_id is not None:
            entry_id = checkpoint_replay_entry_id
        elif checkpoint_project_dir is not None:
            graph = FactGraph(checkpoint_project_dir)
            # Keep the graph's shared snapshot lock through the append so a
            # revoke cannot invalidate a link between validation and durable
            # publication.
            with graph.locked_context(
                checkpoint_fact_ids,
                predecessor_depth=0,
                proof_mode="none",
                include_project_glossary=False,
            ) as context:
                active_ids = {str(item["fact_id"]) for item in context["facts"]}
                if not context["complete"] or active_ids != set(checkpoint_fact_ids):
                    raise ValueError(
                        "advisor_checkpoint fact ids must all be active verified facts; "
                        f"missing={context['missing_fact_ids']}, "
                        f"revoked={context['revoked_fact_ids']}"
                    )
                entry_id = append_memory()
        else:
            entry_id = append_memory()

        if checkpoint_project_dir is not None:
            immutable_checkpoint = memory.get_immutable_in_kind(
                "advisor_checkpoint", entry_id
            )
            immutable_bytes = canonical_global_memory_record(immutable_checkpoint)
            immutable_links = immutable_checkpoint.get("links")
            if (
                immutable_checkpoint.get("id") != entry_id
                or immutable_checkpoint.get("kind") != "advisor_checkpoint"
                or immutable_checkpoint.get("claim") != claim
                or immutable_checkpoint.get("evidence") != evidence
                or not isinstance(immutable_checkpoint.get("author"), str)
                or not str(immutable_checkpoint["author"]).strip()
                or immutable_links != (links or {})
            ):
                raise RuntimeError(
                    "durable advisor checkpoint does not match its exact publication"
                )
            prompt_bytes = evidence.encode("utf-8")
            checkpoint_identity = {
                "checkpoint_id": entry_id,
                "checkpoint_sha256": hashlib.sha256(immutable_bytes).hexdigest(),
                "checkpoint_bytes": len(immutable_bytes),
                "checkpoint_prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                "checkpoint_prompt_bytes": len(prompt_bytes),
                "recommendation_id": checkpoint_recommendation_id,
            }

        if coordination_store is not None and coordination_slot is not None:
            try:
                if coordination_slot["lane"] == "root" and kind in {
                    "obstacle",
                    "dead_end",
                }:
                    coordination_store.record_root_evidence(
                        author,
                        kind,
                        entry_id=entry_id,
                        slot_id=coordination_slot["slot_id"],
                    )
                elif (
                    coordination_slot["lane"] == "critic"
                    and kind in {"obstacle", "dead_end"}
                    and confirms_entry_id is not None
                ):
                    coordination_store.confirm_root_evidence(
                        author,
                        confirms_entry_id,
                        entry_id=entry_id,
                        slot_id=coordination_slot["slot_id"],
                    )
            except Exception:
                # The GM append is already durable and carries the exact slot
                # provenance needed by terminal reconciliation.  Preserve its
                # ID instead of inviting an unbound duplicate after a cut.
                coordination_pending_reconciliation = True
    response = {"id": entry_id, "kind": kind}
    if checkpoint_identity is not None:
        response.update(checkpoint_identity)
    if coordination_pending_reconciliation:
        response["coordination_pending_reconciliation"] = True
    return response


def gm_search(
    query: str,
    kinds: Optional[List[str]] = None,
    limit_per_kind: int = 10,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """BM25 over shared global-memory findings. Use to reuse others' results,
    avoid duplicate work, and learn which paths already died. Main agent: pass
    ``project`` to search a specific project; workers omit it."""
    return _gm(project).search(query, kinds=kinds, limit_per_kind=limit_per_kind)


def gm_get(entry_id: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve one exact, bounded global-memory entry by id.

    Main agents may select a project by name; workers remain pinned to their
    configured project and omit ``project``.
    """
    return _gm(project).get(entry_id)


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

    target_project_dir = _project()
    fg = FactGraph(target_project_dir)
    gm = GlobalMemory(target_project_dir)
    problem_id = os.environ.get("DANUS_PROBLEM_ID", target_project_dir.name)
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
            statement=statement,
            proof=proof,
            intuition=intuition,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
        )
    except Exception:
        undefined = []

    candidate_fact_id = compute_fact_id(
        problem_id=problem_id,
        predecessors=predecessors,
        glossary_introduces=glossary_introduces,
        statement=statement,
        proof=proof,
    )

    # Reject a glossary redefinition while the attempt is still entirely
    # read-only.  This also re-attests glossary integrity before an active exact
    # fact can be reused.  The shared snapshot is only an optimization, not an
    # authorization: a definition can still race in after this check, so
    # add_if_context_unchanged retains the independent conflict check under its
    # exclusive promotion/CAS lock.
    try:
        glossary_conflicts = fg.glossary_conflicts(glossary_introduces)
    except Exception as exc:
        return {
            "accepted": False,
            "promoted": False,
            "submission_status": "error",
            "verification_verdict": None,
            "verdict": "error",
            "error": "glossary preflight error: " + _bounded_exception_detail(exc),
            "undefined_symbols": undefined,
            "adaptive_rounds": 0,
            "verification_calls": 0,
            "expanded_proof_ids": [],
            "verification_metrics": [],
            "verification_scheduler": [],
        }
    if glossary_conflicts:
        terms = ", ".join(glossary_conflicts)
        return {
            "accepted": False,
            "promoted": False,
            "submission_status": "error",
            "verification_verdict": None,
            "verdict": "error",
            "error": (
                "glossary_conflict: refusing to redefine project terms: " + terms
            ),
            "repair_hints": (
                "Reuse the established definition exactly, remove the conflicting "
                f"entry, or introduce a new symbol for: {terms}. Then resubmit "
                "for a fresh verification."
            ),
            "undefined_symbols": undefined,
            "adaptive_rounds": 0,
            "verification_calls": 0,
            "expanded_proof_ids": [],
            "verification_metrics": [],
            "verification_scheduler": [],
        }

    try:
        reused_identity = fg.lookup_active_exact_identity(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
        )
    except Exception as exc:
        return {
            "accepted": False,
            "promoted": False,
            "submission_status": "error",
            "verification_verdict": None,
            "verdict": "error",
            "error": "active fact reuse error: " + _bounded_exception_detail(exc),
            "undefined_symbols": undefined,
            "verification_calls": 0,
        }
    if reused_identity is not None:
        reused_fact_id, reused_fact_identity = reused_identity
        reuse_candidate = _reconcile_exact_reuse_candidate(
            graph=fg,
            project_dir=target_project_dir,
            author=_author(),
            candidate_fact_id=reused_fact_id,
            candidate_fact_identity=reused_fact_identity,
            source_id=source_id,
            statement=statement,
            proof=proof,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
        )
        trace_error = None
        try:
            _append_global_memory_guarded(
                gm,
                target_project_dir,
                "verification",
                claim=f"active exact fact {reused_fact_id}",
                evidence="active exact fact reused; verifier calls: 0",
                author=_author(),
                verifiable=False,
                links={"source_id": source_id, "predecessors": predecessors},
                verdict="correct",
                fact_id=reused_fact_id,
                write_error=None,
                promoted=True,
                submission_status="promoted",
                verification_verdict="correct",
                verification_report=None,
                verification_context_digest=None,
                verification_rounds=[],
                final_math_verdict="correct",
                expanded_proof_ids=[],
                verification_calls=0,
                verification_reuse="active_exact_fact",
                verification_scheduler=[],
                **reuse_candidate,
                conversation_frontier_at_action=conversation_frontier,
            )
        except Exception as exc:
            trace_error = _bounded_exception_detail(exc)
        response = {
            "accepted": True,
            "promoted": True,
            "submission_status": "promoted",
            "verification_verdict": "correct",
            "verdict": "correct",
            "fact_id": reused_fact_id,
            "undefined_symbols": undefined,
            "adaptive_rounds": 0,
            "verification_calls": 0,
            "expanded_proof_ids": [],
            "verification_metrics": [],
            "verification_scheduler": [],
            "verification_reuse": "active_exact_fact",
            **reuse_candidate,
        }
        if trace_error:
            response["trace_error"] = trace_error
        return response

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

    expanded_proof_ids: List[str] = []
    expansion_round = 0
    verification_rounds: List[Dict[str, Any]] = []
    verification_context: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    protocol_error: Optional[str] = None
    verification_call_count = 0
    candidate_registration_checked = False
    candidate_coordination_store: Any = None
    candidate_coordination_slot: Optional[Dict[str, Any]] = None
    candidate_receipt: Optional[str] = None
    candidate_outcome: Optional[str] = None
    candidate_terminalization_error: Optional[str] = None
    verification_delivery_unknown = False

    def terminalize_candidate(outcome: str) -> None:
        nonlocal candidate_outcome, candidate_terminalization_error
        if candidate_outcome is not None:
            return
        if (
            candidate_coordination_store is None
            or candidate_coordination_slot is None
            or candidate_receipt is None
        ):
            return
        candidate_outcome = outcome
        try:
            candidate_coordination_store.terminalize_candidate(
                _author(),
                candidate_receipt,
                slot_id=candidate_coordination_slot["slot_id"],
                outcome=outcome,
            )
        except Exception as exc:
            candidate_terminalization_error = _bounded_exception_detail(exc)

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

        if not candidate_registration_checked:
            candidate_registration_checked = True
            try:
                from danus.coordination import (
                    CoordinationStore,
                    candidate_receipt_id,
                    coordination_config,
                )
                from danus.coordination.store import load_project_metadata

                coordination = coordination_config(
                    load_project_metadata(target_project_dir)
                )
                if _role() == "worker" and coordination.reasoning_first:
                    candidate_coordination_store = CoordinationStore.open_existing(
                        target_project_dir
                    )
                    if candidate_coordination_store is None:
                        raise RuntimeError(
                            "reasoning-first project has no canonical coordination store"
                        )
                    candidate_coordination_slot = (
                        candidate_coordination_store.paid_slot_provenance(_author())
                    )
                    if candidate_coordination_slot is None:
                        raise RuntimeError(
                            "worker has no canonical active reasoning-first paid slot"
                        )
                    digest = verification_context.get("digest")
                    if (
                        not isinstance(digest, str)
                        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                    ):
                        raise RuntimeError(
                            "candidate verification context has no canonical digest"
                        )
                    bare_context_digest = digest.removeprefix("sha256:")
                    candidate_fact_identity = fact_identity_from_verification_context(
                        verification_context=verification_context,
                        problem_id=problem_id,
                        predecessors=predecessors,
                        glossary_introduces=glossary_introduces,
                        statement=statement,
                        proof=proof,
                    )
                    candidate_receipt = candidate_receipt_id(
                        slot_id=candidate_coordination_slot["slot_id"],
                        candidate_fact_id=candidate_fact_id,
                        candidate_fact_identity=candidate_fact_identity,
                        source_id=source_id,
                        context_digest=bare_context_digest,
                    )
                    registration = candidate_coordination_store.register_candidate(
                        _author(),
                        candidate_receipt,
                        slot_id=candidate_coordination_slot["slot_id"],
                        candidate_fact_id=candidate_fact_id,
                        candidate_fact_identity=candidate_fact_identity,
                        source_id=source_id,
                        context_digest=bare_context_digest,
                    )
                    registration_state = registration.get("state")
                    if registration_state in {"terminal", "outcome_unknown"}:
                        prior_outcome = registration.get("outcome")
                        candidate_outcome = (
                            str(prior_outcome)
                            if isinstance(prior_outcome, str)
                            else "outcome_unknown"
                        )
                        protocol_error = (
                            "candidate coordination error: exact candidate receipt "
                            f"is already {registration_state}"
                        )
                        break
                    if registration_state != "active":
                        raise RuntimeError(
                            "candidate coordination did not return an active receipt"
                        )
            except Exception as exc:
                protocol_error = (
                    "candidate coordination error: " + _bounded_exception_detail(exc)
                )
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
            if isinstance(
                exc,
                (OSError, TimeoutError, urllib.error.URLError),
            ):
                verification_delivery_unknown = True
            message = _bounded_exception_detail(exc)
            failed_round = {
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
            failed_scheduler = getattr(exc, "scheduler", None)
            if isinstance(failed_scheduler, dict):
                failed_round["verification_scheduler"] = dict(failed_scheduler)
            verification_rounds.append(failed_round)
            protocol_error = message
            break

        round_scheduler = result.pop(_VERIFY_SCHEDULER_METADATA_FIELD, None)
        requests = result["needs_expanded_proofs"]
        completed_round = {
            "round": expansion_round,
            "context_fact_ids": list(verification_context["scope"]["closure_fact_ids"]),
            "context_digest": verification_context["digest"],
            "expanded_proof_ids": list(
                verification_context["scope"]["expanded_proof_ids"]
            ),
            "verification_status": result["verification_status"],
            "verdict": result["verdict"],
            "needs_expanded_proofs": [dict(request) for request in requests],
            "verification_metrics": result.get("verification_metrics"),
        }
        if isinstance(round_scheduler, dict):
            completed_round["verification_scheduler"] = dict(round_scheduler)
        verification_rounds.append(completed_round)

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
        "verification_scheduler": [
            dict(round_trace["verification_scheduler"])
            for round_trace in verification_rounds
            if isinstance(round_trace.get("verification_scheduler"), dict)
        ],
    }

    if protocol_error is not None:
        if candidate_outcome is None:
            if verification_delivery_unknown:
                terminalize_candidate("outcome_unknown")
            elif (
                verification_rounds
                and verification_rounds[-1].get("verification_status")
                == "needs_context"
            ):
                terminalize_candidate("needs_context")
            else:
                terminalize_candidate("error")
        if candidate_receipt is not None:
            adaptive_metadata.update(
                {
                    "candidate_receipt_id": candidate_receipt,
                    "candidate_outcome": candidate_outcome,
                    "candidate_terminalization_error": (
                        candidate_terminalization_error
                    ),
                }
            )
        candidate_trace = (
            {
                "candidate_receipt_id": candidate_receipt,
                "candidate_outcome": candidate_outcome,
                "candidate_terminalization_error": candidate_terminalization_error,
            }
            if candidate_receipt is not None
            else {}
        )
        trace_error = None
        try:
            _append_global_memory_guarded(
                gm,
                target_project_dir,
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
                **candidate_trace,
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
                problem_id=problem_id,
                author=_author(),
                statement=statement,
                proof=proof,
                predecessors=predecessors,
                glossary_introduces=glossary_introduces,
                intuition=intuition,
                external_refs=external_refs,
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

    if promotion_unknown:
        terminalize_candidate("promotion_unknown")
    elif accepted and promoted:
        terminalize_candidate("correct")
    elif not accepted:
        terminalize_candidate("wrong")
    else:
        terminalize_candidate("error")
    if candidate_receipt is not None:
        adaptive_metadata.update(
            {
                "candidate_receipt_id": candidate_receipt,
                "candidate_outcome": candidate_outcome,
                "candidate_terminalization_error": candidate_terminalization_error,
            }
        )
    candidate_trace = (
        {
            "candidate_receipt_id": candidate_receipt,
            "candidate_outcome": candidate_outcome,
            "candidate_terminalization_error": candidate_terminalization_error,
        }
        if candidate_receipt is not None
        else {}
    )

    # 4) Record the outcome. A trace I/O failure must not hide an accepted fact id.
    trace_error = None
    try:
        _append_global_memory_guarded(
            gm,
            target_project_dir,
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
            **candidate_trace,
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


def fact_search(
    query: str, limit: int = 10, project: Optional[str] = None
) -> Dict[str, Any]:
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


def fact_revoke(
    fact_id: str, reason: str, project: Optional[str] = None
) -> Dict[str, Any]:
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
    empty ``results``. Do not bypass a configured retrieval gate with another
    search channel; continue without external material or ask the owner to fix
    the declared retrieval policy."""
    return _arxiv_search(query, num_results=num_results)


# --------------------------------------------------------------------------- #
# role-based registration                                                     #
# --------------------------------------------------------------------------- #

_TOOLS = {
    "gm_add": gm_add,
    "gm_get": gm_get,
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
