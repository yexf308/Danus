"""Guards for the Codex CLI schema and the stricter production validator."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest

from danus.core import validate_verification_output


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
            "output_schema_version": 2,
            "verification_status": "final",
            "verification_report": {
                "summary": "a gap remains",
                "critical_errors": [],
                "gaps": [{"location": "step 2", "issue": "claim is unproved"}],
            },
            "verdict": "correct",
            "needs_expanded_proofs": [],
            "repair_hints": "",
        },
        {
            "output_schema_version": 2,
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
        validate_verification_output(payload)


def _needs_context_payload() -> dict[str, Any]:
    return {
        "output_schema_version": 2,
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
    assert validate_verification_output(payload) is payload
    payload = _needs_context_payload()
    payload["verdict"] = "correct"
    with pytest.raises(ValueError, match="needs_context"):
        validate_verification_output(payload)


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
                {"location": "proof", "issue": "premature finding"}
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
        validate_verification_output(payload)


def test_production_validator_rejects_expansion_requests_on_final() -> None:
    payload = _needs_context_payload()
    payload["verification_status"] = "final"
    payload["verdict"] = "correct"
    with pytest.raises(ValueError, match="requires no expansion"):
        validate_verification_output(payload)
