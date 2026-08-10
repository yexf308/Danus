"""Consult billing/usage ledger.

A project's total cost = the sum of ``cost_usd`` over ``<project>/spend/consult.jsonl``.
The browser subscription transport has no trustworthy per-call token or price
telemetry, so its usage and cost are recorded as JSON null and counted separately
as an unpriced subscription call -- never fabricated as zero.

Writes are append-only, fsync-durable, process-locked, and idempotent by optional
``request_id``.  Malformed historical lines are tolerated when summing.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

LEDGER_RELPATH = ("spend", "consult.jsonl")


def ledger_path(project: str | Path) -> Path:
    return Path(project).joinpath(*LEDGER_RELPATH)


def _optional_count(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer or null")
    return value


def _optional_cost(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("cost_usd must be a non-negative number or null")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("cost_usd must be a finite non-negative number or null")
    return result


def _record(envelope: Dict[str, Any]) -> Dict[str, Any]:
    usage = envelope.get("usage") or {}
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object or null")
    request_id = envelope.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ValueError("request_id must be a non-empty string or null")
    billing_basis = envelope.get("billing_basis")
    if billing_basis is None:
        billing_basis = "metered_api"
    if billing_basis not in {
        "metered_api",
        "subscription",
        "subscription_estimate",
        "disabled",
    }:
        raise ValueError("invalid consult billing_basis")
    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "transport": envelope.get("transport"),
        "model": envelope.get("model"),
        "ui_mode": envelope.get("ui_mode"),
        "effort": envelope.get("effort"),
        "attempt": envelope.get("attempt"),
        "status": envelope.get("status"),
        "input_tokens": _optional_count(usage.get("input"), label="input_tokens"),
        "output_tokens": _optional_count(usage.get("output"), label="output_tokens"),
        "reasoning_tokens": _optional_count(
            usage.get("reasoning"), label="reasoning_tokens"
        ),
        "billing_basis": billing_basis,
        "cost_usd": _optional_cost(envelope.get("cost_usd")),
        "seconds": envelope.get("seconds"),
    }


def _summary(records: list[Dict[str, Any]]) -> Dict[str, Any]:
    total = 0.0
    unpriced_subscription_calls = 0
    for record in records:
        try:
            cost = record.get("cost_usd")
            if cost is not None:
                parsed = float(cost)
                if math.isfinite(parsed) and parsed >= 0:
                    total += parsed
            elif record.get("billing_basis") == "subscription":
                unpriced_subscription_calls += 1
        except (TypeError, ValueError):
            continue
    formatted = f"{total:.4f}"
    return {
        "project_total_usd": formatted,
        "metered_total_usd": formatted,
        "unpriced_subscription_calls": unpriced_subscription_calls,
    }


def log_spend_summary(project: str | Path, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Idempotently append one consult record and return billing summary."""

    ledger = ledger_path(project)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    record = _record(envelope)
    encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
    with ledger.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            records: list[Dict[str, Any]] = []
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
            request_id = record.get("request_id")
            duplicate = request_id is not None and any(
                item.get("request_id") == request_id for item in records
            )
            if not duplicate:
                handle.seek(0, os.SEEK_END)
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                records.append(record)
            return _summary(records)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def log_spend(project: str | Path, envelope: Dict[str, Any]) -> str:
    """Append one spend record for this consult and return the running total
    (formatted to 4 dp) over the whole ledger."""
    return str(log_spend_summary(project, envelope)["project_total_usd"])


def _sum_ledger(ledger: Path) -> float:
    if not ledger.exists():
        return 0.0
    with ledger.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            records = []
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return float(_summary(records)["project_total_usd"])
