"""Guards for the Codex CLI schema and the stricter production validator."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest

from danus.core import validate_verification_output


_RESPONSES_SCHEMA_KEYS = {
    "$schema",
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


@pytest.mark.parametrize(
    "payload",
    [
        {
            "verification_report": {
                "summary": "a gap remains",
                "critical_errors": [],
                "gaps": [{"location": "step 2", "issue": "claim is unproved"}],
            },
            "verdict": "correct",
            "repair_hints": "",
        },
        {
            "verification_report": {
                "summary": "no finding was reported",
                "critical_errors": [],
                "gaps": [],
            },
            "verdict": "wrong",
            "repair_hints": "add a proof",
        },
    ],
    ids=["correct-with-gap", "wrong-without-finding"],
)
def test_production_validator_still_rejects_inconsistent_verdict(payload: Any) -> None:
    with pytest.raises(ValueError, match="verdict"):
        validate_verification_output(payload)
