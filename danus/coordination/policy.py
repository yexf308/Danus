"""Pure policy for the durable reasoning-first coordinator."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

LEGACY_MODE = "legacy"
REASONING_FIRST_MODE = "reasoning_first_v1"
DEFAULT_MAX_PAID_WORKERS = 2
DEFAULT_PHASE_TIMEOUT_SECONDS = 2700
MAX_PHASE_TIMEOUT_SECONDS = 2700
REASONING_PHASE = "root_critic_reasoning"
CRITIC_REVIEW_PHASE = "critic_obstacle_review"
OWNER_ACTION_REQUIRED_PHASE = "owner_action_required"
MAX_DIRECTIVE_BYTES = 2048
MAX_REVIEW_RECORD_BYTES = 16 * 1024
CANDIDATE_OUTCOMES = frozenset(
    {
        "correct",
        "wrong",
        "needs_context",
        "error",
        "promotion_unknown",
        "outcome_unknown",
    }
)

DEFAULT_COORDINATION: dict[str, object] = {
    "mode": REASONING_FIRST_MODE,
    "max_paid_workers": DEFAULT_MAX_PAID_WORKERS,
    "phase_timeout_seconds": DEFAULT_PHASE_TIMEOUT_SECONDS,
}

_ROLE_PART_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*?):([1-9][0-9]*)$")
_EFFORT_RANK = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
    "ultra": 7,
}


class CoordinationConfigError(ValueError):
    """The project coordination contract is malformed or unsupported."""


@dataclass(frozen=True)
class CoordinationConfig:
    mode: str
    max_paid_workers: int
    phase_timeout_seconds: int

    @property
    def reasoning_first(self) -> bool:
        return self.mode == REASONING_FIRST_MODE


@dataclass(frozen=True)
class LaneRoster:
    root: str
    critic: str | None
    lanes: Mapping[str, str]


def coordination_payload(choice: str | None = None) -> dict[str, object]:
    """Return the exact project.json payload for one CLI coordination choice."""

    if choice in {None, "reasoning-first", REASONING_FIRST_MODE}:
        return dict(DEFAULT_COORDINATION)
    if choice == LEGACY_MODE:
        return {"mode": LEGACY_MODE}
    raise CoordinationConfigError(f"unsupported coordination mode: {choice!r}")


def coordination_config(metadata: Mapping[str, Any]) -> CoordinationConfig:
    """Parse project metadata; a missing field is deliberately legacy."""

    raw = metadata.get("coordination")
    if raw is None:
        return CoordinationConfig(LEGACY_MODE, 0, 0)
    if not isinstance(raw, dict):
        raise CoordinationConfigError("project coordination must be an object")
    mode = raw.get("mode")
    if mode == LEGACY_MODE:
        if set(raw) != {"mode"}:
            raise CoordinationConfigError(
                "legacy coordination accepts only the mode field"
            )
        return CoordinationConfig(LEGACY_MODE, 0, 0)
    if mode != REASONING_FIRST_MODE:
        raise CoordinationConfigError("unsupported project coordination mode")
    expected = {"mode", "max_paid_workers", "phase_timeout_seconds"}
    if set(raw) != expected:
        raise CoordinationConfigError(
            "reasoning-first coordination has an invalid field set"
        )
    max_paid = raw.get("max_paid_workers")
    timeout = raw.get("phase_timeout_seconds")
    if (
        isinstance(max_paid, bool)
        or not isinstance(max_paid, int)
        or not 1 <= max_paid <= DEFAULT_MAX_PAID_WORKERS
    ):
        raise CoordinationConfigError("max_paid_workers must be 1 or 2")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= MAX_PHASE_TIMEOUT_SECONDS
    ):
        raise CoordinationConfigError(
            f"phase_timeout_seconds must be between 1 and {MAX_PHASE_TIMEOUT_SECONDS}"
        )
    return CoordinationConfig(mode, max_paid, timeout)


def _role_pairs(spec: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = _ROLE_PART_RE.fullmatch(part)
        if match is None:
            raise CoordinationConfigError("project roles are malformed")
        effort = match.group(1)
        count = int(match.group(2))
        for index in range(1, count + 1):
            pairs.append((effort if index == 1 else f"{effort}{index}", effort))
    if not pairs:
        raise CoordinationConfigError("project roles are empty")
    return pairs


def select_lane_roster(
    metadata: Mapping[str, Any], config: CoordinationConfig
) -> LaneRoster:
    """Choose root then critic by descending effort and stable roster order."""

    workers = metadata.get("workers")
    roles = metadata.get("roles")
    if (
        not isinstance(workers, list)
        or not workers
        or not all(isinstance(worker, str) and worker for worker in workers)
        or len(set(workers)) != len(workers)
        or not isinstance(roles, str)
    ):
        raise CoordinationConfigError("project roster metadata is malformed")
    effort_by_worker = dict(_role_pairs(roles))
    if set(effort_by_worker) != set(workers):
        raise CoordinationConfigError("project roles and worker roster disagree")
    roster_index = {worker: index for index, worker in enumerate(workers)}
    ordered = sorted(
        workers,
        key=lambda worker: (
            -_EFFORT_RANK.get(effort_by_worker[worker].lower(), -1),
            roster_index[worker],
        ),
    )
    root = ordered[0]
    critic = ordered[1] if config.max_paid_workers > 1 and len(ordered) > 1 else None
    lanes = {root: "root"}
    if critic is not None:
        lanes[critic] = "critic"
    return LaneRoster(root=root, critic=critic, lanes=lanes)


def roster_digest(
    metadata: Mapping[str, Any], config: CoordinationConfig, roster: LaneRoster
) -> str:
    material = {
        "mode": config.mode,
        "max_paid_workers": config.max_paid_workers,
        "phase_timeout_seconds": config.phase_timeout_seconds,
        "roles": metadata.get("roles"),
        "workers": metadata.get("workers"),
        "root": roster.root,
        "critic": roster.critic,
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def coordination_directive(
    *,
    lane: str,
    generation: int,
    phase: str,
    review_id: str | None = None,
    designated_root_entry_id: str | None = None,
) -> str:
    """Return a bounded directive containing no problem or research content."""

    if generation < 1:
        raise CoordinationConfigError("generation must be positive")
    prefix = f"Coordination lane={lane}; generation={generation}; phase={phase}. "
    if phase == CRITIC_REVIEW_PHASE:
        if (
            lane != "critic"
            or not review_id
            or not designated_root_entry_id
            or len(review_id.encode("utf-8")) > 128
            or len(designated_root_entry_id.encode("utf-8")) > 128
        ):
            raise CoordinationConfigError(
                "critic review requires bounded exact review and root entry ids"
            )
        body = (
            f"Protected review_id={review_id}; "
            f"designated_root_entry_id={designated_root_entry_id}. "
            "Use this fresh paid round only to retrieve that exact root obstacle with "
            f"gm_get(entry_id={designated_root_entry_id}); omit project as a worker, "
            "and never substitute gm_search/BM25. If exact lookup fails, is duplicate, "
            "or is oversized, publish no confirmation and terminate inconclusive. "
            "Otherwise stress-test it independently. Confirm it only by publishing one "
            "consolidated obstacle/dead_end whose links.confirms_entry_id is exactly the "
            "designated root entry id. A different id is outside this review and must "
            "not be confirmed."
        )
    elif review_id is not None or designated_root_entry_id is not None:
        raise CoordinationConfigError(
            "review identity is valid only in the critic review phase"
        )
    elif lane == "root":
        body = (
            "Use this paid round for sustained first-principles reasoning on the "
            "assigned task. Prefer one coherent deep route, persist structural "
            "progress, and report an obstacle only when it is concrete."
        )
    elif lane == "critic":
        body = (
            "Independent-first critic directive: derive your own route before "
            "reading or comparing root conclusions, then stress-test the root's "
            "structural claims and confirm only an exact recorded obstacle entry."
        )
    else:
        raise CoordinationConfigError("only root and critic receive paid directives")
    directive = prefix + body
    if len(directive.encode("utf-8")) > MAX_DIRECTIVE_BYTES:
        raise CoordinationConfigError("coordination directive exceeds hard limit")
    return directive


def required_lanes(roster: LaneRoster) -> Sequence[str]:
    return ("root", "critic") if roster.critic is not None else ("root",)


def candidate_outcome_releases(outcome: str) -> bool:
    """Only a fully delivery-unknown verify keeps the overlay frozen."""

    if outcome not in CANDIDATE_OUTCOMES:
        raise CoordinationConfigError("candidate outcome is unsupported")
    return outcome != "outcome_unknown"
