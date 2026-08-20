# Danus — Configuration Reference

All host- and account-specific configuration lives in gitignored `config/*.env`
files; **no path or secret is hardcoded** elsewhere. `scripts/env.sh` sources the
chain and fills defaults:

```
config/codex.env  →  config/danus.env  →  runtime/runtime.env  →  built-in defaults
   (BYO backend)      (host/account)      (machine paths, auto)   (scripts/env.sh)
```

Only `*.env.example` templates are committed; copy them to the real names and edit.
The `bin/` wrappers source `env.sh` for you. Values below are the defaults from
`scripts/env.sh` / `config/danus.env.example`.

## Codex backend (workers + verifier)

| variable | default | meaning |
|---|---|---|
| `CODEX_BACKEND` | `api` | `api` (BYO OpenAI-compatible key) or `chatgpt` (your ChatGPT login) |
| `CODEX_HOME` | `runtime/codex-home` | codex auth/config home (gitignored) |
| `CODEX_API_BASE_URL` | — | (api) your OpenAI-compatible Responses endpoint |
| `CODEX_API_MODEL` | `gpt-5.6-sol` | (api) backend model |
| `DANUS_CODEX_API_KEY` | — | (api) key, **read at run time**, never stored in a file |

These live in `config/codex.env`. See `getting-started.md` §2 and
`scripts/setup-codex.sh`.

## Optional strategy consult

| variable | default | meaning |
|---|---|---|
| `DANUS_CONSULT_TRANSPORT` | `off` | optional attended transport: `off` \| `gpt_pro` \| `claude_api` \| `claude_code`; paid/API transports are explicit opt-ins |
| `DANUS_CONSULT_API_KEY` | — | (gpt_pro) key for the OpenAI-compatible Responses API |
| `DANUS_CONSULT_BASE_URL` | `https://api.openai.com/v1` | (gpt_pro) endpoint |
| `DANUS_CONSULT_MODEL` | `gpt-5.5-pro` | (gpt_pro) model |
| `DANUS_CONSULT_BACKGROUND` | `1` | (gpt_pro) send `background=true`; `0` for a gateway that rejects it (per-call: `--background off`) |
| `DANUS_CONSULT_STORE` | `0` | (gpt_pro) send `store=false`; `1` for a gateway that requires stored responses (per-call: `--store on`) |
| `DANUS_CONSULT_CLAUDE_CODE_MODEL` | `claude-fable-5` | (claude_code) model via the `claude` CLI |
| `DANUS_CONSULT_CLAUDE_CODE_BIN` | `claude` | (claude_code) path to the CLI |
| `DANUS_CONSULT_CLAUDE_CODE_MAX_WALL` | `1800` | (claude_code) hard wall-clock cap per consult (s) |
| `DANUS_CONSULT_CLAUDE_CODE_PRICE_IN` | `10.0` | (claude_code) ledger estimate, USD per 1M input tokens |
| `DANUS_CONSULT_CLAUDE_CODE_PRICE_OUT` | `50.0` | (claude_code) ledger estimate, USD per 1M output tokens |
| `DANUS_CONSULT_CLAUDE_API_KEY` | — (falls back to `ANTHROPIC_API_KEY`) | (claude_api) BYO Anthropic API key |
| `DANUS_CONSULT_CLAUDE_API_BASE_URL` | Anthropic default | (claude_api) only for a proxy |
| `DANUS_CONSULT_CLAUDE_API_MODEL` | `claude-fable-5` | (claude_api) any Claude model |
| `DANUS_CONSULT_CLAUDE_API_FALLBACK` | `claude-opus-4-8` | (claude_api) refusal-fallback model; `off` disables |
| `DANUS_CONSULT_CLAUDE_API_PRICE_IN` | `10.0` | (claude_api) USD per 1M input tokens (real usage) |
| `DANUS_CONSULT_CLAUDE_API_PRICE_OUT` | `50.0` | (claude_api) USD per 1M output tokens (real usage) |

- `gpt_pro` = a paid, per-token OpenAI-compatible model. `claude_api` = the
  Anthropic API via the native SDK (per-token, BYO key; cost from real usage).
  `claude_code` = your Claude subscription via the Claude Code CLI (`claude -p`).
- `chatgpt_pro_browser` is a different, Chrome-only handoff and is never an
  environment choice. The reasoning-first coordinator must first expose its exact
  current content-free recommendation; only then may the attended main agent
  prepare the bound question, stop, and obtain the owner's per-question
  authorization. Broad blocked/stuck evidence alone is insufficient. Merely
  setting the environment value fails closed and creates no request. See
  `browser-advisor.md`.
- `off` = the main agent reasons on its own, no consult.
- The `claude_code` consult runs **isolated**: a throwaway cwd, no settings and no MCP
  servers loaded (`--setting-sources "" --strict-mcp-config` — needs a recent
  `claude` CLI), web-only tools, and the prompt on stdin (never argv, which is
  world-readable on a shared host). It sees the elaboration and the public web,
  nothing else.
