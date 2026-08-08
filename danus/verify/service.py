"""Danus verify service — the mathematical authority behind the write-gate.

    POST /verify {statement, proof, glossary_introduces?, fact_context?}
      -> {verification_report, verdict, repair_hints}
    GET  /health                    -> {status: "ok", pid: <int>}

/verify runs the deterministic pre-checks (``prechecks.run_prechecks``) and, if
they pass, cold-starts a fresh codex verifier (``launcher.run_codex_verification``)
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
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from danus.core import (
    VERIFICATION_CONTEXT_PROJECTION,
    VERIFICATION_CONTEXT_SCHEMA_VERSION,
    verification_context_digest,
)

from .launcher import _allocate_run_id, run_codex_verification
from .prechecks import run_prechecks


_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_CITED_FACT_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")
_UNAVAILABLE_CONTEXT_FIELDS = (
    "missing_fact_ids",
    "revoked_fact_ids",
    "omitted_fact_ids",
    "omitted_glossary_terms",
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


# These checks run before FastAPI/Pydantic buffers or parses the JSON request.
# One admitted request corresponds to at most one expensive cold Codex launch.
VERIFY_MAX_REQUEST_BYTES = _positive_int_env("DANUS_VERIFY_MAX_REQUEST_BYTES", 1_000_000)
VERIFY_BODY_TIMEOUT_SECONDS = _positive_int_env("DANUS_VERIFY_BODY_TIMEOUT_SECONDS", 10)
VERIFY_MAX_CONCURRENT_REQUESTS = _positive_int_env(
    "DANUS_VERIFY_MAX_CONCURRENT_REQUESTS", 1
)
_ADMISSION_SLOTS = threading.BoundedSemaphore(VERIFY_MAX_CONCURRENT_REQUESTS)


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
    """Fail closed on the compact full-closure verification envelope."""
    _require_exact_keys(
        context,
        {
            "schema_version",
            "scope",
            "facts",
            "ancestor_definitions",
            "dependency_closure",
            "glossary",
            "digest",
            "complete",
            "truncated",
            "missing_fact_ids",
            "revoked_fact_ids",
            "omitted_fact_ids",
            "omitted_glossary_terms",
            "characters_used",
            "character_budget",
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
            "requested_fact_ids",
            "predecessor_depth",
            "proof_mode",
            "include_project_glossary",
            "projection",
            "ancestor_definition_terms",
            "glossary_terms",
        },
        "scope",
    )
    requested = scope.get("requested_fact_ids")
    if not isinstance(requested, list) or any(
        not isinstance(fid, str) or not _FACT_ID_RE.fullmatch(fid) for fid in requested
    ):
        raise ValueError("scope.requested_fact_ids must be a list of 16-hex fact_ids")
    if len(requested) != len(set(requested)):
        raise ValueError("scope.requested_fact_ids contains duplicates")
    if scope.get("predecessor_depth", object()) is not None:
        raise ValueError("verification context must contain the full predecessor closure")
    if scope.get("proof_mode") != "none":
        raise ValueError("verification context proof_mode must be none")
    if scope.get("include_project_glossary") is not False:
        raise ValueError("verification context include_project_glossary must be exactly false")
    if scope.get("projection") != VERIFICATION_CONTEXT_PROJECTION:
        raise ValueError(
            f"verification context projection must be {VERIFICATION_CONTEXT_PROJECTION}"
        )
    ancestor_definition_terms = scope.get("ancestor_definition_terms")
    if not isinstance(ancestor_definition_terms, list) or any(
        not isinstance(term, str) or not term for term in ancestor_definition_terms
    ):
        raise ValueError(
            "scope.ancestor_definition_terms must be a list of non-empty strings"
        )
    if ancestor_definition_terms != sorted(set(ancestor_definition_terms)):
        raise ValueError(
            "scope.ancestor_definition_terms must be sorted and unique"
        )
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
    if len(facts) != len(requested):
        raise ValueError("facts must contain exactly one direct card per requested fact_id")
    direct_by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids = []
    for record in facts:
        if not isinstance(record, dict):
            raise ValueError("each fact record must be an object")
        _require_exact_keys(
            record,
            {"fact_id", "statement", "predecessors", "glossary_introduces"},
            "each direct premise card",
        )
        fact_id = record.get("fact_id")
        if not isinstance(fact_id, str) or not _FACT_ID_RE.fullmatch(fact_id):
            raise ValueError("each fact record needs a 16-hex fact_id")
        if fact_id in direct_by_id:
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
        direct_by_id[fact_id] = record
        ordered_ids.append(fact_id)

    if ordered_ids != requested:
        raise ValueError("direct premise cards must match requested ids in caller order")

    dependency_closure = context.get("dependency_closure")
    if not isinstance(dependency_closure, dict):
        raise ValueError("dependency_closure must be an object")
    _require_exact_keys(
        dependency_closure, {"count", "digest"}, "dependency_closure"
    )
    closure_count = dependency_closure.get("count")
    closure_digest = dependency_closure.get("digest")
    if (
        isinstance(closure_count, bool)
        or not isinstance(closure_count, int)
        or closure_count < len(requested)
    ):
        raise ValueError("dependency_closure.count must cover every requested fact")
    if (
        not isinstance(closure_digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", closure_digest)
    ):
        raise ValueError("dependency_closure.digest must be a SHA-256 digest")

    ancestor_definitions = context.get("ancestor_definitions")
    if not isinstance(ancestor_definitions, list):
        raise ValueError("ancestor_definitions must be a list")
    definitions_by_term: Dict[str, str] = {}
    ordered_definition_terms: list[str] = []
    for record in ancestor_definitions:
        if not isinstance(record, dict):
            raise ValueError("each ancestor definition must be an object")
        _require_exact_keys(
            record,
            {"term", "definition", "source_fact_id"},
            "each ancestor definition",
        )
        term = record.get("term")
        definition = record.get("definition")
        source_fact_id = record.get("source_fact_id")
        if not isinstance(term, str) or not term:
            raise ValueError("each ancestor definition needs a non-empty term")
        if not isinstance(definition, str):
            raise ValueError(f"ancestor definition {term} must be a string")
        if term in definitions_by_term:
            raise ValueError(f"duplicate ancestor definition term: {term}")
        if (
            not isinstance(source_fact_id, str)
            or not _FACT_ID_RE.fullmatch(source_fact_id)
            or source_fact_id in direct_by_id
        ):
            raise ValueError(
                f"ancestor definition {term} needs a non-direct 16-hex source"
            )
        definitions_by_term[term] = definition
        ordered_definition_terms.append(term)
    if ordered_definition_terms != ancestor_definition_terms:
        raise ValueError(
            "ancestor definition records must exactly match scope terms in order"
        )

    glossary = context.get("glossary")
    if not isinstance(glossary, dict) or any(
        not isinstance(term, str) or not isinstance(definition, str)
        for term, definition in glossary.items()
    ):
        raise ValueError("glossary must be a string mapping")
    if list(glossary) != glossary_terms:
        raise ValueError("glossary keys must exactly match scope.glossary_terms in order")

    direct_terms = {
        str(term)
        for card in facts
        for term in card["glossary_introduces"]
    }
    candidate_terms = set(glossary_introduces)
    selected_terms = set(definitions_by_term) | set(glossary)
    if set(definitions_by_term) & set(glossary):
        raise ValueError("ancestor and global definition terms must be disjoint")
    shadowed = selected_terms & (direct_terms | candidate_terms)
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
        len(json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
        for record in ancestor_definitions
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

    expected_digest = verification_context_digest(
        scope=scope,
        facts=facts,
        ancestor_definitions=ancestor_definitions,
        dependency_closure=dependency_closure,
        glossary=glossary,
    )
    if context.get("digest") != expected_digest:
        raise ValueError("digest does not match scope and fact records")


class VerifyRequest(BaseModel):
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
        return await call_next(request)
    finally:
        _ADMISSION_SLOTS.release()


@app.get("/health")
async def health() -> Dict[str, Any]:
    # async on purpose: /health must not queue behind sync /verify threadpool
    # calls, so it responds in ~microseconds regardless of in-flight verifications.
    # `pid` self-identifies this instance: a health probe alone cannot tell OUR
    # verify from another deployment's verify holding the same port on a shared
    # host — callers match this pid against runtime/run/verify.pid to be sure.
    return {"status": "ok", "pid": os.getpid()}


@app.post("/verify")
def verify(request: VerifyRequest) -> Dict[str, Any]:
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
        run_id = _allocate_run_id(request.statement)
        # Keep old monkeypatches/callers that implement the original three-arg
        # launcher seam working for self-contained requests.
        if not request.glossary_introduces:
            return run_codex_verification(
                run_id=run_id, statement=request.statement, proof=request.proof
            )
        return run_codex_verification(
            run_id=run_id,
            statement=request.statement,
            proof=request.proof,
            glossary_introduces=request.glossary_introduces,
        )
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
    run_id = _allocate_run_id(request.statement)
    result = run_codex_verification(
        run_id=run_id,
        statement=request.statement,
        proof=request.proof,
        fact_context=request.fact_context,
        glossary_introduces=request.glossary_introduces,
    )
    # Server-side attestation: the gateway requires this exact digest. An older
    # service that silently ignores the new request field cannot accidentally
    # authorize a context-free write during a rolling upgrade.
    return {
        **result,
        "verification_context_digest": request.fact_context["digest"],
    }
