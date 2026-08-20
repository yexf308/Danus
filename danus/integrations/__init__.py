"""danus.integrations — external services the engine grounds proofs against.

The stable ``search`` surface is contamination-gated. Production defaults to
the legacy open arXiv index; evaluation runs can select strict Matlas, a dated
arXiv cutoff, or no retrieval without changing the gateway.
"""

from __future__ import annotations

from .gated_search import RESULT_FIELDS, search

__all__ = ["search", "RESULT_FIELDS"]
