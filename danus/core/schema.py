"""Schemas + content-addressed fact id for the three core data structures.

Kept minimal on purpose: code only models the *fixed* parts of the data
structures (the fact node and the fact id). Behavior — when to publish, verify,
promote — is prose (prompts/skills), not code.

The glossary (``glossary_introduces``) is kept: it is what makes a fact readable
and composable (every symbol has a definition somewhere). See ``DATA_MODEL.md``
§3 and ``glossary.py``. ``compute_fact_id`` is the Danus scheme, including the
glossary term.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# global memory kinds (the strong categorization) + statuses                  #
# --------------------------------------------------------------------------- #

# kind -> default `verifiable` (objectively checkable vs. a judgment).
GLOBAL_KINDS: Dict[str, bool] = {
    "conclusion": True,
    "example": True,
    "counterexample": True,
    "proof_attempt": True,
    "plan": False,
    "dead_end": False,
    "direction": False,
    "obstacle": False,
    "master_guidance": False,  # main agent, via GPT-5.5-pro (DATA_MODEL.md §2.3)
    "verification": False,     # trace of a fact_submit verification outcome (logged by fact_submit)
    "elaboration": False,      # main agent's periodic high-signal progress synthesis (DATA_MODEL.md §2.4)
    "advisor_checkpoint": False,  # bounded late-intervention summary awaiting owner authorization
}

ADVISOR_CHECKPOINT_MAX_BYTES = 16 * 1024
ADVISOR_CHECKPOINT_MAX_FACT_IDS = 12
ADVISOR_CHECKPOINT_HEADINGS = (
    "## Verified facts",
    "## Failed routes and evidence",
    "## Unresolved bottleneck",
    "## Candidate decision question",
)
_FACT_ID_RE = re.compile(r"[0-9a-f]{16}")


def validate_advisor_checkpoint(
    claim: object,
    evidence: object,
    links: object,
) -> None:
    """Validate the bounded late-intervention handoff stored in global memory."""

    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("advisor_checkpoint claim must be non-empty text")
    if len(claim.encode("utf-8")) > 512:
        raise ValueError("advisor_checkpoint claim exceeds 512 UTF-8 bytes")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("advisor_checkpoint evidence must be non-empty text")
    if len(evidence.encode("utf-8")) > ADVISOR_CHECKPOINT_MAX_BYTES:
        raise ValueError(
            f"advisor_checkpoint exceeds {ADVISOR_CHECKPOINT_MAX_BYTES} UTF-8 bytes"
        )
    offsets: list[int] = []
    for heading in ADVISOR_CHECKPOINT_HEADINGS:
        if evidence.count(heading) != 1:
            raise ValueError(
                "advisor_checkpoint must contain each canonical section exactly once"
            )
        offsets.append(evidence.index(heading))
    if offsets != sorted(offsets):
        raise ValueError("advisor_checkpoint sections are out of canonical order")
    for index, heading in enumerate(ADVISOR_CHECKPOINT_HEADINGS):
        start = offsets[index] + len(heading)
        end = offsets[index + 1] if index + 1 < len(offsets) else len(evidence)
        if not evidence[start:end].strip():
            raise ValueError(f"advisor_checkpoint section {heading!r} is empty")
    if not isinstance(links, dict):
        raise ValueError("advisor_checkpoint links must be an object")
    unknown_links = set(links) - {"fact_ids", "recommendation_id"}
    if unknown_links:
        raise ValueError(
            "advisor_checkpoint contains unsupported protected links: "
            f"{sorted(unknown_links)}"
        )
    fact_ids = links.get("fact_ids")
    if not isinstance(fact_ids, list):
        raise ValueError("advisor_checkpoint links.fact_ids must be a list")
    if len(fact_ids) > ADVISOR_CHECKPOINT_MAX_FACT_IDS:
        raise ValueError(
            "advisor_checkpoint contains too many verified fact ids "
            f"(maximum {ADVISOR_CHECKPOINT_MAX_FACT_IDS})"
        )
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("advisor_checkpoint links.fact_ids must not contain duplicates")
    if any(not isinstance(item, str) or _FACT_ID_RE.fullmatch(item) is None for item in fact_ids):
        raise ValueError("advisor_checkpoint links.fact_ids contains an invalid fact id")

# A global-memory entry's lifecycle. Set/advanced by the agent; the store just
# records it (no enforcement machinery).
STATUSES = (
    "unverified", "verifying", "verified", "refuted",  # verifiable entries
    "open", "supported", "challenged",                  # judgment entries
)


# Fixed provenance shape for a strategy consult recorded as master_guidance.
# Browser reports do not receive this shape until an explicit review/adoption
# transition has produced strategy-only text.
CONSULT_PROVENANCE_FIELDS = {
    "schema_version",
    "transport",
    "request_id",
    "elaboration_id",
    "context_id",
    "recommendation_id",
    "binding_sha256",
    "receipt_sha256",
    "prompt_sha256",
    "reply_sha256",
    "adopted_strategy_sha256",
    "trust",
    "billing_basis",
    "model",
    "ui_mode",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "checkpoint_id",
    "checkpoint_sha256",
    "checkpoint_bytes",
}
CONSULT_CHECKPOINT_FIELDS = {
    "checkpoint_id",
    "checkpoint_sha256",
    "checkpoint_bytes",
}
CONSULT_CHECKPOINT_MAX_BYTES = 32 * 1024
CONSULT_TRANSPORTS = {
    "gpt_pro",
    "claude_api",
    "claude_code",
    "chatgpt_pro_browser",
    "off",
}
CONSULT_BILLING_BASES = {
    "metered_api",
    "subscription",
    "subscription_estimate",
    "disabled",
}
_FULL_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def clean_consult_provenance(value: object) -> Dict[str, Any]:
    """Validate the bounded, non-secret consult receipt projection."""

    if not isinstance(value, dict):
        raise ValueError("consult_provenance must be an object")
    unknown = set(value) - CONSULT_PROVENANCE_FIELDS
    if unknown:
        raise ValueError(f"unknown consult_provenance fields: {sorted(unknown)}")
    required = {
        "schema_version",
        "transport",
        "trust",
        "billing_basis",
        "model",
        "ui_mode",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    }
    missing = required - set(value)
    if missing:
        raise ValueError(f"missing consult_provenance fields: {sorted(missing)}")
    schema_version = value["schema_version"]
    if schema_version not in (1, 2):
        raise ValueError("consult_provenance.schema_version must be 1 or 2")
    transport = value["transport"]
    if transport not in CONSULT_TRANSPORTS:
        raise ValueError("invalid consult_provenance transport")
    if value["billing_basis"] not in CONSULT_BILLING_BASES:
        raise ValueError("invalid consult_provenance billing_basis")
    checkpoint_fields_present = CONSULT_CHECKPOINT_FIELDS & set(value)
    if schema_version == 1 and checkpoint_fields_present:
        raise ValueError("schema 1 consult_provenance cannot include checkpoint fields")
    if schema_version == 2:
        if transport != "chatgpt_pro_browser":
            raise ValueError(
                "schema 2 consult_provenance requires chatgpt_pro_browser transport"
            )
        missing_checkpoint_fields = CONSULT_CHECKPOINT_FIELDS - set(value)
        if missing_checkpoint_fields:
            raise ValueError(
                "missing schema 2 consult_provenance fields: "
                f"{sorted(missing_checkpoint_fields)}"
            )

    def optional_text(name: str, *, max_bytes: int = 512) -> str | None:
        item = value.get(name)
        if item is None:
            return None
        if not isinstance(item, str) or not item:
            raise ValueError(f"consult_provenance.{name} must be non-empty text or null")
        try:
            encoded = item.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"consult_provenance.{name} is not valid UTF-8") from exc
        if len(encoded) > max_bytes:
            raise ValueError(f"consult_provenance.{name} exceeds hard limit")
        return item

    def optional_hash(name: str) -> str | None:
        item = value.get(name)
        if item is None:
            return None
        if not isinstance(item, str) or _FULL_SHA256_RE.fullmatch(item) is None:
            raise ValueError(f"consult_provenance.{name} must be a SHA-256 digest")
        return item

    def optional_count(name: str) -> int | None:
        item = value[name]
        if item is None:
            return None
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(
                f"consult_provenance.{name} must be a non-negative integer or null"
            )
        return item

    request_id = optional_text("request_id")
    elaboration_id = optional_text("elaboration_id")
    context_id = optional_text("context_id")
    recommendation_id = optional_text("recommendation_id")
    binding_sha256 = optional_hash("binding_sha256")
    receipt_sha256 = optional_hash("receipt_sha256")
    model = optional_text("model")
    ui_mode = optional_text("ui_mode")
    trust = optional_text("trust")
    prompt_sha256 = optional_hash("prompt_sha256")
    reply_sha256 = optional_hash("reply_sha256")
    adopted_strategy_sha256 = optional_hash("adopted_strategy_sha256")
    input_tokens = optional_count("input_tokens")
    output_tokens = optional_count("output_tokens")
    checkpoint_id: str | None = None
    checkpoint_sha256: str | None = None
    checkpoint_bytes: int | None = None
    if schema_version == 2:
        raw_checkpoint_id = value["checkpoint_id"]
        if (
            not isinstance(raw_checkpoint_id, str)
            or _FACT_ID_RE.fullmatch(raw_checkpoint_id) is None
        ):
            raise ValueError(
                "consult_provenance.checkpoint_id must be 16 lowercase hex characters"
            )
        checkpoint_id = raw_checkpoint_id
        raw_checkpoint_sha256 = value["checkpoint_sha256"]
        if (
            not isinstance(raw_checkpoint_sha256, str)
            or _FULL_SHA256_RE.fullmatch(raw_checkpoint_sha256) is None
        ):
            raise ValueError(
                "consult_provenance.checkpoint_sha256 must be a SHA-256 digest"
            )
        checkpoint_sha256 = raw_checkpoint_sha256
        raw_checkpoint_bytes = value["checkpoint_bytes"]
        if (
            isinstance(raw_checkpoint_bytes, bool)
            or not isinstance(raw_checkpoint_bytes, int)
            or raw_checkpoint_bytes <= 0
            or raw_checkpoint_bytes > CONSULT_CHECKPOINT_MAX_BYTES
        ):
            raise ValueError(
                "consult_provenance.checkpoint_bytes must be a positive integer "
                f"no greater than {CONSULT_CHECKPOINT_MAX_BYTES}"
            )
        checkpoint_bytes = raw_checkpoint_bytes
    cost = value["cost_usd"]
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or float(cost) < 0
    ):
        raise ValueError("consult_provenance.cost_usd must be non-negative or null")
    if transport == "chatgpt_pro_browser":
        if "recommendation_id" not in value:
            raise ValueError(
                "browser provenance must include its recommendation binding"
            )
        if (
            trust != "adopted_strategy"
            or value["billing_basis"] != "subscription"
            or model is not None
            or ui_mode != "Pro"
            or input_tokens is not None
            or output_tokens is not None
            or cost is not None
        ):
            raise ValueError(
                "browser provenance requires adopted strategy, Pro UI, subscription "
                "billing, and null model/token/cost telemetry"
            )
        if not all(
            (
                request_id,
                context_id,
                binding_sha256,
                receipt_sha256,
                prompt_sha256,
                reply_sha256,
                adopted_strategy_sha256,
            )
        ):
            raise ValueError("browser provenance is missing its durable adoption receipt")
    elif recommendation_id is not None:
        raise ValueError(
            "non-browser consult provenance cannot claim a coordinator recommendation"
        )

    cleaned: Dict[str, Any] = {
        "schema_version": schema_version,
        "transport": transport,
        "request_id": request_id,
        "elaboration_id": elaboration_id,
        "context_id": context_id,
        "recommendation_id": recommendation_id,
        "binding_sha256": binding_sha256,
        "receipt_sha256": receipt_sha256,
        "prompt_sha256": prompt_sha256,
        "reply_sha256": reply_sha256,
        "adopted_strategy_sha256": adopted_strategy_sha256,
        "trust": trust,
        "billing_basis": value["billing_basis"],
        "model": model,
        "ui_mode": ui_mode,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": None if cost is None else float(cost),
    }
    if schema_version == 2:
        cleaned.update(
            {
                "checkpoint_id": checkpoint_id,
                "checkpoint_sha256": checkpoint_sha256,
                "checkpoint_bytes": checkpoint_bytes,
            }
        )
    return cleaned


# --------------------------------------------------------------------------- #
# fact node (the only structured schema we keep)                              #
# --------------------------------------------------------------------------- #

# Canonical key order for a structured external reference (a published result the
# proof cites). Extra keys are preserved but sorted after these. Kept loose on
# purpose — bibliographic data is filled by the worker and corrected by the paper
# pipeline's reference auditor, not policed here.
EXTERNAL_REF_KEYS = ("key", "authors", "title", "arxiv", "year", "venue", "doi", "cited_for")


def clean_external_refs(refs: object) -> List[Dict[str, object]]:
    """Normalize an external-refs payload to a list of plain JSON-safe dicts with a
    stable key order. Drops non-dict entries; never raises (advisory data)."""
    if not refs:
        return []
    out: List[Dict[str, object]] = []
    for r in refs:  # type: ignore[union-attr]
        if not isinstance(r, dict):
            continue
        ordered = {k: r[k] for k in EXTERNAL_REF_KEYS if k in r}
        for k in sorted(r):  # preserve any extra keys, deterministically
            if k not in ordered:
                ordered[k] = r[k]
        out.append(ordered)
    return out


@dataclass
class Fact:
    """A verified fact = one node in the fact graph. Frontmatter (fact_id /
    problem_id / author / predecessors / glossary_introduces / external_refs) +
    the markdown body (statement / proof / optional intuition)."""

    fact_id: str
    problem_id: str
    author: str
    predecessors: List[str]                    # bare-hex fact ids (the DAG)
    statement: str
    proof: str
    glossary_introduces: Dict[str, str] = field(default_factory=dict)  # symbol -> definition
    intuition: str = ""
    # Full, collision-resistant semantic identity.  The public ``fact_id``
    # remains the historical 16-hex content address; FactGraph uses this digest
    # to distinguish an exact active fact from a short-id collision.
    fact_identity: str = ""
    # Structured bibliography of external (published) results the proof cites.
    # Mutable metadata — NOT part of the content-addressed fact_id (see
    # compute_fact_id): the reference auditor corrects these post-hoc, so binding
    # them into the id would break the DAG on every audit. The citation *keys* used
    # in the proof text are already hashed (they live in `proof`).
    external_refs: List[Dict[str, object]] = field(default_factory=list)


def _normalize(text: str) -> str:
    """Whitespace-stable canonical form for content hashing (cosmetic edits do
    not perturb the fact_id)."""
    return re.sub(r"\s+", " ", text or "").strip()


def compute_fact_id(
    *,
    problem_id: str,
    predecessors: List[str],
    glossary_introduces: Dict[str, str],
    statement: str,
    proof: str,
) -> str:
    """Deterministic 16-hex SHA-256 of the canonical content (the Danus scheme).
    Same content -> same id -> natural dedup.

    Note: ``external_refs`` is deliberately excluded — it is mutable bibliographic
    metadata the reference auditor corrects after the fact is verified; hashing it
    would change the id (and break the DAG) on every audit, and would also perturb
    the ids of all pre-existing facts. The cited keys themselves are already in
    ``proof``, which is hashed."""
    body = {
        "problem_id": problem_id,
        "predecessors": sorted(predecessors),
        "glossary_introduces": dict(
            sorted((str(k), str(v)) for k, v in glossary_introduces.items())
        ),
        "statement": _normalize(statement),
        "proof": _normalize(proof),
    }
    canon = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:16]


FACT_IDENTITY_SCHEMA = "danus.fact-identity.v1"


def compute_fact_identity(
    *,
    problem_id: str,
    predecessors: List[str],
    glossary_introduces: Dict[str, str],
    statement: str,
    proof: str,
    context_bindings: Any,
    glossary_bindings: Dict[str, str],
) -> str:
    """Return the canonical full SHA-256 identity of a mathematical fact.

    The historical :func:`compute_fact_id` stays unchanged for path and DAG
    compatibility.  This full identity additionally commits to the exact
    predecessor-context cards and immutable/global glossary definitions under
    which the candidate is interpreted.  Cosmetic whitespace in mathematical
    text and definition values is canonicalized; ordering of predecessor and
    glossary maps is immaterial.  ``context_bindings`` is emitted by FactGraph
    from one authenticated snapshot and is encoded as strict canonical JSON.
    """

    body = {
        "schema": FACT_IDENTITY_SCHEMA,
        "problem_id": problem_id,
        "predecessors": sorted(predecessors),
        "definitions": {
            str(symbol): _normalize(str(definition))
            for symbol, definition in sorted(
                glossary_introduces.items(), key=lambda item: str(item[0])
            )
        },
        "statement": _normalize(statement),
        "proof": _normalize(proof),
        "context_bindings": context_bindings,
        "glossary_bindings": {
            str(symbol): _normalize(str(definition))
            for symbol, definition in sorted(
                glossary_bindings.items(), key=lambda item: str(item[0])
            )
        },
    }
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
