# OPERATOR.md — durable operator profile & standing preferences

> Read by the main agent after the deployment initialization marker has been
> checked. The initialize skill copies these blank fields into the deployment's
> `OPERATOR.md`, asks the current operator, and fills them in. No secrets belong
> here or in `OPERATOR.md`; tokens and keys go only in gitignored `config/*.env`.

## Operator
- **Name / how to address:** _(ask once; fill in)_
- **Language:** _(the language the main agent replies in; code/comments/skills stay English)_
- **Timezone:** _(for scheduling summaries/consults)_

## Standing preferences
- **Notifications:** _(how/where to reach them; what severity warrants a ping)_
- **Spend ceiling (paid consult API):** _(USD; warn before crossing)_
- **Optional consult transport:** _(`off` by default / `gpt_pro` - paid API, BYO key / `claude_api` - Anthropic API, per-token BYO key / `claude_code` - your Claude subscription; `chatgpt_pro_browser` is available only after an exact coordinator recommendation and per-question owner authorization)_
- **worker roster:** _(reasoning-first default `max:2,high:5` - two paid root/critic lanes plus five dormant observers; explicit legacy default `high:3,xhigh:4`; override per project with `danus new --roles`)_

## Per-project pointers
_(One line per live project → where its durable facts live. The project's own
problem lives under `runtime/projects/<project>/PROBLEM.md`, not here.)_

## Notes
_(Anything else durable the operator told you: conventions, do/don't, contacts.)_
