"""Danus verify service — the mathematical authority behind the write-gate.

    POST /verify {expected_verifier_instance_nonce,
                  expected_output_protocol_version,
                  expected_verifier_bundle_digest,
                  statement, proof, glossary_introduces?, fact_context?}
      -> {output_schema_version, verification_status, verification_report,
          verdict, needs_expanded_proofs, repair_hints,
          verification_context_digest?, verification_metrics?}
    GET  /health                    -> {status: "ok", pid: <int>,
                                       instance_nonce: <guardian nonce>,
                                       output_protocol_version: 3,
                                       verifier_bundle_digest: <sha256>}
    GET  /scheduler                 -> bounded counts/limits/counters only

/verify runs the deterministic pre-checks (``prechecks.run_prechecks``) and, if
they pass, enters a bounded FIFO scheduler. Exact duplicates coalesce or reuse a
nonce-bound completed-result cache; a distinct leader cold-starts a fresh codex
verifier (``launcher.run_codex_verification``)
whose verdict the gateway's ``fact_submit`` uses to decide whether a claim becomes
a fact. The verifier is an LLM, NOT a formal proof assistant, with no human in the
loop by default — see the verifier contract (``agents/contracts/verifier.md``).
"""

from __future__ import annotations

import json
import os
import re
import asyncio
import threading
from typing import Any, Dict, Mapping, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from danus.core import (
    VERIFICATION_OUTPUT_PROTOCOL_VERSION,
    VERIFICATION_CONTEXT_PROJECTION,
    VERIFICATION_CONTEXT_SCHEMA_VERSION,
    validate_verification_output,
    verification_context_digest,
)
from danus.gateway_runtime import GatewayRuntimeUnavailable, require_gateway_runtime

from .launcher import (
    VERIFIER_BUNDLE_DIGEST,
    VerificationExecutionProfile,
    _allocate_run_id,
    capture_execution_profile,
    run_codex_verification,
)
from .prechecks import run_prechecks
from .scheduler import (
    SCHEDULER_KEY_SCHEMA,
    SchedulerLimits,
    SchedulerReceipt,
    SchedulerRejected,
    SchedulerWorkFailed,
    VerificationScheduler,
    canonical_sha256,
)


_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_CITED_FACT_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")
_UNAVAILABLE_CONTEXT_FIELDS = (
    "missing_fact_ids",
    "revoked_fact_ids",
    "omitted_fact_ids",
    "omitted_glossary_terms",
    "omitted_expanded_proof_ids",
)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _condition_wait_int_env(name: str, default: int) -> int:
    """Read one positive timeout that is safe for ``Condition.wait``."""

    value = _positive_int_env(name, default)
    if value > threading.TIMEOUT_MAX:
        raise RuntimeError(f"{name} must not exceed threading.TIMEOUT_MAX")
    return value


# These checks run before FastAPI/Pydantic buffers or parses the JSON request.
# Upload admission is intentionally independent from the paid scheduler: many
# bounded bodies may be parsed while exactly one verifier process is running.
VERIFY_MAX_REQUEST_BYTES = _positive_int_env("DANUS_VERIFY_MAX_REQUEST_BYTES", 1_000_000)
VERIFY_BODY_TIMEOUT_SECONDS = _positive_int_env("DANUS_VERIFY_BODY_TIMEOUT_SECONDS", 10)
VERIFY_MAX_BODY_UPLOADS = _positive_int_env("DANUS_VERIFY_MAX_BODY_UPLOADS", 32)
_ADMISSION_SLOTS = threading.BoundedSemaphore(VERIFY_MAX_BODY_UPLOADS)

_raw_instance_nonce = os.getenv("DANUS_VERIFY_INSTANCE_NONCE")
if _raw_instance_nonce is None:
    VERIFY_INSTANCE_NONCE = "standalone"
elif re.fullmatch(r"[0-9a-f]{32}", _raw_instance_nonce):
    VERIFY_INSTANCE_NONCE = _raw_instance_nonce
else:
    # Fail import/startup rather than exposing an ambiguous health identity.
    raise RuntimeError("DANUS_VERIFY_INSTANCE_NONCE must be 128-bit lowercase hex")


