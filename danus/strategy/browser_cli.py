"""Owner CLI for the durable ChatGPT Pro browser-advisor handoff.

No command in this module opens or controls a browser.  The external owner skill
performs UI actions and records its observations through these exact-CAS verbs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .browser_advisor import BrowserAdvisorBroker, BrowserAdvisorError


def _text_source(parser: argparse.ArgumentParser, *, label: str) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(f"--{label}-file", dest=f"{label}_file")
    group.add_argument(f"--{label}-stdin", dest=f"{label}_stdin", action="store_true")


def _outside_project_source(args: argparse.Namespace, value: str) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_file():
        raise ValueError("owner source must be a regular file")
    project = Path(args.project).resolve(strict=True)
    if path == project or project in path.parents:
        raise ValueError(
            "raw browser response/URL source must be outside the Danus project; "
            "prefer stdin for no-plaintext-at-rest handling"
        )
    return path


def _read_source(
    args: argparse.Namespace, label: str, *, outside_project: bool = False
) -> str:
    if getattr(args, f"{label}_stdin"):
        stream = getattr(sys.stdin, "buffer", None)
        return stream.read().decode("utf-8") if stream is not None else sys.stdin.read()
    value = getattr(args, f"{label}_file")
    path = _outside_project_source(args, value) if outside_project else Path(value)
    return path.read_bytes().decode("utf-8")


def _url_source(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--conversation-url-file",
        help="read the visible ChatGPT URL from a local file (recommended)",
    )
    group.add_argument(
        "--conversation-url-stdin",
        action="store_true",
        help="read the visible ChatGPT URL from stdin",
    )


def _predecessor_url_source(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--predecessor-conversation-url-file",
        help=(
            "read the predecessor ChatGPT URL from an owner-controlled file; "
            "required for a local continuation"
        ),
    )
    group.add_argument(
        "--predecessor-conversation-url-stdin",
        action="store_true",
        help=(
            "read the predecessor ChatGPT URL from stdin; required for a "
            "local continuation"
        ),
    )


def _read_predecessor_url(args: argparse.Namespace) -> Optional[str]:
    if getattr(args, "predecessor_conversation_url_stdin", False):
        if getattr(args, "prompt_stdin", False):
            raise ValueError(
                "prompt and predecessor conversation URL cannot both consume stdin; "
                "use a file for one"
            )
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read().decode("utf-8") if stream is not None else sys.stdin.read()
        return raw.strip()
    path = getattr(args, "predecessor_conversation_url_file", None)
    if path is not None:
        return _outside_project_source(args, path).read_bytes().decode("utf-8").strip()
    return None


def _read_url(args: argparse.Namespace) -> str:
    if getattr(args, "conversation_url_stdin", False):
        if getattr(args, "response_stdin", False):
            raise ValueError(
                "response and conversation URL cannot both consume stdin; use a file for one"
            )
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read().decode("utf-8") if stream is not None else sys.stdin.read()
        return raw.strip()
    path = getattr(args, "conversation_url_file", None)
    if path is not None:
        return _outside_project_source(args, path).read_bytes().decode("utf-8").strip()
    raise ValueError("conversation URL source is missing")


def _request_parser(
    sub: argparse._SubParsersAction, name: str
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name)
    parser.add_argument("--project", required=True)
    parser.add_argument("--request-id", required=True)
    return parser


def _completion_args(parser: argparse.ArgumentParser) -> None:
    _text_source(parser, label="response")
    parser.add_argument("--observed-prompt-sha256", required=True)
    parser.add_argument("--ui-mode", required=True)
    _url_source(parser)
    parser.add_argument("--stable-snapshots", required=True, type=int)
    parser.add_argument("--completion-actions-observed", action="store_true")
    parser.add_argument("--composer-available", action="store_true")
    parser.add_argument("--working-indicator-absent", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="consult-browser",
        description=(
            "Durable owner handoff for ChatGPT Pro browser advising. "
            "This command never launches or controls a browser."
        ),
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--project", required=True)
    _text_source(prepare, label="prompt")
    prepare.add_argument("--elaboration-id")
    prepare.add_argument("--client-id")
    prepare.add_argument("--context-id", required=True)
    prepare.add_argument(
        "--recommendation-id",
        help=(
            "exact current coordinator recommendation; required for "
            "reasoning-first projects"
        ),
    )
    prepare.add_argument(
        "--checkpoint-id",
        help="exact immutable advisor_checkpoint global-memory id",
    )
    prepare.add_argument(
        "--checkpoint-sha256",
        help="SHA-256 of the strict canonical immutable checkpoint record",
    )
    prepare.add_argument(
        "--checkpoint-bytes",
        type=int,
        help="exact byte length of the strict canonical checkpoint record",
    )
    prepare.add_argument("--predecessor-request-id")
    _predecessor_url_source(prepare)

    authorize = _request_parser(sub, "authorize")
    authorize.add_argument("--prompt-sha256", required=True)
    authorize.add_argument("--scope", required=True)
    authorize.add_argument("--acknowledge-external-transmission", action="store_true")

    dispatch_started = _request_parser(sub, "dispatch-started")
    _predecessor_url_source(dispatch_started)

    submitted = _request_parser(sub, "submitted")
    submitted.add_argument("--observed-prompt-sha256", required=True)
    submitted.add_argument("--ui-mode", required=True)
    _url_source(submitted)
    submitted.add_argument("--full-prompt-observed", action="store_true")

    complete = _request_parser(sub, "complete")
    _completion_args(complete)

    needs_input = _request_parser(sub, "needs-input")
    _completion_args(needs_input)

    import_result = _request_parser(sub, "import")
    _text_source(import_result, label="response")

    adopt = _request_parser(sub, "adopt")
    _text_source(adopt, label="strategy")
    adopt.add_argument("--acknowledge-untrusted-review", action="store_true")

    recover = _request_parser(sub, "recover")
    recover.add_argument("--observation", choices=["unknown"], required=True)
    recover.add_argument("--reason", default="")

    failed = _request_parser(sub, "fail-not-submitted")
    failed.add_argument("--reason", required=True)
    failed.add_argument("--before-click-evidence", required=True)
    failed.add_argument("--acknowledge-no-submit-action", action="store_true")
    failed.add_argument("--pre-click-token")

    abandon = _request_parser(sub, "abandon")
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--acknowledge-delivery-unknown", action="store_true")

    status = _request_parser(sub, "status")
    status.add_argument("--include-prompt", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    return_code = 0
    try:
        broker = BrowserAdvisorBroker(args.project)
        if args.verb == "prepare":
            result = broker.prepare(
                _read_source(args, "prompt"),
                elaboration_id=args.elaboration_id,
                client_id=args.client_id,
                context_id=args.context_id,
                recommendation_id=args.recommendation_id,
                checkpoint_id=args.checkpoint_id,
                checkpoint_sha256=args.checkpoint_sha256,
                checkpoint_bytes=args.checkpoint_bytes,
                predecessor_request_id=args.predecessor_request_id,
                predecessor_conversation_url=_read_predecessor_url(args),
            )
        elif args.verb == "authorize":
            result = broker.authorize(
                args.request_id,
                prompt_sha256=args.prompt_sha256,
                authorization_scope=args.scope,
                acknowledge_external_transmission=args.acknowledge_external_transmission,
            )
        elif args.verb == "dispatch-started":
            result = broker.dispatch_started(
                args.request_id,
                predecessor_conversation_url=_read_predecessor_url(args),
            )
            if not result.get("transitioned"):
                # A replay after a lost acknowledgement must never be mistaken
                # for fresh permission to click Send.
                return_code = 3
        elif args.verb == "submitted":
            result = broker.submitted(
                args.request_id,
                observed_prompt_sha256=args.observed_prompt_sha256,
                ui_mode=args.ui_mode,
                full_prompt_observed=args.full_prompt_observed,
                conversation_url=_read_url(args),
            )
        elif args.verb in {"complete", "needs-input"}:
            kwargs = {
                "response": _read_source(args, "response", outside_project=True),
                "observed_prompt_sha256": args.observed_prompt_sha256,
                "ui_mode": args.ui_mode,
                "conversation_url": _read_url(args),
                "stable_snapshots": args.stable_snapshots,
                "completion_actions_observed": args.completion_actions_observed,
                "composer_available": args.composer_available,
                "working_indicator_absent": args.working_indicator_absent,
            }
            method = broker.complete if args.verb == "complete" else broker.needs_input
            result = method(args.request_id, **kwargs)
        elif args.verb == "import":
            result = broker.import_result(
                args.request_id,
                response=_read_source(args, "response", outside_project=True),
            )
        elif args.verb == "adopt":
            result = broker.adopt(
                args.request_id,
                strategy=_read_source(args, "strategy"),
                acknowledge_untrusted_review=args.acknowledge_untrusted_review,
            )
        elif args.verb == "recover":
            result = broker.recover(
                args.request_id,
                observation=args.observation,
                reason=args.reason,
            )
        elif args.verb == "fail-not-submitted":
            result = broker.fail_not_submitted(
                args.request_id,
                reason=args.reason,
                before_click_evidence=args.before_click_evidence,
                acknowledge_no_submit_action=args.acknowledge_no_submit_action,
                pre_click_token=args.pre_click_token,
            )
        elif args.verb == "abandon":
            result = broker.abandon(
                args.request_id,
                reason=args.reason,
                acknowledge_delivery_unknown=args.acknowledge_delivery_unknown,
            )
        else:
            result = broker.get(args.request_id, include_prompt=args.include_prompt)
    except (BrowserAdvisorError, OSError, UnicodeError, ValueError) as exc:
        print(f"consult-browser: {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return return_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
