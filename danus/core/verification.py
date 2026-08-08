"""Deterministic validation for the verifier's final JSON contract."""

from __future__ import annotations

from typing import Any, Dict


def _require_exact_keys(value: Dict[str, Any], expected: set[str], path: str) -> None:
    """Reject missing and unknown fields at a contract boundary."""
    actual = set(value)
    if actual == expected:
        return
    problems = []
    missing = expected - actual
    unknown = actual - expected
    if missing:
        problems.append("missing: " + ", ".join(sorted(map(str, missing))))
    if unknown:
        problems.append("unknown: " + ", ".join(sorted(map(str, unknown))))
    raise ValueError(f"{path} must have exactly the required keys ({'; '.join(problems)})")


def validate_verification_output(payload: Any) -> Dict[str, Any]:
    """Return ``payload`` iff it satisfies the strict verdict contract.

    Mathematical judgment remains the verifier's job. This validator prevents a
    malformed or self-contradictory agent response (for example ``correct`` plus
    a non-empty gap list) from crossing the write gate.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    _require_exact_keys(
        payload,
        {"verification_report", "verdict", "repair_hints"},
        "payload",
    )

    report = payload.get("verification_report")
    if not isinstance(report, dict):
        raise ValueError("verification_report must be a dict")
    _require_exact_keys(
        report,
        {"summary", "critical_errors", "gaps"},
        "verification_report",
    )
    if not isinstance(report.get("summary"), str):
        raise ValueError("verification_report.summary must be a string")

    for key in ("critical_errors", "gaps"):
        findings = report.get(key)
        if not isinstance(findings, list):
            raise ValueError(f"verification_report.{key} must be a list")
        for finding in findings:
            if not isinstance(finding, dict):
                raise ValueError(f"each {key} entry must be a dict")
            _require_exact_keys(finding, {"location", "issue"}, f"each {key} entry")
            if not isinstance(finding.get("location"), str) or not finding["location"]:
                raise ValueError(f"each {key} entry needs a non-empty string location")
            if not isinstance(finding.get("issue"), str) or not finding["issue"]:
                raise ValueError(f"each {key} entry needs a non-empty string issue")

    verdict = payload.get("verdict")
    if verdict not in ("correct", "wrong"):
        raise ValueError('verdict must be "correct" or "wrong"')

    repair_hints = payload.get("repair_hints")
    if not isinstance(repair_hints, str):
        raise ValueError("repair_hints must be a string")

    clean = not report["critical_errors"] and not report["gaps"]
    if clean != (verdict == "correct"):
        raise ValueError(
            'verdict must be "correct" iff critical_errors and gaps are both empty'
        )
    if verdict == "correct" and repair_hints != "":
        raise ValueError('verdict "correct" requires empty repair_hints')
    if verdict == "wrong" and not repair_hints.strip():
        raise ValueError('verdict "wrong" requires non-empty repair_hints')

    return payload