def _scheduler_limits_from_env() -> SchedulerLimits:
    return SchedulerLimits(
        max_distinct_queue=_positive_int_env("DANUS_VERIFY_QUEUE_LIMIT", 4),
        queue_wait_seconds=_condition_wait_int_env(
            "DANUS_VERIFY_QUEUE_WAIT_SECONDS", 1800
        ),
        max_waiters_per_key=_positive_int_env(
            "DANUS_VERIFY_MAX_WAITERS_PER_KEY", 8
        ),
        max_total_waiters=_positive_int_env("DANUS_VERIFY_MAX_WAITERS", 32),
        cache_max_entries=_positive_int_env("DANUS_VERIFY_CACHE_MAX_ENTRIES", 64),
        cache_max_bytes=_positive_int_env(
            "DANUS_VERIFY_CACHE_MAX_BYTES", 16 * 1024 * 1024
        ),
        cache_ttl_seconds=_positive_int_env(
            "DANUS_VERIFY_CACHE_TTL_SECONDS", 3600
        ),
    )


_SCHEDULER = VerificationScheduler(
    instance_nonce=VERIFY_INSTANCE_NONCE,
    limits=_scheduler_limits_from_env(),
)

SCHEDULER_SOURCE_HEADER = "X-Danus-Verify-Scheduler"
SCHEDULER_KEY_HEADER = "X-Danus-Verify-Key"
SCHEDULER_WAIT_HEADER = "X-Danus-Verify-Wait-Ms"
SCHEDULER_REJECTION_HEADER = "X-Danus-Verify-Rejection"


def _preflight_gateway_or_500() -> None:
    """Reject before allocating a verifier run when its MCP runtime is broken."""
    try:
        require_gateway_runtime()
    except GatewayRuntimeUnavailable as exc:
        raise HTTPException(
            status_code=500,
            detail=f"gateway runtime preflight failed: {exc}",
        ) from exc


