---
name: identify-key-failures
description: Synthesize the common stuck points across failed decomposition plans. Use when the current batch of decomposition plans has failed — whether they failed already at direct proving or only after further attempts.
---

# Identify Key Failures

Use this skill to turn many failed attempts into reusable guidance for the next planning round.

## Input Contract

Read:

- the failed decomposition plans
- direct-proving stuck points
- existing `failed_paths`
- relevant `counterexamples` and `toy_examples`

## Procedure

1. Gather the reports from all failed plans. If only direct proving has run so far, work directly from the direct-proving failures.
2. List the key stuck points for each plan.
3. Identify common points across those failures:
   - recurring obstructions or counterexamples
   - decomposition patterns that keep breaking
   - search gaps or missing background facts
4. Summarize what the failures suggest for the next generation of decomposition plans.
5. When all current decomposition plans have failed and no pattern is leading anywhere, publish the synthesized `dead_end` below. In `reasoning_first_v1`, a root publishes only its evidence-backed obstruction. A critic must first preserve an independent analysis, then retrieve the designated root record with `gm_get` using its exact id (never a BM25 `gm_search` substitute), and may confirm it only by passing that same id in `links.confirms_entry_id`; the gateway injects the protected coordination metadata. Neither record calls, prepares, or authorizes an advisor; the coordinator can create only a content-free owner-facing recommendation.
6. Save the synthesized failure knowledge to `failed_paths` so later planning skills can use it.
7. After recording the failure synthesis, return control to `$propose-subgoal-decomposition-plans`.

## Output Contract

Publish the failure synthesis to global memory with `gm_add` (kind `dead_end`):
`claim` = the common stuck points, `evidence` = the per-plan failures, so siblings
skip these paths. Carry these fields:

```json
{
  "record_type": "key_failures_summary",
  "failed_plan_ids": ["..."],
  "plan_failures": [
    {
      "plan_id": "...",
      "stuck_points": ["..."]
    }
  ],
  "common_failures": ["..."],
  "implications_for_next_plans": ["..."]
}
```

In `reasoning_first_v1`, the gateway supplies the protected generation, lane,
and slot as `links.coordination`; never copy them from model text. A root calls
`gm_add` without a confirmation link. A critic confirms only with the actual
tool argument `links={"confirms_entry_id":"<exact returned root gm id>"}`.
Writing `confirms_entry_id` merely inside the evidence JSON has no coordination
effect, and a critic must never invent or abbreviate the root id.

Publish at most one such synthesis in the admitted phase. Also note in local
memory (`events`) that a new planning round is needed. Do not duplicate the same
content as separate `obstacle`, `dead_end`, and `plan` records.

## Tools

- `gm_add` (publish the dead_end synthesis)
- `gm_get` (exactly hydrate the designated 16-hex root checkpoint; unique and
  bounded to 16 KiB)
- `gm_search` (gather the failed plans and stuck points across the swarm)

## Failure Logging

If the reports are too weak to identify meaningful common failures, note in local
memory (`events`) `event_type="key_failures_inconclusive"` and state what
information is still missing. Do not publish a confirmation and do not recommend
Pro merely because the turn is old, expensive, or subjectively stuck.
