"""danus.strategy — the stateless strategic-consult gateway.

Optionally feeds an elaboration to an explicitly selected strong-model
transport and returns a uniform JSON envelope
(``reply`` + ``cost_usd`` + ``usage`` + ``transport``). It touches no truth
stores — the only file it writes is the per-project spend ledger.

The safe default transport is ``off``. Paid API/CLI transports are explicit
opt-ins; the Chrome-only ChatGPT Pro handoff remains separately owner-gated.

Run as ``python -m danus.strategy`` (bin/consult wraps it).
"""

from __future__ import annotations

from .cli import main
from .config import ConsultConfig, load_config, resolve_transport
from .ledger import ledger_path, log_spend
from .transport import GptProTransport, OffTransport, Transport, shape_envelope, tools_for

__all__ = [
    "main",
    "ConsultConfig",
    "load_config",
    "resolve_transport",
    "ledger_path",
    "log_spend",
    "Transport",
    "GptProTransport",
    "OffTransport",
    "shape_envelope",
    "tools_for",
]
