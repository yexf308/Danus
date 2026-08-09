"""Guards for the Codex CLI schema and the stricter production validator."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
import re
from typing import Any

import pytest

from danus.core import validate_verification_output

_STMT = "For every h, d <= h < M/2."
_PROOF = "The endpoint range is [0, 1], so d ≤ h < M/2."


def _evidence(*, source: str = "proof", line: int = 1, exact_line: str = _PROOF):
    return {"source": source, "line": line, "exact_line": exact_line}


def _validate(payload: Any) -> dict[str, Any]:
    return validate_verification_output(payload, statement=_STMT, proof=_PROOF)


_RESPONSES_SCHEMA_KEYS = {
    "$defs",
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "items",
}


def _assert_responses_schema_subset(schema: Any) -> None:
    """Recursively reject conditionals/combinators and other fragile keywords."""
    assert isinstance(schema, dict)
    assert set(schema) <= _RESPONSES_SCHEMA_KEYS

    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        assert set(schema.get("required", [])) == set(schema.get("properties", {}))

    for child in schema.get("properties", {}).values():
        _assert_responses_schema_subset(child)
    for child in schema.get("$defs", {}).values():
        _assert_responses_schema_subset(child)
    if isinstance(schema.get("items"), dict):
        _assert_responses_schema_subset(schema["items"])


def test_canonical_and_packaged_contract_examples_are_self_consistent() -> None:
    root = Path(__file__).resolve().parents[3]
    canonical = (root / "agents" / "contracts" / "verifier.md").read_text(
        encoding="utf-8"
    )
    packaged = resources.files("danus.verify._resources").joinpath(
        "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert packaged == canonical

    examples = [
        json.loads(block)
        for block in re.findall(r"```json\s*(.*?)\s*```", canonical, re.DOTALL)
        if '"output_schema_version"' in block
    ]
    assert {example["verdict"] for example in examples} == {"correct", "wrong"}
    for example in examples:
        report = example["verification_report"]
        findings = report["critical_errors"] + report["gaps"]
        if example["verdict"] == "correct":
            assert findings == []
            assert example["repair_hints"] == ""
            proof = "A complete candidate proof."
        else:
            assert findings
            assert example["repair_hints"].strip()
            proof = findings[0]["candidate_evidence"]["exact_line"]
        validate_verification_output(
            example, statement="A candidate statement.", proof=proof
        )


def test_cli_output_schema_uses_conservative_responses_subset() -> None:
    schema_path = resources.files("danus.verify").joinpath(
        "verification_output.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    _assert_responses_schema_subset(schema)
    forbidden = {
        "$schema", "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
        "dependentRequired", "dependentSchemas", "minLength", "pattern",
        "uniqueItems",
    }

    def assert_forbidden_absent(value: Any) -> None:
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            for child in value.values():
                assert_forbidden_absent(child)
        elif isinstance(value, list):
            for child in value:
                assert_forbidden_absent(child)

    assert_forbidden_absent(schema)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "output_schema_version": 3,
            "verification_status": "final",
            "verification_report": {
                "summary": "a gap remains",
                "critical_errors": [],
                "gaps": [{
                    "location": "step 2",
                    "issue": "claim is unproved",
                    "candidate_evidence": _evidence(),
                }],
            },
            "verdict": "correct",
            "needs_expanded_proofs": [],
            "repair_hints": "",
        },
        {
            "output_schema_version": 3,
            "verification_status": "final",
            "verification_report": {
                "summary": "no finding was reported",
                "critical_errors": [],
                "gaps": [],
            },
            "verdict": "wrong",
            "needs_expanded_proofs": [],
            "repair_hints": "add a proof",
        },
    ],
    ids=["correct-with-gap", "wrong-without-finding"],
)
def test_production_validator_still_rejects_inconsistent_verdict(payload: Any) -> None:
    with pytest.raises(ValueError, match="verdict"):
        _validate(payload)


def _needs_context_payload() -> dict[str, Any]:
    return {
        "output_schema_version": 3,
        "verification_status": "needs_context",
        "verification_report": {
            "summary": "a specific ancestor proof is required",
            "critical_errors": [],
            "gaps": [],
        },
        "verdict": "wrong",
        "needs_expanded_proofs": [
            {"id": "aaaaaaaaaaaaaaaa", "reason": "inspect the cited lemma"}
        ],
        "repair_hints": "",
    }


def test_production_validator_accepts_control_response_but_not_as_correct() -> None:
    payload = _needs_context_payload()
    assert _validate(payload) is payload
    payload = _needs_context_payload()
    payload["verdict"] = "correct"
    with pytest.raises(ValueError, match="needs_context"):
        _validate(payload)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda payload: payload.update(needs_expanded_proofs=[]), "at least one"),
        (
            lambda payload: payload["needs_expanded_proofs"].append(
                {"id": "aaaaaaaaaaaaaaaa", "reason": "again"}
            ),
            "duplicate expansion",
        ),
        (
            lambda payload: payload["needs_expanded_proofs"][0].update(reason=" "),
            "non-empty string reason",
        ),
        (
            lambda payload: payload["verification_report"]["gaps"].append(
                {
                    "location": "proof",
                    "issue": "premature finding",
                    "candidate_evidence": _evidence(),
                }
            ),
            "empty findings",
        ),
        (lambda payload: payload.update(repair_hints="repair"), "empty repair_hints"),
    ],
)
def test_production_validator_rejects_malformed_control_response(
    mutation: Any, match: str
) -> None:
    payload = _needs_context_payload()
    mutation(payload)
    with pytest.raises(ValueError, match=match):
        _validate(payload)


def test_production_validator_rejects_expansion_requests_on_final() -> None:
    payload = _needs_context_payload()
    payload["verification_status"] = "final"
    payload["verdict"] = "correct"
    with pytest.raises(ValueError, match="requires no expansion"):
        _validate(payload)


@pytest.mark.parametrize("bucket", ["critical_errors", "gaps"])
@pytest.mark.parametrize(
    "evidence,match",
    [
        ({"source": "proof", "line": 1}, "required keys"),
        (_evidence(source="context"), "source"),
        (_evidence(line=0), "positive integer"),
        (_evidence(line=2), "outside candidate proof"),
        (_evidence(exact_line="The endpoint range is [0, 1], so d < h < M/2."), "verbatim"),
    ],
)
def test_every_finding_requires_exact_original_candidate_line(
    bucket: str, evidence: dict[str, Any], match: str
) -> None:
    payload = {
        "output_schema_version": 3,
        "verification_status": "final",
        "verification_report": {
            "summary": "reported mismatch",
            "critical_errors": [],
            "gaps": [],
        },
        "verdict": "wrong",
        "needs_expanded_proofs": [],
        "repair_hints": "repair the reported mismatch",
    }
    payload["verification_report"][bucket].append({
        "location": "proof line 1",
        "issue": "The candidate allegedly used a strict bound.",
        "candidate_evidence": evidence,
    })
    with pytest.raises(ValueError, match=match):
        _validate(payload)


def test_exact_non_strict_and_endpoint_evidence_is_accepted_without_rewriting_verdict() -> None:
    payload = {
        "output_schema_version": 3,
        "verification_status": "final",
        "verification_report": {
            "summary": "reported mismatch",
            "critical_errors": [],
            "gaps": [{
                "location": "proof line 1",
                "issue": "A verifier-authored mathematical allegation.",
                "candidate_evidence": _evidence(),
            }],
        },
        "verdict": "wrong",
        "needs_expanded_proofs": [],
        "repair_hints": "review the allegation against the verbatim line",
    }
    assert _validate(payload) is payload
    assert payload["verdict"] == "wrong"


@pytest.mark.parametrize("bucket", ["critical_errors", "gaps"])
def test_blank_candidate_line_cannot_serve_as_finding_evidence(bucket: str) -> None:
    payload = {
        "output_schema_version": 3,
        "verification_status": "final",
        "verification_report": {
            "summary": "blank anchor",
            "critical_errors": [],
            "gaps": [],
        },
        "verdict": "wrong",
        "needs_expanded_proofs": [],
        "repair_hints": "anchor the finding to relevant candidate text",
    }
    payload["verification_report"][bucket].append({
        "location": "proof line 2",
        "issue": "An allegation without relevant candidate text.",
        "candidate_evidence": {
            "source": "proof",
            "line": 2,
            "exact_line": "",
        },
    })
    with pytest.raises(ValueError, match="non-whitespace candidate text"):
        validate_verification_output(
            payload,
            statement=_STMT,
            proof="first line\n\nthird line",
        )


def test_legacy_v2_contract_is_rejected_fail_closed() -> None:
    payload = _needs_context_payload()
    payload["output_schema_version"] = 2
    with pytest.raises(ValueError, match="exactly 3"):
        _validate(payload)
