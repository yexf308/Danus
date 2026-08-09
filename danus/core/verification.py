"""Deterministic validation for the verifier's final JSON contract."""

from __future__ import annotations

from typing import Any, Dict, Optional


# This is the wire/output contract implemented by this validator.  The verify
# service checks its captured CLI JSON schema against this constant at process
# import, and callers declare the same value before any paid verification may
# start.  Bump it whenever an incompatible verifier output contract ships.
VERIFICATION_OUTPUT_PROTOCOL_VERSION = 3


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


def _candidate_lines(text: str) -> list[str]:
    """Return logical lines without changing any non-newline character."""
    return text.splitlines()


def _validate_candidate_evidence(
    evidence: Any,
    *,
    statement: Optional[str],
    proof: Optional[str],
    path: str,
) -> None:
    """Validate a finding's verbatim candidate-line anchor.

    This deliberately checks provenance, not mathematical truth.  A verifier
    may still be wrong about what an exact line implies, but it may no longer
    support a critical error with a normalized, truncated, or invented quote.
    """
    if not isinstance(evidence, dict):
        raise ValueError(f"{path} must be a dict")
    _require_exact_keys(
        evidence,
        {"source", "line", "exact_line"},
        path,
    )
    source = evidence.get("source")
    if source not in ("statement", "proof"):
        raise ValueError(f'{path}.source must be "statement" or "proof"')
    line = evidence.get("line")
    if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
        raise ValueError(f"{path}.line must be a positive integer")
    exact_line = evidence.get("exact_line")
    if not isinstance(exact_line, str):
        raise ValueError(f"{path}.exact_line must be a string")
    if not exact_line.strip():
        raise ValueError(f"{path}.exact_line must contain non-whitespace candidate text")

    candidate = statement if source == "statement" else proof
    if candidate is None:
        raise ValueError(
            f"{path} cannot be authenticated without the original candidate "
            f"{source}"
        )
    lines = _candidate_lines(candidate)
    if line > len(lines):
        raise ValueError(
            f"{path}.line {line} is outside candidate {source} "
            f"({len(lines)} logical lines)"
        )
    if exact_line != lines[line - 1]:
        raise ValueError(
            f"{path}.exact_line is not the verbatim candidate {source} line {line}"
        )


def validate_verification_output(
    payload: Any,
    *,
    statement: Optional[str] = None,
    proof: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``payload`` iff it satisfies the strict verdict contract.

    Mathematical judgment remains the verifier's job. This validator prevents a
    malformed or self-contradictory agent response (for example ``correct`` plus
    a non-empty gap list) from crossing the write gate.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    _require_exact_keys(
        payload,
        {
            "output_schema_version",
            "verification_status",
            "verification_report",
            "verdict",
            "needs_expanded_proofs",
            "repair_hints",
        },
        "payload",
    )

    if payload.get("output_schema_version") != VERIFICATION_OUTPUT_PROTOCOL_VERSION:
        raise ValueError(
            "output_schema_version must be exactly "
            f"{VERIFICATION_OUTPUT_PROTOCOL_VERSION}"
        )

    verification_status = payload.get("verification_status")
    if verification_status not in ("final", "needs_context"):
        raise ValueError(
            'verification_status must be "final" or "needs_context"'
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
            expected_keys = {"location", "issue", "candidate_evidence"}
            _require_exact_keys(finding, expected_keys, f"each {key} entry")
            if not isinstance(finding.get("location"), str) or not finding["location"]:
                raise ValueError(f"each {key} entry needs a non-empty string location")
            if not isinstance(finding.get("issue"), str) or not finding["issue"]:
                raise ValueError(f"each {key} entry needs a non-empty string issue")
            _validate_candidate_evidence(
                finding.get("candidate_evidence"),
                statement=statement,
                proof=proof,
                path=f"each {key} entry.candidate_evidence",
            )

    verdict = payload.get("verdict")
    if verdict not in ("correct", "wrong"):
        raise ValueError('verdict must be "correct" or "wrong"')

    repair_hints = payload.get("repair_hints")
    if not isinstance(repair_hints, str):
        raise ValueError("repair_hints must be a string")

    requests = payload.get("needs_expanded_proofs")
    if not isinstance(requests, list):
        raise ValueError("needs_expanded_proofs must be a list")
    requested_ids: set[str] = set()
    for request in requests:
        if not isinstance(request, dict):
            raise ValueError("each needs_expanded_proofs entry must be a dict")
        _require_exact_keys(
            request, {"id", "reason"}, "each needs_expanded_proofs entry"
        )
        fact_id = request.get("id")
        reason = request.get("reason")
        if not isinstance(fact_id, str) or not fact_id:
            raise ValueError("each expansion request needs a non-empty string id")
        if fact_id in requested_ids:
            raise ValueError(f"duplicate expansion request id: {fact_id}")
        requested_ids.add(fact_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                "each expansion request needs a non-empty string reason"
            )

    if verification_status == "needs_context":
        if verdict != "wrong":
            raise ValueError('verification_status "needs_context" requires verdict "wrong"')
        if not requests:
            raise ValueError(
                'verification_status "needs_context" requires at least one expansion request'
            )
        if report["critical_errors"] or report["gaps"]:
            raise ValueError(
                'verification_status "needs_context" requires empty findings'
            )
        if repair_hints != "":
            raise ValueError(
                'verification_status "needs_context" requires empty repair_hints'
            )
        # This is a control response, never a final mathematical verdict. The
        # request reasons carry the explanation; only a later fresh session may
        # emit mathematical findings.
        return payload

    if requests:
        raise ValueError('verification_status "final" requires no expansion requests')

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