- Consult effort is selected per call with `--effort`. Accepted values are
  `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. All transports support
  through `max`; a `gpt_pro` `max` request is never silently retried without its
  requested reasoning effort.

## Models & reasoning effort

All three codex-exec sites (workers, verifier, paper/report renderers) resolve
binary + model + effort through the shared launcher, so names are unified. Neutral
model defaults apply everywhere; per-service overrides win. The verifier effort
is an independent `xhigh` default and intentionally does not inherit
`DANUS_CODEX_EFFORT`.

| variable | default | applies to |
|---|---|---|
| `DANUS_CODEX_BIN` | `<repo>/bin/codex`, else `codex` on PATH | all codex calls |
| `DANUS_CODEX_MODEL` | `gpt-5.6-sol` | neutral default (all sites) |
| `DANUS_CODEX_EFFORT` | `xhigh` | neutral default effort (all sites) |
| `DANUS_VERIFY_MODEL` | neutral (`gpt-5.6-sol`) | verifier model |
| `DANUS_VERIFY_EFFORT` | `xhigh` | verifier effort; independent from the neutral effort |
| `DANUS_WRITE_PAPER_MODEL` / `_EFFORT` | neutral | paper renderer |
| `DANUS_HUMAN_SUMMARY_MODEL` / `_EFFORT` | neutral | human-summary renderer |

## Ports (all loopback)

| variable | default | service |
|---|---|---|
| `VERIFY_PORT` | `8091` | verify service (`127.0.0.1`) |
| `DASHBOARD_PORT` | `8099` | read-only dashboard (`127.0.0.1`) |
| `DANUS_VERIFY_URL` | `http://127.0.0.1:8091/verify` | where `fact_submit` posts |
| `DANUS_VERIFY_CONTEXT_MAX_CHARS` | `200000` | maximum complete statement-closure/expanded-record/selected-definition context; overflow blocks submission before verification |
| `DANUS_VERIFY_MAX_EXPANSION_ROUNDS` | `2` | maximum adaptive proof-hydration rounds after statement-only round zero |
| `DANUS_VERIFY_MAX_EXPANDED_PROOFS` | `8` | maximum cumulative strict-ancestor whole proof records |
| `DANUS_VERIFY_MAX_EXPANDED_PROOF_CHARS` | `200000` | maximum canonical JSON characters across expanded proof records; never sliced |
| `DANUS_VERIFY_MAX_PROMPT_BYTES` | `1000000` | maximum exact final UTF-8 verifier prompt (candidate + escaped context + envelope); gateway preflights the shared serializer before HTTP and launcher rechecks before Codex |
| `DANUS_VERIFY_TIMEOUT` | `3600` | gateway HTTP timeout for one verifier request |
| `CODEX_TIMEOUT_SECONDS` | `0` in the library, `900` via `start-verify.sh` | verifier Codex child timeout; `0` means no library-level timeout |
| `DANUS_VERIFY_MAX_REQUEST_BYTES` | `1000000` | maximum `/verify` request-body bytes, enforced before JSON model parsing |
| `DANUS_VERIFY_BODY_TIMEOUT_SECONDS` | `10` | total request-body upload deadline before HTTP 408 |
| `DANUS_VERIFY_MAX_BODY_UPLOADS` | `32` | bounded request-body upload/parser slots, independent of paid verification |
| `DANUS_VERIFY_QUEUE_LIMIT` | `4` | maximum queued distinct verifier request identities; paid concurrency remains fixed at one |
| `DANUS_VERIFY_QUEUE_WAIT_SECONDS` | `1800` | maximum FIFO wait for distinct work |
| `DANUS_VERIFY_MAX_WAITERS_PER_KEY` | `8` | followers allowed to coalesce onto one exact in-flight request |
| `DANUS_VERIFY_MAX_WAITERS` | `32` | total queued and coalesced waiting callers |
| `DANUS_VERIFY_CACHE_MAX_ENTRIES` | `64` | per-process validated-success LRU entries |
| `DANUS_VERIFY_CACHE_MAX_BYTES` | `16777216` | total canonical result bytes in the verifier cache |
| `DANUS_VERIFY_CACHE_TTL_SECONDS` | `3600` | monotonic TTL for validated-success cache entries |
| `DANUS_FACTGRAPH_LOCK_TIMEOUT_SECONDS` | `10` | bounded wait for the external project truth-graph lock; timeout fails closed |
| `VERIFY_HOST` | `127.0.0.1` | verify bind host (keep loopback — see security doc) |

## Literature retrieval

