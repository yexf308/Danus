"""Schema guard for the verifier's final JSON contract (skill-3 output).

This pins the shape the `synthesize-verification-report` skill must emit and the
verify service returns verbatim as the `/verify` HTTP response:

    {
      "output_schema_version": 2,
      "verification_status": "final" | "needs_context",
      "verification_report": {"summary", "critical_errors": [...], "gaps": [...]},
      "verdict": "correct" | "wrong",
      "needs_expanded_proofs": [{"id", "reason"}, ...],
      "repair_hints": ""            # non-empty iff verdict == "wrong"
    }

It complements the verify service's fake-codex sanity harness; it is a cheap,
LLM-independent guard that the contract stays shaped. It asserts only the
structural contract, not any mathematical judgement.
"""

import pytest

from danus.core import validate_verification_output


def _final(payload):
    return {
        "output_schema_version": 2,
        "verification_status": "final",
        "needs_expanded_proofs": [],
        **payload,
    }


def test_accept_clean_proof():
    validate_verification_output(
        _final({
            "verification_report": {"summary": "ok", "critical_errors": [], "gaps": []},
            "verdict": "correct",
            "repair_hints": "",
        })
    )


def test_reject_on_critical_error():
    validate_verification_output(
        _final({
            "verification_report": {
                "summary": "bad implication",
                "critical_errors": [{"location": "Lemma 3", "issue": "A does not imply B."}],
                "gaps": [],
            },
            "verdict": "wrong",
            "repair_hints": "Prove A => B or drop the step.",
        })
    )


def test_reject_on_gap_alone():
    # Gaps alone force "wrong" — never relax to "no critical errors only".
    validate_verification_output(
        _final({
            "verification_report": {
                "summary": "missing bound",
                "critical_errors": [],
                "gaps": [{"location": "proof paragraph 2", "issue": "boundedness unproved."}],
            },
            "verdict": "wrong",
            "repair_hints": "Add the boundedness argument.",
        })
    )


@pytest.mark.parametrize(
    "payload",
    [
        # correct verdict with a finding present
        {
            "verification_report": {
                "summary": "x",
                "critical_errors": [{"location": "L1", "issue": "bad"}],
                "gaps": [],
            },
            "verdict": "correct",
            "repair_hints": "",
        },
        # wrong verdict but no findings
        {
            "verification_report": {"summary": "x", "critical_errors": [], "gaps": []},
            "verdict": "wrong",
            "repair_hints": "something",
        },
        # correct verdict with non-empty repair_hints
        {
            "verification_report": {"summary": "x", "critical_errors": [], "gaps": []},
            "verdict": "correct",
            "repair_hints": "leftover",
        },
        # wrong verdict with empty repair_hints
        {
            "verification_report": {
                "summary": "x",
                "critical_errors": [{"location": "L1", "issue": "bad"}],
                "gaps": [],
            },
            "verdict": "wrong",
            "repair_hints": "   ",
        },
        # unknown verdict value
        {
            "verification_report": {"summary": "x", "critical_errors": [], "gaps": []},
            "verdict": "maybe",
            "repair_hints": "",
        },
        # finding missing issue
        {
            "verification_report": {
                "summary": "x",
                "critical_errors": [{"location": "L1"}],
                "gaps": [],
            },
            "verdict": "wrong",
            "repair_hints": "fix it",
        },
        # misplaced findings cannot hide behind an otherwise-clean report
        {
            "verification_report": {
                "summary": "x",
                "critical_errors": [],
                "gaps": [],
            },
            "errors": [{"location": "L1", "issue": "bad"}],
            "verdict": "correct",
            "repair_hints": "",
        },
        {
            "verification_report": {
                "summary": "x",
                "critical_errors": [],
                "gaps": [],
                "errors": [{"location": "L1", "issue": "bad"}],
            },
            "verdict": "correct",
            "repair_hints": "",
        },
        # findings themselves are exact-shape objects
        {
            "verification_report": {
                "summary": "x",
                "critical_errors": [
                    {"location": "L1", "issue": "bad", "severity": "critical"}
                ],
                "gaps": [],
            },
            "verdict": "wrong",
            "repair_hints": "fix it",
        },
    ],
)
def test_contract_violations_raise(payload):
    with pytest.raises(ValueError):
        validate_verification_output(_final(payload))


def test_needs_context_is_valid_control_flow_not_a_math_verdict():
    validate_verification_output(
        {
            "output_schema_version": 2,
            "verification_status": "needs_context",
            "verification_report": {
                "summary": "Need the exact ancestor proof.",
                "critical_errors": [],
                "gaps": [],
            },
            "verdict": "wrong",
            "needs_expanded_proofs": [
                {"id": "aaaaaaaaaaaaaaaa", "reason": "Audit the cited lemma."}
            ],
            "repair_hints": "",
        }
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"verdict": "correct"},
        {"needs_expanded_proofs": []},
        {
            "needs_expanded_proofs": [
                {"id": "aaaaaaaaaaaaaaaa", "reason": "first"},
                {"id": "aaaaaaaaaaaaaaaa", "reason": "duplicate"},
            ]
        },
        {"repair_hints": "not a final repair"},
    ],
)
def test_needs_context_semantic_violations_raise(updates):
    payload = {
        "output_schema_version": 2,
        "verification_status": "needs_context",
        "verification_report": {
            "summary": "Need context.", "critical_errors": [], "gaps": [],
        },
        "verdict": "wrong",
        "needs_expanded_proofs": [
            {"id": "aaaaaaaaaaaaaaaa", "reason": "Audit the cited lemma."}
        ],
        "repair_hints": "",
    }
    payload.update(updates)
    with pytest.raises(ValueError):
        validate_verification_output(payload)
