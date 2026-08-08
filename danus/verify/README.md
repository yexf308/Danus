# danus.verify — the verification authority behind the write-gate

An **informal-LLM proof verifier** behind a tiny HTTP gateway. It is the sole
authority on mathematical correctness: a worker's `fact_submit` (in `danus.gateway`)
calls it. A `correct` verdict is necessary but not sufficient for a graph write;
the gateway must also recheck the exact context and add under the graph mutation
lock, and can return an accept-but-write-failed result.

It is **not** a formal / Lean checker — a gpt-5.5 codex agent reads the
natural-language markdown proof (logic, theorem application, external-citation
checking) and returns a verdict. There is **no human in the loop by default** —
research-level target theorems still need expert review before being trusted.

## Black-box contract

```
POST /verify
  request : {"statement": <str, >=1 char>, "proof": <str, >=1 char>,
             "glossary_introduces": <object, optional>,
             "fact_context": <object, optional>}                       # application/json
  200     : {"verification_report": {"summary": str,
                                      "critical_errors": [{"location": str, "issue": str}, ...],
                                      "gaps":            [{"location": str, "issue": str}, ...]},
             "verdict": "correct" | "wrong",
             "repair_hints": str,
             "verification_context_digest": str?} # attested when context supplied;
                                                    # repair_hints="" iff correct
  400     : vacuous/P1/P3/P5 input; incomplete, malformed, or tampered fact_context;
            internal fact_id citation without declared context
  408     : request-body upload exceeded DANUS_VERIFY_BODY_TIMEOUT_SECONDS
  413     : request body or final serialized verifier prompt exceeded its byte cap
  429     : another admitted verification already occupies all configured slots
  422     : request-model validation (empty statement/proof)
  500     : codex failed / wrote no output / output violates the verdict JSON contract
  504     : codex exec timed out (only if CODEX_TIMEOUT_SECONDS is set)

GET /health -> {"status": "ok", "pid": <int>}    # async; never queues behind /verify
                                                 # pid self-identifies the instance so
                                                 # doctor/services can tell OUR verify
                                                 # from a foreign one on a shared port
```

**Fail-closed invariants (enforced in production code, with prompt backstops):**
`verdict == "correct"` ⟺ `critical_errors == []` **and** `gaps == []`.
The raw verifier payload, report, and every finding are exact-shape objects;
unknown or misplaced fields fail closed rather than being ignored.
`fact_context` must have the current compact schema: complete direct-premise
cards, every inherited fact-local definition, immutable global definitions, a
digest/count commitment to the core-validated transitive closure, consistent
whole-record character accounting, and a matching context digest. Ancestor
statements, predecessor proofs, and the closure edge skeleton do not cross into
the model prompt. Project-wide glossary hydration is disabled at this boundary.
Internal 16-hex fact ids are scanned in both the
statement and proof, and their set must exactly equal the declared direct
predecessor ids. The service checks the compact envelope and digests before Codex
starts; the gateway owns closure integrity and recomputes the locked snapshot
before add. The launcher rejects malformed or self-contradictory verdict output. For a
context-bearing request, the service itself adds `verification_context_digest`
to the response; the gateway refuses responses without that exact attestation.

## Modules
- `prechecks.py` — pure, offline-testable: vacuousness + P1/P3/P5 hard prohibitions
  (all env-toggleable, all purely additive — they can only *reject* more).
- `launcher.py` — cold-start codex launcher (via the shared `danus.codex`): `codex
  exec --model gpt-5.5 --config model_reasoning_effort="xhigh" -C <AGENT_HOME>
  -c <danus MCP, role=verifier> --sandbox read-only --ephemeral
  --ignore-user-config --output-schema <schema> -o <verification.json> -`;
  the bounded prompt is delivered over stdin (never process argv), uses an atomic
  run-id, and the CLI captures the schema-constrained last message. Codex stdout
  and stderr are discarded rather than copied to persistent logs; `log.md` is a
  mode-`0600` service-written metadata record containing only timestamps, status,
  and return code. Injects the read-only gateway with the verify service's exact
  interpreter as **`<sys.executable> -m danus.gateway`**.
- `service.py` — FastAPI app (`/verify`, `/health`) and deterministic context
  validation before any verifier process starts.

## Run

```bash
python -m danus.verify          # 127.0.0.1:8091, default CODEX_TIMEOUT_SECONDS=900
```

