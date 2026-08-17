# OPERATOR.md — durable operator profile & standing preferences

> Read by the main agent (codex) at the start of every session — it is NOT
> auto-loaded, so `AGENTS.md` tells the agent to read it. It is the main agent's
> **long-term memory of the operator** —
> the things it learns by asking and must not forget when the session ends. Keep it
> short, factual, current; update in place (no duplicates). **No secrets here**
> (tokens/keys go to `config/*.env`, gitignored). This file is committed.
>
> On a fresh deployment this is the blank template — the `initialize` skill fills it.

## Operator
- **Name / how to address:** Felix
- **Language:** Chinese; code, comments, skills, and commits stay English
- **Timezone:** America/New_York

## Standing preferences
- **Notifications:** Report material progress, blockers, failures, and authorization forks in the current Codex conversation.
- **Spend ceiling (paid consult API):** Not applicable while consult transport is `off`; ask before enabling a paid consult transport.
- **Optional consult transport:** `off`; do not use `gpt_pro`, `claude_api`, `claude_code`, or `chatgpt_pro_browser` without separate authorization.
- **worker roster:** Reasoning-first default `max:2,high:5`; use an explicit smaller override only for a bounded validation run authorized by Felix.

## Per-project pointers
_(One line per live project → where its durable facts live. The project's own
problem lives under `runtime/projects/<project>/PROBLEM.md`, not here.)_

## Notes
- Validate the Codex main-agent integration first with a minimal real E2E run.
- After the minimal gate passes, run a separate substantive reasoning-first validation; do not treat the minimal smoke as the final real-world test.
