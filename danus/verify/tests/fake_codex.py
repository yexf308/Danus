#!/usr/bin/env python3
"""A stand-in for the `codex` CLI, for PLUMBING tests of the verify service.

The real service cold-starts `codex exec ... <prompt>`; the codex agent reads
AGENTS.md and emits a final schema-constrained JSON message, which the CLI captures
as verification.json. This stub does NOT judge any mathematics -- it only exercises the
service's subprocess + file-readback + verdict-propagation plumbing
deterministically, with no codex install and no API spend.

Verdict rule (deterministic, plumbing only):
  - prompt contains "[[FAKE:wrong]]"  -> verdict "wrong"
  - otherwise                         -> verdict "correct"

Point the service at it with DANUS_CODEX_BIN=/abs/path/to/fake_codex.py . It accepts
(and ignores) the real codex flags; like current ``codex exec -``, it reads the
prompt from stdin. A final literal prompt argument remains supported for older
plumbing callers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("fake_codex: no prompt source\n")
        return 2
    prompt = sys.stdin.read() if sys.argv[-1] == "-" else sys.argv[-1]

    if "--output-last-message" not in sys.argv:
        sys.stderr.write("fake_codex: missing --output-last-message\n")
        return 3
    out_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])

    if "[[FAKE:wrong]]" in prompt:
        payload = {
            "output_schema_version": 2,
            "verification_status": "final",
            "verification_report": {
                "summary": "FAKE stub verdict (plumbing test): marker [[FAKE:wrong]] present.",
                "critical_errors": [
                    {"location": "proof", "issue": "fake_codex injected critical error for the reject path"}
                ],
                "gaps": [],
            },
            "verdict": "wrong",
            "needs_expanded_proofs": [],
            "repair_hints": "This is a fake reject from fake_codex.py (plumbing only).",
        }
    else:
        payload = {
            "output_schema_version": 2,
            "verification_status": "final",
            "verification_report": {
                "summary": "FAKE stub verdict (plumbing test): no error marker; accepting.",
                "critical_errors": [],
                "gaps": [],
            },
            "verdict": "correct",
            "needs_expanded_proofs": [],
            "repair_hints": "",
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    sys.stdout.write(f"fake_codex: returned {payload['verdict']} verdict\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