Binds **loopback by default** (set `VERIFY_HOST=0.0.0.0` if the
gateway runs on another host). Needs a codex CLI: set **`DANUS_CODEX_BIN`** (or
`codex` on PATH / the repo's `bin/codex` wrapper) and
an account via `CODEX_HOME` — **there is no built-in fallback path** (BYO). The
verifier agent runs `-m danus.gateway` with the verify service's exact Python
interpreter, so a wheel-installed virtual environment does not fall through to a
system Python without `danus`.

## Configuration (env vars)

| var | default | meaning |
| --- | --- | --- |
| `VERIFY_HOST` / `VERIFY_PORT` (or `PORT`) | `127.0.0.1` / `8091` | bind addr (`python -m danus.verify`) |
| `DANUS_STATE_DIR` | `$XDG_STATE_HOME/danus`, else `~/.local/state/danus` | writable verifier state root when the two paths below are unset |
| `VERIFY_AGENT_HOME` | `<state>/verify/agent-<resource-digest>` | writable codex `-C` home; packaged AGENTS.md + skills are materialized here, with resource-versioned defaults |
| `VERIFIER_RESULTS_DIR` | `<state>/verify/runs` | per-verification run dirs (sanitized metadata-only `log.md` + CLI-captured `verification.json`) |
| `DANUS_CODEX_BIN` | `<repo>/bin/codex` → `which codex` → bare `"codex"` | the codex binary; resolved via the shared `danus.codex` launcher |
| `DANUS_VERIFY_MODEL` / `DANUS_VERIFY_EFFORT` (fall back to neutral `DANUS_CODEX_MODEL` / `DANUS_CODEX_EFFORT`) | `gpt-5.5` / `xhigh` | codex knobs |
| `CODEX_TIMEOUT_SECONDS` | `0` lib / **`900`** via `python -m danus.verify` | per-verification codex timeout |
| `DANUS_VERIFY_CONTEXT_MAX_CHARS` | `200000` | gateway budget for direct fact cards, all inherited definitions, and immutable global definitions; overflow blocks before `/verify` |
| `DANUS_VERIFY_MAX_PROMPT_BYTES` | `200000` | hard limit on the final UTF-8 prompt, including candidate, escaped context, definitions, and envelope; overflow returns HTTP 413 before Codex starts |
| `DANUS_VERIFY_MAX_REQUEST_BYTES` | `1000000` | hard request-body cap enforced before FastAPI/Pydantic buffers or parses JSON |
| `DANUS_VERIFY_BODY_TIMEOUT_SECONDS` | `10` | total time allowed to upload a `/verify` request body |
| `DANUS_VERIFY_MAX_CONCURRENT_REQUESTS` | `1` | pre-parse admission slots; excess requests receive HTTP 429 instead of starting more cold verifier sessions |
| `VERIFY_MIN_STATEMENT_CHARS` / `VERIFY_MIN_PROOF_CHARS` / `VERIFY_MIN_PROOF_WORDS` | 10 / 30 / 5 | vacuousness thresholds |
| `VERIFY_REJECT_PROBLEM_MD_CITATIONS` / `VERIFY_REJECT_UNPROVEN_CONDITIONALS` / `VERIFY_REJECT_VAGUE_GESTURES` | `1` | toggle P1 / P3 / P5 (`0` disables) |

## How `fact_submit` reaches it
`danus.gateway`'s `fact_submit` first builds complete predecessor context (full
cards for direct premises, every inherited fact-local definition, immutable
global definitions, and a digest/count closure commitment; ancestor statements,
the edge skeleton, and already-verified predecessor proofs are never re-sent),
then POSTs
`{statement, proof, glossary_introduces, fact_context}` to `DANUS_VERIFY_URL`
(e.g. `http://127.0.0.1:8091/verify`). Missing, revoked, incomplete, or
over-budget context blocks before the service is contacted. After a `correct`
verdict it writes only if the same context is still current under the graph
mutation lock, and records the outcome plus context digest to global memory (kind
`verification`).
Until this service is up and `DANUS_VERIFY_URL` is set, `fact_submit` returns a
clear "verify service not wired" error.

Danus deliberately does not slice one candidate proof across hidden verifier
calls: coverage would become ambiguous. If a candidate reaches the prompt cap,
factor it into smaller verified facts and cite their ids. The lazy DAG context
then sends only their direct-premise cards plus required definitions, without
re-sending ancestor statements or any predecessor proofs.

## Trust assumptions (security)

- The verifier runs an ephemeral Codex session with a read-only shell sandbox,
  ignores user config/rules, and returns through a strict output schema. Its
  `VERIFY_AGENT_HOME` contract and skills remain trusted input. Read-only is not
  a host confidentiality boundary: for adversarial proof text, still run this
  service in a dedicated container or low-privilege account with no unrelated
  readable secrets.
- Codex stdout/stderr is never persisted because the CLI may echo the stdin
  prompt, including the candidate proof and lazy fact context. Persistent
  `log.md` files contain only server-generated timestamps, completion status,
  and return code; diagnostic model text must not be copied into them.
- The CLI output schema deliberately uses only a conservative OpenAI Responses
  Structured Outputs subset: exact object shapes, required fields, primitive
  types, arrays, references, and the verdict enum. Cross-field rules cannot be
  expressed there with `allOf`/`if`/`then`; production code immediately runs
  `validate_verification_output()` and fails closed on empty findings, empty
  repair hints, or any verdict/report contradiction before a graph write.
- It is an **LLM judge, not a formal (Lean) checker**, with **no human in the loop
  by default**; a `correct` verdict merely authorizes the gateway's locked
  context-CAS/add step. Research-level target theorems need expert human review
  before being trusted.
- Binds **loopback** by default; `DANUS_VERIFY_TIMEOUT` (900 via `python -m
  danus.verify`) bounds each codex call.