def _require_exact_keys(value: Dict[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unknown:
            details.append("unknown: " + ", ".join(unknown))
        raise ValueError(f"{path} has invalid fields ({'; '.join(details)})")


def _validate_fact_context(
    context: Dict[str, Any],
    *,
    statement: str,
    proof: str,
    glossary_introduces: Dict[str, str],
) -> None:
    """Fail closed on the full statement-closure adaptive envelope."""
    _require_exact_keys(
        context,
        {
            "schema_version",
            "scope",
            "facts",
            "expanded_proofs",
            "glossary",
            "digest",
            "complete",
            "truncated",
            "missing_fact_ids",
            "revoked_fact_ids",
            "omitted_fact_ids",
            "omitted_glossary_terms",
            "omitted_expanded_proof_ids",
            "characters_used",
            "character_budget",
            "expanded_proof_characters",
            "expanded_proof_character_budget",
        },
        "fact_context",
    )
    if context.get("schema_version") != VERIFICATION_CONTEXT_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {VERIFICATION_CONTEXT_SCHEMA_VERSION}"
        )
    if context.get("complete") is not True:
        raise ValueError("complete must be exactly true")
    if context.get("truncated") is not False:
        raise ValueError("truncated must be exactly false")
    for field in _UNAVAILABLE_CONTEXT_FIELDS:
        value = context.get(field)
        if not isinstance(value, list) or value:
            raise ValueError(f"{field} must be an empty list")

    scope = context.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("scope must be an object")
    _require_exact_keys(
        scope,
        {
            "candidate_fact_id",
            "requested_fact_ids",
            "predecessor_depth",
            "proof_mode",
            "include_project_glossary",
            "projection",
            "expansion_round",
            "closure_fact_ids",
            "expanded_proof_ids",
            "glossary_terms",
        },
        "scope",
    )
    candidate_fact_id = scope.get("candidate_fact_id")
    if not isinstance(candidate_fact_id, str) or not _FACT_ID_RE.fullmatch(
        candidate_fact_id
    ):
        raise ValueError("scope.candidate_fact_id must be a 16-hex fact_id")
    requested = scope.get("requested_fact_ids")
    if not isinstance(requested, list) or any(
        not isinstance(fid, str) or not _FACT_ID_RE.fullmatch(fid) for fid in requested
    ):
        raise ValueError("scope.requested_fact_ids must be a list of 16-hex fact_ids")
    if len(requested) != len(set(requested)):
        raise ValueError("scope.requested_fact_ids contains duplicates")
    if scope.get("predecessor_depth", object()) is not None:
        raise ValueError("verification context must contain the full predecessor closure")
    if scope.get("proof_mode") != "adaptive":
        raise ValueError("verification context proof_mode must be adaptive")
    if scope.get("include_project_glossary") is not False:
        raise ValueError("verification context include_project_glossary must be exactly false")
    if scope.get("projection") != VERIFICATION_CONTEXT_PROJECTION:
        raise ValueError(
            f"verification context projection must be {VERIFICATION_CONTEXT_PROJECTION}"
        )
    expansion_round = scope.get("expansion_round")
    if (
        isinstance(expansion_round, bool)
        or not isinstance(expansion_round, int)
        or expansion_round < 0
    ):
        raise ValueError("scope.expansion_round must be a non-negative integer")
    expanded_proof_ids = scope.get("expanded_proof_ids")
    if not isinstance(expanded_proof_ids, list) or any(
        not isinstance(fid, str) or not _FACT_ID_RE.fullmatch(fid)
        for fid in expanded_proof_ids
    ):
        raise ValueError(
            "scope.expanded_proof_ids must be a list of 16-hex fact_ids"
        )
    if len(expanded_proof_ids) != len(set(expanded_proof_ids)):
        raise ValueError("scope.expanded_proof_ids contains duplicates")
    if expansion_round == 0 and expanded_proof_ids:
        raise ValueError("round zero may not contain expanded proofs")
    if expansion_round > 0 and not expanded_proof_ids:
        raise ValueError("an expansion round requires an expanded proof")
    closure_fact_ids = scope.get("closure_fact_ids")
    if not isinstance(closure_fact_ids, list) or any(
        not isinstance(fid, str) or not _FACT_ID_RE.fullmatch(fid)
        for fid in closure_fact_ids
    ):
        raise ValueError("scope.closure_fact_ids must be a list of 16-hex fact_ids")
    if len(closure_fact_ids) != len(set(closure_fact_ids)):
        raise ValueError("scope.closure_fact_ids contains duplicates")
    if candidate_fact_id in closure_fact_ids:
        raise ValueError("scope.candidate_fact_id must not be an ancestor")
    glossary_terms = scope.get("glossary_terms")
    if not isinstance(glossary_terms, list) or any(
        not isinstance(term, str) or not term for term in glossary_terms
    ):
        raise ValueError("scope.glossary_terms must be a list of non-empty strings")
    if glossary_terms != sorted(set(glossary_terms)):
        raise ValueError("scope.glossary_terms must be sorted and unique")

    facts = context.get("facts")
    if not isinstance(facts, list):
        raise ValueError("facts must be a list")
    records_by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for record in facts:
        if not isinstance(record, dict):
            raise ValueError("each fact record must be an object")
        _require_exact_keys(
            record,
            {"fact_id", "statement", "predecessors", "glossary_introduces"},
            "each fact statement card",
        )
        fact_id = record.get("fact_id")
        if not isinstance(fact_id, str) or not _FACT_ID_RE.fullmatch(fact_id):
            raise ValueError("each fact record needs a 16-hex fact_id")
        if fact_id in records_by_id:
            raise ValueError(f"duplicate fact record: {fact_id}")
        if not isinstance(record.get("statement"), str) or not record["statement"].strip():
            raise ValueError(f"fact {fact_id} needs a non-empty statement")
        predecessors = record.get("predecessors")
        if not isinstance(predecessors, list) or any(
            not isinstance(pid, str) or not _FACT_ID_RE.fullmatch(pid)
            for pid in predecessors
        ):
            raise ValueError(f"fact {fact_id} predecessors must be 16-hex fact_ids")
        if len(predecessors) != len(set(predecessors)):
            raise ValueError(f"fact {fact_id} has duplicate predecessor ids")
        glossary = record.get("glossary_introduces")
        if not isinstance(glossary, dict) or any(
            not isinstance(symbol, str) or not isinstance(definition, str)
            for symbol, definition in glossary.items()
        ):
            raise ValueError(f"fact {fact_id} glossary_introduces must be string mappings")
        records_by_id[fact_id] = record
        ordered_ids.append(fact_id)

    if ordered_ids != closure_fact_ids:
        raise ValueError("fact statement cards must exactly match closure_fact_ids")
    if ordered_ids[: len(requested)] != requested:
        raise ValueError("closure must begin with requested ids in caller order")

    expanded_proofs = context.get("expanded_proofs")
    if not isinstance(expanded_proofs, list):
        raise ValueError("expanded_proofs must be a list")
    proof_ids: list[str] = []
    proofs_by_id: Dict[str, str] = {}
    for proof_record in expanded_proofs:
        if not isinstance(proof_record, dict):
            raise ValueError("each expanded proof record must be an object")
        _require_exact_keys(
            proof_record, {"fact_id", "proof"}, "each expanded proof record"
        )
        fact_id = proof_record.get("fact_id")
        proof_text = proof_record.get("proof")
        if not isinstance(fact_id, str) or not _FACT_ID_RE.fullmatch(fact_id):
            raise ValueError("each expanded proof record needs a 16-hex fact_id")
        if fact_id in proofs_by_id:
            raise ValueError(f"duplicate expanded proof record: {fact_id}")
        if not isinstance(proof_text, str) or not proof_text:
            raise ValueError(f"expanded proof {fact_id} must be a non-empty string")
        proof_ids.append(fact_id)
        proofs_by_id[fact_id] = proof_text
    if proof_ids != expanded_proof_ids:
        raise ValueError(
            "proof-bearing records must exactly match expanded_proof_ids in order"
        )
    if proof_ids != [fid for fid in closure_fact_ids if fid in set(proof_ids)]:
        raise ValueError("expanded proof ids must follow closure order")
    if any(fid not in records_by_id for fid in expanded_proof_ids):
        raise ValueError("expanded proof ids must belong to the supplied closure")

    reachable: set[str] = set()
    pending = list(requested)
    while pending:
        fact_id = pending.pop()
        if fact_id in reachable:
            continue
        record = records_by_id.get(fact_id)
        if record is None:
            raise ValueError(f"closure is missing dependency fact {fact_id}")
        reachable.add(fact_id)
        for predecessor in record["predecessors"]:
            if predecessor not in reachable:
                pending.append(predecessor)
    if reachable != set(records_by_id):
        raise ValueError("facts must be exactly the reachable dependency closure")

    glossary = context.get("glossary")
    if not isinstance(glossary, dict) or any(
        not isinstance(term, str) or not isinstance(definition, str)
        for term, definition in glossary.items()
    ):
        raise ValueError("glossary must be a string mapping")
    if list(glossary) != glossary_terms:
        raise ValueError("glossary keys must exactly match scope.glossary_terms in order")

    closure_terms = {
        str(term)
        for record in facts
        for term in record["glossary_introduces"]
    }
    candidate_terms = set(glossary_introduces)
    shadowed = set(glossary) & (closure_terms | candidate_terms)
    if shadowed:
        raise ValueError(
            "selected inherited definitions shadow higher-precedence terms: "
            + ", ".join(sorted(shadowed))
        )

    characters_used = context.get("characters_used")
    if isinstance(characters_used, bool) or not isinstance(characters_used, int):
        raise ValueError("characters_used must be an integer")
    expected_characters = sum(
        len(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for record in facts
    )
    expected_characters += sum(
        len(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for record in expanded_proofs
    )
    expected_characters += sum(
        len(json.dumps(
            {"term": term, "definition": definition},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
        for term, definition in glossary.items()
    )
    if characters_used != expected_characters:
        raise ValueError("characters_used does not match the complete fact records")
    character_budget = context.get("character_budget")
    if character_budget is not None and (
        isinstance(character_budget, bool)
        or not isinstance(character_budget, int)
        or character_budget < characters_used
    ):
        raise ValueError("character_budget must cover every supplied fact record")

    expanded_proof_characters = context.get("expanded_proof_characters")
    if (
        isinstance(expanded_proof_characters, bool)
        or not isinstance(expanded_proof_characters, int)
        or expanded_proof_characters < 0
    ):
        raise ValueError("expanded_proof_characters must be a non-negative integer")
    expected_proof_characters = sum(
        len(json.dumps(
            {"fact_id": fid, "proof": proofs_by_id[fid]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
        for fid in proof_ids
    )
    if expanded_proof_characters != expected_proof_characters:
        raise ValueError("expanded_proof_characters does not match hydrated proofs")
    expanded_proof_budget = context.get("expanded_proof_character_budget")
    if expanded_proof_budget is not None and (
        isinstance(expanded_proof_budget, bool)
        or not isinstance(expanded_proof_budget, int)
        or expanded_proof_budget < expanded_proof_characters
    ):
        raise ValueError(
            "expanded_proof_character_budget must cover every hydrated proof"
        )

    digest_input = dict(context)
    digest_input.pop("digest", None)
    expected_digest = verification_context_digest(context=digest_input)
    if context.get("digest") != expected_digest:
        raise ValueError("digest does not match scope and fact records")


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_verifier_instance_nonce: str = Field(..., min_length=1)
    expected_output_protocol_version: int = Field(..., strict=True)
    expected_verifier_bundle_digest: str = Field(
        ..., pattern=r"^[0-9a-f]{64}$"
    )
    statement: str = Field(..., min_length=1)
    proof: str = Field(..., min_length=1)
    fact_context: Optional[Dict[str, Any]] = None
    glossary_introduces: Dict[str, str] = Field(default_factory=dict)


app = FastAPI(title="Danus verify service", version="0.1.0")


@app.middleware("http")
async def protect_verification_ingress(request: Request, call_next: Any) -> Any:
    """Bound uploads and cold-start concurrency before request-model parsing."""
    if request.url.path != "/verify" or request.method != "POST":
        return await call_next(request)

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
        if declared_length < 0:
            return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
        if declared_length > VERIFY_MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "verification request body too large"},
            )

    if not _ADMISSION_SLOTS.acquire(blocking=False):
        return JSONResponse(
            status_code=429,
            content={"detail": "verification service is busy"},
        )
    try:
        async def read_limited_body() -> bytes | None:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > VERIFY_MAX_REQUEST_BYTES:
                    return None
            return bytes(body)

        try:
            request_body = await asyncio.wait_for(
                read_limited_body(),
                timeout=VERIFY_BODY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=408,
                content={"detail": "verification request body timed out"},
            )
        if request_body is None:
            return JSONResponse(
                status_code=413,
                content={"detail": "verification request body too large"},
            )

        # Starlette caches request bodies on this attribute. Supplying the exact
        # bounded bytes lets FastAPI/Pydantic parse without touching the socket a
        # second time.
        request._body = request_body
    finally:
        _ADMISSION_SLOTS.release()
    # Body admission ends before endpoint execution.  In particular, ASGI
    # cancellation cannot release the independent paid-job lease below.
    return await call_next(request)


@app.get("/health")
async def health() -> Dict[str, Any]:
    # async on purpose: /health must not queue behind sync /verify threadpool
    # calls, so it responds in ~microseconds regardless of in-flight verifications.
    # `pid` self-identifies this instance: a health probe alone cannot tell OUR
    # verify from another deployment's verify holding the same port on a shared
    # host — callers match this pid against runtime/run/verify.pid to be sure.
    return {
        "status": "ok",
        "pid": os.getpid(),
        "instance_nonce": VERIFY_INSTANCE_NONCE,
        "output_protocol_version": VERIFICATION_OUTPUT_PROTOCOL_VERSION,
        "verifier_bundle_digest": VERIFIER_BUNDLE_DIGEST,
    }


@app.get("/scheduler")
async def scheduler_snapshot() -> Dict[str, Any]:
    """Expose bounded counts only; request keys and proof data stay private."""
    return _SCHEDULER.snapshot()


def _scheduler_key(
    request: VerifyRequest, *, execution_profile: Mapping[str, Any]
) -> str:
    return canonical_sha256(
        {
            "schema": SCHEDULER_KEY_SCHEMA,
            "service_instance_nonce": VERIFY_INSTANCE_NONCE,
            "output_protocol_version": VERIFICATION_OUTPUT_PROTOCOL_VERSION,
            "verifier_bundle_digest": VERIFIER_BUNDLE_DIGEST,
            "execution_profile": dict(execution_profile),
            "request": {
                "statement": request.statement,
                "proof": request.proof,
                "glossary_introduces": request.glossary_introduces,
                "fact_context": request.fact_context,
            },
        }
    )


def _validated_completed_result(
    result: Dict[str, Any],
    *,
    request: VerifyRequest,
    fact_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Independently validate every HTTP-200 value before it is cacheable."""
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="verifier result is not an object")
    authority_fields = {
        "output_schema_version",
        "verification_status",
        "verification_report",
        "verdict",
        "needs_expanded_proofs",
        "repair_hints",
    }
    optional_fields = {"verification_metrics"}
    unknown = set(result) - authority_fields - optional_fields
    missing = authority_fields - set(result)
    if missing or unknown:
        raise HTTPException(
            status_code=500,
            detail="verifier result has an invalid service envelope",
        )
    authority_payload = {field: result[field] for field in authority_fields}
    try:
        validated = validate_verification_output(
            authority_payload,
            statement=request.statement,
            proof=request.proof,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"verifier result failed independent service validation: {exc}",
        ) from exc
    completed: Dict[str, Any] = dict(validated)
    if "verification_metrics" in result:
        metrics = result["verification_metrics"]
        if not isinstance(metrics, dict):
            raise HTTPException(
                status_code=500,
                detail="verification_metrics must be an object",
            )
        completed["verification_metrics"] = metrics
    if fact_context is not None:
        completed["verification_context_digest"] = fact_context["digest"]
    return completed


def _scheduler_headers(
    *, source: str, key: str, wait_ms: int, rejection: Optional[str] = None
) -> Dict[str, str]:
    headers = {
        SCHEDULER_SOURCE_HEADER: source,
        SCHEDULER_KEY_HEADER: key,
        SCHEDULER_WAIT_HEADER: str(max(0, wait_ms)),
    }
    if rejection is not None:
        headers[SCHEDULER_REJECTION_HEADER] = rejection
    return headers


def _raise_scheduled_failure(failure: SchedulerWorkFailed) -> None:
    headers = _scheduler_headers(
        source=failure.source,
        key=failure.key,
        wait_ms=failure.wait_ms,
    )
    cause = failure.cause
    if isinstance(cause, SchedulerRejected):
        headers[SCHEDULER_REJECTION_HEADER] = cause.reason
        raise HTTPException(
            status_code=429,
            detail=cause.detail,
            headers=headers,
        ) from cause
    if isinstance(cause, HTTPException):
        merged = dict(cause.headers or {})
        merged.update(headers)
        raise HTTPException(
            status_code=cause.status_code,
            detail=cause.detail,
            headers=merged,
        ) from cause
    if isinstance(cause, Exception):
        raise HTTPException(
            status_code=500,
            detail="verification scheduler leader failed",
            headers=headers,
        ) from cause
    raise cause


def _run_scheduled_verification(
    request: VerifyRequest,
    *,
    fact_context: Optional[Dict[str, Any]],
    execution_profile: VerificationExecutionProfile,
) -> SchedulerReceipt:
    key = _scheduler_key(
        request,
        execution_profile=execution_profile.canonical(),
    )

    def paid_leader() -> Dict[str, Any]:
        # Only the FIFO leader reaches preflight/allocation/launch.  Its sync
        # worker remains alive after ASGI cancellation until the owned verifier
        # process group is terminal and this function returns or raises.
        _preflight_gateway_or_500()
        run_id = _allocate_run_id(request.statement)
        kwargs: Dict[str, Any] = {
            "run_id": run_id,
            "statement": request.statement,
            "proof": request.proof,
            "execution_profile": execution_profile,
        }
        if fact_context is not None:
            kwargs["fact_context"] = fact_context
        if request.glossary_introduces:
            kwargs["glossary_introduces"] = request.glossary_introduces
        result = run_codex_verification(**kwargs)
        return _validated_completed_result(
            result,
            request=request,
            fact_context=fact_context,
        )

    try:
        return _SCHEDULER.execute(key, paid_leader)
    except SchedulerRejected as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.detail,
            headers=_scheduler_headers(
                source="rejected",
                key=key,
                wait_ms=0,
                rejection=exc.reason,
            ),
        ) from exc
    except SchedulerWorkFailed as failure:
        _raise_scheduled_failure(failure)
        raise AssertionError("scheduled failure did not raise")


@app.post("/verify")
def verify(request: VerifyRequest, response: Response) -> Dict[str, Any]:
    # This is the first endpoint action: a stale caller is rejected before
    # prechecks, result-directory allocation, gateway preflight, or Codex.  The
    # digest echoed from /health also closes a service-restart race between the
    # gateway's health probe and this POST.
    if request.expected_verifier_instance_nonce != VERIFY_INSTANCE_NONCE:
        raise HTTPException(
            status_code=409,
            detail="verifier instance changed after caller health preflight",
        )
    if (
        request.expected_output_protocol_version
        != VERIFICATION_OUTPUT_PROTOCOL_VERSION
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "verifier output protocol mismatch: service requires "
                f"{VERIFICATION_OUTPUT_PROTOCOL_VERSION}"
            ),
        )
    if request.expected_verifier_bundle_digest != VERIFIER_BUNDLE_DIGEST:
        raise HTTPException(
            status_code=409,
            detail="verifier bundle changed after caller health preflight",
        )
    rejected = run_prechecks(request.statement, request.proof)
    if rejected is not None:
        status_code, detail = rejected
        raise HTTPException(status_code=status_code, detail=detail)
    if request.fact_context is None:
        cited = set(_CITED_FACT_ID_RE.findall(request.statement))
        cited.update(_CITED_FACT_ID_RE.findall(request.proof))
        if cited:
            raise HTTPException(
                status_code=400,
                detail=(
                    "fact_context is required when a statement or proof cites an "
                    "internal fact_id"
                ),
            )
        execution_profile = capture_execution_profile()
        receipt = _run_scheduled_verification(
            request,
            fact_context=None,
            execution_profile=execution_profile,
        )
        for name, value in _scheduler_headers(
            source=receipt.source,
            key=receipt.key,
            wait_ms=receipt.wait_ms,
        ).items():
            response.headers[name] = value
        return receipt.value
    try:
        _validate_fact_context(
            request.fact_context,
            statement=request.statement,
            proof=request.proof,
            glossary_introduces=request.glossary_introduces,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid fact_context: {exc}") from exc
    requested = set(request.fact_context["scope"]["requested_fact_ids"])
    cited = set(_CITED_FACT_ID_RE.findall(request.statement))
    cited.update(_CITED_FACT_ID_RE.findall(request.proof))
    undeclared = cited - requested
    uncited = requested - cited
    if undeclared or uncited:
        details = []
        if undeclared:
            details.append("undeclared: " + ", ".join(sorted(undeclared)))
        if uncited:
            details.append("declared but not cited: " + ", ".join(sorted(uncited)))
        raise HTTPException(
            status_code=400,
            detail=(
                "statement/proof fact_id citations must exactly match declared "
                "predecessors ("
            )
            + "; ".join(details)
            + ")",
        )
    execution_profile = capture_execution_profile()
    receipt = _run_scheduled_verification(
        request,
        fact_context=request.fact_context,
        execution_profile=execution_profile,
    )
    for name, value in _scheduler_headers(
        source=receipt.source,
        key=receipt.key,
        wait_ms=receipt.wait_ms,
    ).items():
        response.headers[name] = value
    return receipt.value
