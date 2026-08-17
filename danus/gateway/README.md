# danus/gateway — role-gated MCP server (the permission gate)

The **only sanctioned door to the truth stores.** A stdio MCP server (`danus-core`)
whose exposed tools depend on the caller's role — permission is enforced by *which
tools a role can even see*, not by prompt convention.

```
danus/gateway/
  server.py            the 7 MCP tools + the fact_submit write-gate; build_app(role)
  roles.py             ROLE_TOOLS — the role→tools table (the security surface)
  __main__.py          `python -m danus.gateway` → build_app().run() (role from DANUS_ROLE)
  tests/test_gateway.py
```

## The role table (`roles.py`)

| role | tools |
|---|---|
| worker | `gm_add gm_search fact_submit fact_search fact_context search_arxiv_theorems` |
| main | `gm_add gm_search fact_search fact_context fact_revoke search_arxiv_theorems` (**no `fact_submit`**) |
| verifier | `search_arxiv_theorems` only (read-only) |

Ungated tools are **physically absent** from the surface. Unknown, mis-typed, or
*unset* role → **fail-closed** to the verifier set; the full dev set requires the
explicit `DANUS_ROLE=all`.

## The write-gate (`fact_submit`, in `server.py`)

The single path a fact enters truth: (1) build complete lazy context for every
declared predecessor: the complete transitive ancestor statement/edge/fact-local
definition closure, selected immutable definitions, and no ancestor proof; (2)
GET verify `/health`, require output protocol 3 plus a bundle digest, echo both
into the POST, then call the verify service (`DANUS_VERIFY_URL`) and require its server-side
context-digest attestation; (3) on `needs_context`, authenticate every requested
id as a strict ancestor, hydrate only those whole proofs from canonical fact
files, and repeat in a fresh session within the configured round/count/record
budgets; (4) after a final `correct` verdict, rebuild that exact expansion
snapshot and add under the graph mutation lock; (5) durably attempt to trace every
round and the final outcome to global memory,
returning an explicit `trace_error` without hiding a written fact id. Missing,
revoked, incomplete, or over-budget context blocks before verification. A
`correct` verdict is necessary but not sufficient for a write. For compatibility,
`accepted` reports that mathematical verdict; end-to-end publication is reported
separately as `promoted: true`, `submission_status: "promoted"`, and a non-null
`fact_id`. A stale locked snapshot, glossary conflict, or storage failure returns
`accepted: true`, `verification_verdict: "correct"`, `promoted: false`,
`submission_status: "verified_not_promoted"`, `fact_id: null`, and `write_error`.
Workers repair/retry that result and monitors must not count it as a published
fact. A rare fsync ambiguity in which crash recovery may either preserve or
rollback a commit returns `promoted: null`,
`submission_status: "promotion_unknown"`, and no fact id instead of making a
false claim; monitors do not count that response either. During a rolling
upgrade, a valid `fact_id` is the safe fallback when the
new promotion fields are absent; `accepted` alone is not. Service
unreachable or a malformed/self-contradictory verdict → clean error, nothing
written. Before a verified candidate is promoted, the gateway reconstructs and
compares the context snapshot under the same cross-process mutation lock used by
revoke, so a concurrent change becomes either a verified-but-not-promoted retry or
a cascade that includes the new fact—never stale truth.
The verify service also requires the literal internal ids cited by the proof to
match the declared direct predecessor set exactly.
An old service whose health response lacks the output protocol/bundle fields is
rejected before any paid POST. Conversely, a new service requires the caller to
declare protocol 3 and its health-probed digest before run-directory allocation,
so old/new rolling combinations fail closed rather than spending on an output
contract the other side cannot validate.
Every successful HTTP response body is independently capped at 8 MiB and read as
`limit + 1`; oversized, truncated/read-error, or malformed 2xx responses fail
closed and the response handle is closed on every path. A same-digest but
misbehaving endpoint therefore cannot stream an unbounded success body into the
gateway process.

The adaptive defaults are two expansion rounds, eight total expanded proofs,
and 200000 canonical proof-record characters. Unknown, non-ancestor, current,
duplicate, already-expanded, missing, revoked, or no-progress requests fail
closed as protocol errors. Graphify or other discovery indexes never participate
in closure completeness, proof hydration, digest construction, or verdicts.

`fact_search` remains full-text BM25 with a statement-only result payload.
`fact_context` reads explicit ids and is statement/relations-only by default;
callers opt into selected-root or all-proof
hydration and receive completeness/budget metadata without partial fact or
definition slicing. The response binds its requested scope, records, and selected
glossary snapshot with a SHA-256 digest.

## Launched by

`bin/danus-mcp` (role=main, for codex via `.codex/config.toml`); each worker's
`.codex/config.toml` (role=worker); the verify launcher injects it (role=verifier) so
the judge can call `search_arxiv_theorems`. Config (`DANUS_PROJECT_DIR`,
`DANUS_AGENTS_ROOT`, `DANUS_VERIFY_URL`, role, author) is read at **call time**.

## Pinned interfaces (ARCHITECTURE §4 — change both ends together)

The 7-tool set + role table; `python -m danus.gateway` launch; the verify HTTP seam.

## Tests

`python -m pytest danus/gateway/` (offline; the verify call is stubbed).
