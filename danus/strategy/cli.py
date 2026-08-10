"""consult CLI — the stateless strategic-consult gateway.

Reads an elaboration (``--file`` / ``--stdin``), sends it to a strong model via
the chosen transport, and prints the pinned JSON envelope on stdout. The caller
records ``reply`` verbatim as ``master_guidance`` and dispatches workers from it.

Entry point: ``python -m danus.strategy`` (bin/consult wraps this). Exit 0 on
success; non-zero on empty prompt / missing key / ``off``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

from .config import (
    CONSULT_TRANSPORTS,
    load_claude_api_config,
    load_claude_code_config,
    load_config,
    resolve_transport,
)
from .ledger import log_spend_summary
from .browser_advisor import BrowserAdvisorBroker, BrowserAdvisorError
from .transport import (
    ClaudeApiTransport, ClaudeCodeTransport, GptProTransport, OffTransport,
)


EFFORT_CHOICES = ("minimal", "low", "medium", "high", "xhigh", "max")


def _failure_envelope(transport: str, model: Optional[str], effort: str,
                      exc: BaseException) -> Dict[str, Any]:
    """The pinned envelope for a consult that could not run — every attempt was
    rejected (a strongest-effort request has no effort-dropping fallback, by
    design) or the transport raised. Callers always get one JSON envelope, never a
    traceback; ``status="failed"`` plus ``error`` says what went wrong."""
    billing_basis = (
        "subscription_estimate" if transport == "claude_code" else "metered_api"
    )
    return {
        "transport": transport,
        "model": model,
        "effort": effort,
        "attempt": "failed",
        "status": "failed",
        "seconds": 0.0,
        "usage": {"input": 0, "output": 0, "reasoning": None},
        "cost_usd": 0.0,
        "billing_basis": billing_basis,
        "tool_calls": [],
        "reasoning_summary": "",
        "reply": "",
        "error": f"{type(exc).__name__}: {exc}",
    }


def _claude_available(binary: str) -> bool:
    """True if the ``claude`` CLI is invokable (on PATH, or an executable path)."""
    if shutil.which(binary):
        return True
    return os.path.isfile(binary) and os.access(binary, os.X_OK)


def _write_out(path: str, res: Dict[str, Any]) -> None:
    """Human-readable markdown dump (reasoning summary + reply). A failed consult
    writes its ``error`` instead of an empty reply — this file is what an operator
    reads (the example strategy loop points ``--out`` at it), so it must never be
    silently blank."""
    usage = res.get("usage") or {}
    if res.get("status") == "failed":
        Path(path).write_text(
            f"# consult FAILED ({res.get('transport')}, effort={res.get('effort')})\n\n"
            f"The consult could not run; nothing was recorded as master_guidance.\n\n"
            f"## error\n\n{res.get('error', '(none reported)')}\n",
            encoding="utf-8")
        return
    md = (
        f"# consult ({res.get('model')}, effort={res.get('effort')}, "
        f"transport={res.get('transport')}, {res.get('status')})\n\n"
        f"- time: {res.get('seconds')}s · tools: {res.get('tool_calls') or 'none'}\n"
        f"- tokens: in {usage.get('input')} / out {usage.get('output')} "
        f"(reasoning {usage.get('reasoning')}) · cost "
        f"{('$' + str(res.get('cost_usd'))) if res.get('cost_usd') is not None else 'unpriced'}\n\n"
        f"## reasoning summary\n\n{res.get('reasoning_summary') or '_(none)_'}\n\n"
        f"## reply (record this as master_guidance)\n\n{res.get('reply', '')}\n"
    )
    Path(path).write_text(md, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="consult",
        description="Optionally consult a configured strong model (default "
        "transport: off); emit reply+cost as one JSON line.",
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="read the elaboration / prompt from this file")
    src.add_argument("--stdin", action="store_true", help="read the prompt from stdin")
    ap.add_argument(
        "--effort",
        choices=EFFORT_CHOICES,
        default="high",
        help="reasoning effort (default high; max = strongest supported level)",
    )
    ap.add_argument("--tools", choices=["auto", "web", "none"], default="auto",
                    help="tool set for the richest attempts (auto = web_search + code_interpreter)")
    ap.add_argument("--project", help="project dir — append a spend record to "
                    "<project>/spend/consult.jsonl and report project_total_usd")
    ap.add_argument("--out", help="also write the full reply+summary as markdown here")
    ap.add_argument("--max-output-tokens", type=int, default=100000,
                    help="output-token cap; 0 = no explicit cap (gpt_pro omits the "
                    "parameter entirely, for a gateway that rejects it; claude_api "
                    "sends its ceiling)")
    ap.add_argument("--model", default=None,
                    help="override the consult model (api: any OpenAI-compatible id; "
                    "claude_api/claude_code: any Claude model, e.g. claude-fable-5 / claude-opus-4-8)")
    ap.add_argument("--transport", choices=CONSULT_TRANSPORTS, default=None,
                    help="off (the default), gpt_pro (paid OpenAI-compatible), claude_api "
                    "(paid Anthropic API, BYO key), claude_code (your Claude "
                    "subscription via the Claude Code CLI), chatgpt_pro_browser "
                    "(owner-mediated durable UI handoff; this process never opens "
                    "a browser); "
                    "falls back to $DANUS_CONSULT_TRANSPORT then off")
    ap.add_argument("--elaboration-id", help="provenance id for browser-advisor prepare")
    ap.add_argument("--browser-client-id", help="idempotency key for browser prepare")
    ap.add_argument(
        "--browser-context-id",
        help="stable conversation-lineage binding for explicit browser prepare",
    )
    ap.add_argument(
        "--browser-recommendation-id",
        help=(
            "exact current coordinator recommendation; required for a "
            "reasoning-first browser prepare"
        ),
    )
    ap.add_argument(
        "--browser-checkpoint-id",
        help="exact immutable advisor_checkpoint global-memory id",
    )
    ap.add_argument(
        "--browser-checkpoint-sha256",
        help="SHA-256 of the strict canonical immutable checkpoint record",
    )
    ap.add_argument(
        "--browser-checkpoint-bytes",
        type=int,
        help="exact byte length of the strict canonical checkpoint record",
    )
    ap.add_argument(
        "--owner-browser-prepare",
        action="store_true",
        help=(
            "explicit owner-only permission to create a durable browser request; "
            "the env transport alone never creates one"
        ),
    )
    ap.add_argument("--background", choices=["on", "off"], default=None,
                    help="(gpt_pro) send background=true; override for a gateway that "
                    "rejects it (default: DANUS_CONSULT_BACKGROUND, else on)")
    ap.add_argument("--store", choices=["on", "off"], default=None,
                    help="(gpt_pro) send store=true; override for a gateway that requires "
                    "stored responses (default: DANUS_CONSULT_STORE, else off)")
    ap.add_argument("--quiet", action="store_true", help="suppress the stderr heartbeat")
    return ap


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)

    prompt = sys.stdin.read() if args.stdin else Path(args.file).read_text(encoding="utf-8")
    if not prompt.strip():
        print("refusing to consult on an empty prompt", file=sys.stderr, flush=True)
        return 2

    try:
        transport_name = resolve_transport(args.transport)
    except ValueError as exc:
        print(f"consult: {exc}", file=sys.stderr, flush=True)
        return 2

    if transport_name == "chatgpt_pro_browser":
        if args.transport != "chatgpt_pro_browser" or not args.owner_browser_prepare:
            print(
                "chatgpt_pro_browser never prepares from environment/unattended "
                "selection; invoke it explicitly with --transport "
                "chatgpt_pro_browser --owner-browser-prepare, or use "
                "consult-browser prepare",
                file=sys.stderr,
                flush=True,
            )
            return 2
        if not args.project:
            print(
                "chatgpt_pro_browser requires --project for its durable owner receipt",
                file=sys.stderr,
                flush=True,
            )
            return 2
        if not args.browser_context_id:
            print(
                "chatgpt_pro_browser requires --browser-context-id to bind this "
                "exact strategic cycle",
                file=sys.stderr,
                flush=True,
            )
            return 2
        if args.out:
            print(
                "chatgpt_pro_browser has no reply yet; --out is valid only after "
                "`consult-browser import`",
                file=sys.stderr,
                flush=True,
            )
            return 2
        try:
            prepared = BrowserAdvisorBroker(args.project).prepare(
                prompt,
                elaboration_id=args.elaboration_id,
                client_id=args.browser_client_id,
                context_id=args.browser_context_id,
                recommendation_id=args.browser_recommendation_id,
                checkpoint_id=args.browser_checkpoint_id,
                checkpoint_sha256=args.browser_checkpoint_sha256,
                checkpoint_bytes=args.browser_checkpoint_bytes,
            )
        except (BrowserAdvisorError, OSError, ValueError) as exc:
            print(f"consult: browser-advisor prepare failed: {exc}", file=sys.stderr)
            return 2
        prepared["status"] = "interactive_action_required"
        prepared["next_command"] = (
            "consult-browser authorize --project <project> --request-id "
            f"{prepared['request_id']} --prompt-sha256 {prepared['prompt_sha256']} "
            "--scope <exact-authorized-scope> --acknowledge-external-transmission"
        )
        print(json.dumps(prepared, ensure_ascii=False, allow_nan=False))
        print(
            "[consult] owner browser action required; no browser/model was started",
            file=sys.stderr,
            flush=True,
        )
        return 4

    if transport_name == "off":
        res = OffTransport().consult(
            prompt, effort=args.effort, tools=args.tools,
            max_output_tokens=args.max_output_tokens,
        )
        if args.project:
            res.update(log_spend_summary(args.project, res))  # records the $0 event
        if args.out:
            _write_out(args.out, res)
        print(json.dumps(res, ensure_ascii=False))
        print("[consult] transport=off (disabled); returning empty reply", file=sys.stderr, flush=True)
        return 1

    if transport_name == "claude_code":
        # Claude Code CLI via `claude -p` (subscription auth). On failure we do NOT fall
        # back to the paid gpt_pro — the caller (main agent) reasons on its own.
        cfg = load_claude_code_config()
        model = args.model or cfg.model
        if not _claude_available(cfg.claude_bin):
            print(f"claude CLI not found at '{cfg.claude_bin}' (set DANUS_CONSULT_CLAUDE_CODE_BIN, "
                  "or use --transport off)", file=sys.stderr, flush=True)
            return 3
        try:
            res = ClaudeCodeTransport(model, claude_bin=cfg.claude_bin, max_wall=cfg.max_wall,
                                  price_in=cfg.price_in, price_out=cfg.price_out).consult(
                prompt, effort=args.effort, tools=args.tools,
                max_output_tokens=args.max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — an envelope, never a traceback
            res = _failure_envelope("claude_code", model, args.effort, exc)
        if res.get("status") != "completed":
            print(f"[consult] WARNING status={res.get('status')} (claude_code transport did not "
                  "complete; main agent should reason on its own)", file=sys.stderr, flush=True)
        if args.project:
            res.update(log_spend_summary(args.project, res))
        if args.out:
            _write_out(args.out, res)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("status") == "completed" else 1

    if transport_name == "claude_api":
        # Native Anthropic API (per-token, BYO key). On failure we do NOT fall
        # back to gpt_pro — the caller (main agent) reasons on its own.
        acfg = load_claude_api_config()
        if args.model:
            acfg = replace(acfg, model=args.model)
        if not acfg.has_key:
            print("Anthropic consult key not set (set DANUS_CONSULT_CLAUDE_API_KEY "
                  "or ANTHROPIC_API_KEY, or use --transport off)",
                  file=sys.stderr, flush=True)
            return 3

        def _ahb(elapsed: float, status: Optional[str], n: int) -> None:
            print(f"[consult {elapsed:.0f}s status={status} events={n}]",
                  file=sys.stderr, flush=True)

        try:
            res = ClaudeApiTransport(acfg).consult(
                prompt, effort=args.effort, tools=args.tools,
                max_output_tokens=args.max_output_tokens,
                on_progress=None if args.quiet else _ahb,
            )
        except Exception as exc:  # noqa: BLE001 — an envelope, never a traceback
            res = _failure_envelope("claude_api", acfg.model, args.effort, exc)
        if res.get("status") != "completed":
            print(f"[consult] WARNING status={res.get('status')} (claude_api transport "
                  "did not complete; main agent should reason on its own)",
                  file=sys.stderr, flush=True)
        if args.project:
            res.update(log_spend_summary(args.project, res))
        if args.out:
            _write_out(args.out, res)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("status") == "completed" else 1

    config = load_config()
    if args.model:
        config = replace(config, model=args.model)
    # per-call overrides of the transport params: a rejected one is fixed on the
    # NEXT call by the caller (an agent reading the error), no config edit needed.
    if args.background is not None:
        config = replace(config, background=args.background == "on")
    if args.store is not None:
        config = replace(config, store=args.store == "on")
    if not config.has_key:
        print("consult API key not set (set DANUS_CONSULT_API_KEY, "
              "or use --transport off)", file=sys.stderr, flush=True)
        return 3

    def _hb(elapsed: float, status: Optional[str], n: int) -> None:
        print(f"[consult {elapsed:.0f}s status={status} events={n}]", file=sys.stderr, flush=True)

    try:
        res = GptProTransport(config).consult(
            prompt, effort=args.effort, tools=args.tools,
            max_output_tokens=args.max_output_tokens,
            on_progress=None if args.quiet else _hb,
        )
    except Exception as exc:  # noqa: BLE001 — an envelope, never a traceback
        res = _failure_envelope("gpt_pro", config.model, args.effort, exc)
    if res.get("status") and res["status"] != "completed":
        print(f"[consult] WARNING status={res['status']} (not completed)", file=sys.stderr, flush=True)
    if args.project:
        res.update(log_spend_summary(args.project, res))
    if args.out:
        _write_out(args.out, res)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("status") == "completed" and bool(res.get("reply")) else 1
