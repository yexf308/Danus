"""Lightweight, idempotent redaction for untrusted external diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Optional


_SECRET_KEY_RE = re.compile(
    r"(?i)(?:authorization|password|secret|token|api[_-]?key|"
    r"client[_-]?secret|access[_-]?token|refresh[_-]?token)"
)


def _sensitive_key(key: str) -> bool:
    camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return _SECRET_KEY_RE.search(camel) is not None


def _redact_object(value: object, *, key: Optional[str] = None) -> object:
    if key is not None and _sensitive_key(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_object(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_object(child) for child in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    text = value[:32_768]
    text = re.sub(
        r"(?im)^([ \t]*authorization\s*:\s*)(?!<redacted>)[^\r\n]*",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r'''(?ix)(\b(?:proxy[_-]?authorization|authorization)\s*:\s*)(?!<redacted>)[^,;\r\n"'}]+''',
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=:%-]+",
        "Bearer <redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\bBasic\s+[A-Za-z0-9._~+/=:%-]+",
        "Basic <redacted>",
        text,
    )
    text = re.sub(
        r'''(?ix)(["'][A-Za-z0-9_-]*(?:authorization|password|secret|token|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)["']\s*:\s*["'])([^"']*)(["'])''',
        r"\1<redacted>\3",
        text,
    )
    text = re.sub(
        r'''(?ix)(\b[A-Za-z0-9_-]*(?:authorization|password|secret|token|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b\s*[=:]\s*["'])([^"']*)(["'])''',
        r"\1<redacted>\3",
        text,
    )
    text = re.sub(
        r'''(?ix)(\b[A-Za-z0-9_-]*(?:authorization|password|secret|token|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b\s*[=:]\s*)(?!["']|<redacted>)([^\s,;&]+)''',
        r"\1<redacted>",
        text,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-<redacted>", text)


def redact_external_error(value: object, *, max_bytes: int = 4096) -> str:
    """Idempotently redact and bound provider/RPC/error material."""
    raw = str(value)
    raw_bytes = raw.encode("utf-8", errors="replace")
    if len(raw_bytes) > 32_768:
        return (
            f"<external error omitted bytes={len(raw_bytes)} "
            f"sha256={hashlib.sha256(raw_bytes).hexdigest()}>"
        )
    if isinstance(value, (Mapping, list, tuple)):
        try:
            safe = json.dumps(
                _redact_object(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            safe = _redact_text(raw)
    else:
        safe = _redact_text(raw)
    # JSON accepts escaped lone surrogates, but durable UTF-8 projections do
    # not. Normalize untrusted display text before any caller bounds/encodes it.
    safe = safe.encode("utf-8", errors="replace").decode("utf-8")
    encoded = safe.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        return (
            f"<external error omitted bytes={len(raw_bytes)} "
            f"sha256={hashlib.sha256(raw_bytes).hexdigest()}>"
        )
    return safe


__all__ = ["redact_external_error"]
