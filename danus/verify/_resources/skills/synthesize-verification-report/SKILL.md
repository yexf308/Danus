---
name: synthesize-verification-report
description: Aggregate all detected errors and gaps into the final verification report, apply strict accept/reject logic, and produce repair hints when rejected.
---

# Synthesize Verification Report

Produce the final verification output JSON and verdict.

## Input Contract

Aggregate all findings you produced earlier in this verification session — the
per-item records from the sequential check and the reference check, held in
context. Each issue must include `location` and `issue`.

## Procedure

1. Collect all critical errors and all gaps from previous checks.
2. Build a complete `verification_report` object with:
   - `summary`
   - `critical_errors`
   - `gaps`
3. Apply strict verdict rule:
   - `correct` iff `critical_errors=[]` and `gaps=[]`.
   - otherwise `wrong`.
4. If verdict is `wrong`, produce concrete non-empty `repair_hints`.
5. Self-check the JSON against its schema before emitting — do this by reasoning, not by calling a tool:
   - `verdict` is exactly `"correct"` or `"wrong"`,
   - `repair_hints` is non-empty **iff** `verdict == "wrong"` (empty string when `"correct"`),
   - every entry of `critical_errors` and `gaps` has both `location` and `issue`,
   - the top-level object, report, and each finding have exactly the documented
     keys and no unknown or misplaced fields,
   - the verdict is consistent with the rule in step 3 (any critical error or gap forces `"wrong"`).
   If the self-check fails, correct the object before continuing.
6. Emit the final JSON as your final message and nothing else. The Codex CLI
   captures that last message into the run's result file under a strict output
   schema; do not write files or invoke a tool to persist it. The verify service
   validates the JSON again and adds transport-level context attestation when
   applicable.

## Output Contract

Final output JSON:

```json
{
  "verification_report": {
    "summary": "string",
    "critical_errors": [],
    "gaps": []
  },
  "verdict": "correct",
  "repair_hints": ""
}
```

If there is any error or gap, verdict must be `"wrong"` and `repair_hints` must be non-empty.

## Tools

- None — you build and self-check the report by reasoning. Emit it as the final
  message; the Codex CLI captures it and the verify service validates it before
  returning an augmented `/verify` response.

(The verdict is the verifier's only output — no memory is written; the worker does
all writing to global memory and the fact graph.)