| variable | default | meaning |
|---|---|---|
| `DANUS_RETRIEVAL_MODE` | `open` in production, `off` when an eval cutoff is present | `open`, `strict`, `dated`, or `off` |
| `DANUS_ARXIV_INDEX_URL` | `https://leansearch.net/thm/search` | legacy arXiv theorem index used by `open` and `dated` |
| `DANUS_MATLAS_URL` | `https://matlas.ai/api/search` | official journal/book Matlas endpoint used by `strict` |
| `DANUS_EVAL_CUTOFF` | unset | `YYMM`, `YYYY-MM`, or `YYYYMM`; marks a gated evaluation run |
| `DANUS_EVAL_SOURCE_ID` | unset | comma/space-separated source arXiv ids that must be dropped |
| `DANUS_RETRIEVAL_AUDIT` | unset | owner-side append-only JSONL audit path |
| `DANUS_RETRIEVAL_MAX_BYTES` | `8388608` | external response-body cap before JSON parsing |

The generic `MATLAS_URL` variable is intentionally ignored. It previously
named the arXiv index in Danus but names the official Matlas endpoint in
Rethlas, so sharing it across the two deployments silently selected the wrong
corpus.

## Runtime data locations (gitignored, under `runtime/`)

| variable | default | holds |
|---|---|---|
| `DANUS_RUNTIME` | `<repo>/runtime` | the whole self-contained runtime |
| `DANUS_AGENTS_ROOT` | `runtime/projects` | where `danus new` puts projects |
| `DANUS_STATE_DIR` | `$XDG_STATE_HOME/danus`, else `~/.local/state/danus` outside repo wrappers | writable packaged-verifier resources and default run state |
| `VERIFY_AGENT_HOME` | `<DANUS_STATE_DIR>/verify` | optional base for digest-keyed materialized verifier bundles (contract, skills/YAML, CLI schema) |
| `VERIFIER_RESULTS_DIR` | `runtime/verify-runs` | per-verification run logs |
| `DANUS_PY` | `runtime/venv/bin/python` (else system `python3`) | the engine's Python |

## Worker loop pacing (optional; engine defaults are sane)

| variable | default | meaning |
|---|---|---|
| `DANUS_ROUND_HARD_TIMEOUT` | `14400` (4h) | legacy-mode per-round wall-clock cap; each reasoning-first paid turn uses its project-pinned 2700-second cap (this does not claim the whole protected phase finishes in 2700 seconds) |
| `DANUS_MAX_ROUNDS` | `0` (unlimited) | round backstop |
| `DANUS_MAX_CONSEC_FAILURES` | `5` | bail after N consecutive failed rounds |
| `DANUS_ROUND_BEAT` | `5` | seconds between rounds |
| `DANUS_WORKER_TRANSPORT` | context-dependent | when unset, reasoning-first projects use `app-server` and legacy projects use `exec`; an explicit `exec` remains a compatibility opt-out and reports reasoning telemetry unavailable |

`danus new` defaults to `--coordination reasoning-first`, which persists
`max_paid_workers=2` and `phase_timeout_seconds=2700` in `project.json`. A
reasoning-first project without explicit `--roles` uses `max:2,high:5`: the two
`max` workers are the fixed paid root/critic lanes and the five `high` workers
are dormant observers. `--active-explorers N`, for `N` equal to 0, 1, or 2,
persists no extra field; it sets `max_paid_workers=2+N` and assigns the next
roster workers stable `explorer1`/`explorer2` lanes. A nonzero request requires a
sufficient roster before creation. Explicit `--roles` is preserved. Explicit
legacy rejects nonzero active explorers and without roles uses
`high:3,xhigh:4`; a project created by an older release without a `coordination`
field remains legacy and is never silently migrated to different paid-turn
semantics. Existing two-lane schema-v7 coordination databases reopen at v7;
new explorer databases use the expanded schema without rewriting those stores.

## Rendering & misc

| variable | default | meaning |
|---|---|---|
| `DANUS_CHROME_BIN` | (auto-detect) | headless Chrome/Chromium for human-summary PDF |
| `TEX_ENGINE` | `pdflatex` | write-paper LaTeX engine (`xelatex`/`lualatex`/`tectonic`) |
| `DANUS_WRITE_PAPER_RUN_LOG` | on | per-call write-paper diagnostic logs (`0` disables) |
| `DANUS_PAPER_VERIFY_WHOLE_DOC_CAP` | `700000` | char budget for one whole-paper math-verify call; over it the tool reports `too_large` (the main agent decomposes — the tool never auto-splits) |

## LaTeX-git push (write-paper deliver, optional)

In `config/latex-git.env` (gitignored): `LATEX_GIT_URL`, `LATEX_GIT_TOKEN`, and
optional `LATEX_GIT_AUTHOR_NAME` / `_EMAIL`. Pushing outward is an operator-gated
action.

---

Ports and the verify HTTP contract are **pinned** cross-module interfaces
(`../ARCHITECTURE.md` §4) — do not renumber `8091`/`8099` without changing both
ends. See `operations.md` to run the services and `cli-and-tools.md` for the
commands that use these.
