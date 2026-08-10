"""danus.core — the truth layer.

Three tiered stores + their shared schema, ranking, and glossary:

- ``FactGraph``   — the project-shared, content-addressed DAG of verifier-accepted
                    facts. The ONLY correctness source.
- ``GlobalMemory``— project-shared, strongly-typed findings (awareness, never truth).
- ``LocalMemory`` — per-worker private scratch log.

Pure data-structure I/O only; *when* to publish / verify / promote is prose in
the agent prompts, not code here. See the repo ARCHITECTURE.md.
"""

from __future__ import annotations

from . import bm25, glossary
from .factgraph import (
    FACT_CONTEXT_SCHEMA_VERSION,
    VERIFICATION_CONTEXT_PROJECTION,
    VERIFICATION_CONTEXT_SCHEMA_VERSION,
    FactGraph,
    FactPromotionOutcomeUnknown,
    dependency_closure_digest,
    fact_identity_from_verification_context,
    fact_context_digest,
    parse_frontmatter,
    serialize_fact,
    select_referenced_definitions,
    statement_of,
    verification_context_digest,
)
from .global_memory import GlobalMemory, canonical_global_memory_record
from .local_memory import DEFAULT_CHANNELS, LocalMemory
from .schema import (
    EXTERNAL_REF_KEYS,
    GLOBAL_KINDS,
    STATUSES,
    Fact,
    clean_external_refs,
    compute_fact_id,
)
from .verification import (
    VERIFICATION_OUTPUT_PROTOCOL_VERSION,
    validate_verification_output,
)

__all__ = [
    "FactGraph",
    "FactPromotionOutcomeUnknown",
    "FACT_CONTEXT_SCHEMA_VERSION",
    "VERIFICATION_CONTEXT_SCHEMA_VERSION",
    "VERIFICATION_CONTEXT_PROJECTION",
    "fact_context_digest",
    "fact_identity_from_verification_context",
    "verification_context_digest",
    "dependency_closure_digest",
    "select_referenced_definitions",
    "GlobalMemory",
    "canonical_global_memory_record",
    "LocalMemory",
    "DEFAULT_CHANNELS",
    "Fact",
    "GLOBAL_KINDS",
    "STATUSES",
    "EXTERNAL_REF_KEYS",
    "clean_external_refs",
    "compute_fact_id",
    "serialize_fact",
    "parse_frontmatter",
    "statement_of",
    "validate_verification_output",
    "VERIFICATION_OUTPUT_PROTOCOL_VERSION",
    "bm25",
    "glossary",
]
