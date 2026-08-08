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
call the verify service (`DANUS_VERIFY_URL`) and require its server-side
context-digest attestation; (3) on `needs_context`, authenticate every requested
id as a strict ancestor, hydrate only those whole proofs from canonical fact
files, and repeat in a fresh session within the configured round/count/record
budgets; (4) after a final `correct` verdict, rebuild that exact expansion
snapshot and add under the graph mutation lock; (5) durably attempt to trace every
round and the final outcome to global memory,
returning an explicit `trace_error` without hiding a written fact id. Missing,
revoked, incomplete, or over-budget context blocks before verification. A
`correct` verdict is necessary but not sufficient for a write: a stale locked
snapshot returns an explicit accept-but-write-failed result. Service
unreachable or a malformed/self-contradictory verdict → clean error, nothing
written. Before an accepted fact is written, the gateway reconstructs and
compares the context snapshot under the same cross-process mutation lock used by
revoke, so a concurrent change becomes either an accept-but-write-failed retry or
a cascade that includes the new fact—never stale truth.
The verify service also requires the literal internal ids cited by the proof to
match the declared direct predecessor set exactly.

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

`bin/danus-mcp` (role=main, for Claude Code via `.mcp.json`); each worker's
`.codex/config.toml` (role=worker); the verify launcher injects it (role=verifier) so
the judge can call `search_arxiv_theorems`. Config (`DANUS_PROJECT_DIR`,
`DANUS_AGENTS_ROOT`, `DANUS_VERIFY_URL`, role, author) is read at **call time**.

## Pinned interfaces (ARCHITECTURE §4 — change both ends together)

The 7-tool set + role table; `python -m danus.gateway` launch; the verify HTTP seam.

## Tests

`python -m pytest danus/gateway/` (offline; the verify call is stubbed).
