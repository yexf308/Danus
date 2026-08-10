"""Content-free app-server telemetry for reasoning-first Danus turns.

The app-server exposes item lifecycle timestamps and cumulative token usage.  This
module reduces those notifications to bounded counts and interval unions.  It
never retains prompts, reasoning text, command arguments, tool arguments, or
tool results, and telemetry degradation is deliberately non-causal for the
worker's mathematical outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


REASONING_BANDWIDTH_SCHEMA = "danus_reasoning_bandwidth_v1"
MAX_TELEMETRY_ITEM_ID_BYTES = 512
MAX_TRACKED_ITEMS_PER_TURN = 16_384

_SAFE_ITEM_TYPES = frozenset(
    {
        "agentMessage",
        "collabAgentToolCall",
        "commandExecution",
        "contextCompaction",
        "dynamicToolCall",
        "enteredReviewMode",
        "exitedReviewMode",
        "fileChange",
        "hookPrompt",
        "imageGeneration",
        "imageView",
        "mcpToolCall",
        "plan",
        "reasoning",
        "sleep",
        "subAgentActivity",
        "userMessage",
        "webSearch",
    }
)
_SAFE_ITEM_STATUSES = frozenset({"completed", "declined", "failed", "inProgress"})
_DANUS_MCP_SERVERS = frozenset({"danus"})
_DANUS_MCP_CATEGORIES = {
    "fact_context": "fact_retrieval",
    "fact_search": "fact_retrieval",
    "fact_submit": "verification",
    "gm_add": "memory_write",
    "gm_get": "memory_search",
    "gm_search": "memory_search",
    "search_arxiv_theorems": "external_retrieval",
    "verifier_control": "verification_control",
}
_COLLAB_CATEGORIES = {
    "closeAgent": "collab_close",
    "close_agent": "collab_close",
    "resumeAgent": "collab_resume",
    "resume_agent": "collab_resume",
    "sendInput": "collab_send",
    "send_input": "collab_send",
    "spawnAgent": "collab_spawn",
    "spawn_agent": "collab_spawn",
    "wait": "collab_wait",
    "wait_agent": "collab_wait",
}
_ITEM_TYPE_CATEGORIES = {
    "agentMessage": "agent_message",
    "commandExecution": "command_execution",
    "contextCompaction": "context_compaction",
    "dynamicToolCall": "dynamic_tool",
    "enteredReviewMode": "review_mode",
    "exitedReviewMode": "review_mode",
    "fileChange": "file_change",
    "hookPrompt": "hook_prompt",
    "imageGeneration": "image_generation",
    "imageView": "image_view",
    "plan": "plan",
    "reasoning": "reasoning_item",
    "sleep": "sleep_wait",
    "subAgentActivity": "subagent_activity",
    "userMessage": "user_message",
    "webSearch": "external_retrieval",
}
_RESUME_TRIGGER_CATEGORIES = frozenset(
    {
        "collab_close",
        "collab_other",
        "collab_resume",
        "collab_send",
        "collab_spawn",
        "collab_wait",
        "command_execution",
        "context_compaction",
        "dynamic_tool",
        "external_retrieval",
        "fact_retrieval",
        "file_change",
        "image_generation",
        "image_view",
        "mcp_tool_other",
        "memory_search",
        "memory_write",
        "sleep_wait",
        "verification",
        "verification_control",
    }
)
_TOOL_OR_CONTROL_CATEGORIES = _RESUME_TRIGGER_CATEGORIES | frozenset({"review_mode"})
_WAIT_CATEGORIES = frozenset({"collab_wait", "sleep_wait"})
_MEMORY_CATEGORIES = frozenset({"memory_search", "memory_write"})
_MEMORY_WRITE_CATEGORIES = frozenset({"memory_write"})
_RETRIEVAL_CATEGORIES = frozenset(
    {"external_retrieval", "fact_retrieval", "memory_search"}
)


class TelemetryError(ValueError):
    """A diagnostic notification is internally inconsistent."""


def token_usage_cumulative_total_changed(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> bool:
    """Return whether one cumulative notification contains observable growth."""

    current_last = current["last"]
    current_total = current["total"]
    if set(current_last) != set(current_total):
        raise TelemetryError("token usage last/total fields differ")
    if previous is None:
        return any(value > 0 for value in current_last.values())
    previous_last = previous["last"]
    previous_total = previous["total"]
    if set(previous_last) != set(previous_total) or set(previous_total) != set(
        current_total
    ):
        raise TelemetryError("token usage cumulative fields changed")
    if current_total == previous_total:
        if current_last != previous_last:
            raise TelemetryError("duplicate cumulative total changed its last sample")
        return False
    for field_name, total in current_total.items():
        delta = total - previous_total[field_name]
        if delta < 0 or delta != current_last[field_name]:
            raise TelemetryError("token usage cumulative growth is inconsistent")
    return True


def _add_breakdown(
    aggregate: Mapping[str, int] | None, increment: Mapping[str, int]
) -> dict[str, int]:
    if aggregate is None:
        return dict(increment)
    if set(aggregate) != set(increment):
        raise TelemetryError("token usage aggregate fields changed")
    return {name: aggregate[name] + increment[name] for name in aggregate}


def _item_identity(item: object) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    item_type = item.get("type")
    if (
        not isinstance(item_id, str)
        or not item_id
        or len(item_id.encode("utf-8")) > MAX_TELEMETRY_ITEM_ID_BYTES
        or not isinstance(item_type, str)
        or not item_type
    ):
        return None
    return item_id, item_type if item_type in _SAFE_ITEM_TYPES else "other"


def _item_category(item: Mapping[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "mcpToolCall":
        server = item.get("server")
        tool = item.get("tool")
        if server in _DANUS_MCP_SERVERS and isinstance(tool, str):
            return _DANUS_MCP_CATEGORIES.get(tool, "mcp_tool_other")
        return "mcp_tool_other"
    if item_type == "collabAgentToolCall":
        tool = item.get("tool")
        return (
            _COLLAB_CATEGORIES.get(tool, "collab_other")
            if isinstance(tool, str)
            else "collab_other"
        )
    return (
        _ITEM_TYPE_CATEGORIES.get(item_type, "other_item")
        if isinstance(item_type, str)
        else "other_item"
    )


def _item_status(item: Mapping[str, Any]) -> str:
    status = item.get("status")
    return (
        status
        if isinstance(status, str) and status in _SAFE_ITEM_STATUSES
        else "observed"
    )


def _nonnegative_ms(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _interval_union_ms(intervals: Iterable[tuple[int, int]]) -> int:
    ordered = sorted((start, end) for start, end in intervals if 0 <= start <= end)
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


@dataclass
class TurnReasoningBandwidth:
    """Bounded root-thread telemetry for one paid app-server turn."""

    starts: dict[str, tuple[int, str]] = field(default_factory=dict)
    completed_ids: set[str] = field(default_factory=set)
    item_counts: dict[str, int] = field(default_factory=dict)
    operation_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    intervals_by_category: dict[str, list[tuple[int, int]]] = field(
        default_factory=dict
    )
    usage_by_resume_trigger: dict[str, dict[str, int]] = field(default_factory=dict)
    usage_sample_counts_by_resume_trigger: dict[str, int] = field(default_factory=dict)
    pending_resume_categories: set[str] = field(default_factory=set)
    degradation_reasons: set[str] = field(default_factory=set)
    started_count: int = 0
    completed_notification_count: int = 0
    terminal_recovered_count: int = 0
    missing_start_count: int = 0
    duration_fallback_count: int = 0
    duplicate_start_count: int = 0
    duplicate_completion_count: int = 0
    malformed_notification_count: int = 0
    compaction_count: int = 0
    branch_spawn_count: int = 0
    spawned_agent_count: int = 0
    tracking_limit_drop_count: int = 0

    def degrade(self, reason: str) -> None:
        self.degradation_reasons.add(reason)

    def observe_start(self, item: object, started_at_ms: object) -> None:
        identity = _item_identity(item)
        started = _nonnegative_ms(started_at_ms)
        if identity is None or started is None:
            self.malformed_notification_count += 1
            self.degrade("malformed_item_started")
            return
        item_id, item_type = identity
        previous = self.starts.get(item_id)
        if previous is not None:
            self.duplicate_start_count += 1
            if previous != (started, item_type):
                self.degrade("conflicting_item_started")
            return
        if item_id in self.completed_ids:
            self.duplicate_start_count += 1
            self.degrade("item_started_after_completion")
            return
        if len(self.starts) + len(self.completed_ids) >= MAX_TRACKED_ITEMS_PER_TURN:
            self.tracking_limit_drop_count += 1
            self.degrade("item_tracking_limit_reached")
            return
        self.starts[item_id] = (started, item_type)
        self.started_count += 1

    def observe_completion(
        self, item: object, completed_at_ms: object, *, source: str
    ) -> None:
        identity = _item_identity(item)
        if identity is None or not isinstance(item, dict):
            self.malformed_notification_count += int(source == "notification")
            self.degrade("malformed_item_completed")
            return
        item_id, item_type = identity
        if item_id in self.completed_ids:
            if source == "notification":
                self.duplicate_completion_count += 1
            return
        if (
            item_id not in self.starts
            and len(self.starts) + len(self.completed_ids) >= MAX_TRACKED_ITEMS_PER_TURN
        ):
            self.tracking_limit_drop_count += 1
            self.degrade("item_tracking_limit_reached")
            return
        self.completed_ids.add(item_id)
        self.item_counts[item_type] = self.item_counts.get(item_type, 0) + 1
        category = _item_category(item)
        status = _item_status(item)
        if category in _TOOL_OR_CONTROL_CATEGORIES or category in {
            "collab_other",
            "subagent_activity",
        }:
            statuses = self.operation_counts.setdefault(category, {})
            statuses[status] = statuses.get(status, 0) + 1
        if source == "notification":
            self.completed_notification_count += 1
        else:
            self.terminal_recovered_count += 1
            self.degrade("missing_item_completed_notification")

        completed = _nonnegative_ms(completed_at_ms)
        started = self.starts.pop(item_id, None)
        if completed is None:
            if source == "notification":
                self.malformed_notification_count += 1
                self.degrade("malformed_item_completed_timestamp")
            elif started is not None:
                self.degrade("terminal_item_missing_completion_timestamp")
        elif started is not None:
            started_ms, started_type = started
            if started_type != item_type:
                self.degrade("item_type_changed_during_lifecycle")
            if completed < started_ms:
                self.degrade("item_completion_precedes_start")
            else:
                self.intervals_by_category.setdefault(category, []).append(
                    (started_ms, completed)
                )
        else:
            self.missing_start_count += 1
            self.degrade("missing_item_started_notification")
            duration = _nonnegative_ms(item.get("durationMs"))
            if duration is not None and duration <= completed:
                self.intervals_by_category.setdefault(category, []).append(
                    (completed - duration, completed)
                )
                self.duration_fallback_count += 1

        if category == "context_compaction":
            self.compaction_count += 1
        if category == "collab_spawn" and status == "completed":
            self.branch_spawn_count += 1
            receivers = item.get("receiverThreadIds")
            if isinstance(receivers, list):
                valid = {
                    value
                    for value in receivers
                    if isinstance(value, str)
                    and value
                    and len(value.encode("utf-8")) <= MAX_TELEMETRY_ITEM_ID_BYTES
                }
                self.spawned_agent_count += len(valid)
                if len(valid) != len(receivers):
                    self.degrade("invalid_spawn_receiver_identity")
            else:
                self.degrade("missing_spawn_receiver_list")
        if source == "notification" and category in _RESUME_TRIGGER_CATEGORIES:
            self.pending_resume_categories.add(category)

    def observe_usage_growth(self, breakdown: Mapping[str, int]) -> None:
        pending = set(self.pending_resume_categories)
        if not pending:
            trigger = "initial_or_unattributed"
        elif len(pending) == 1:
            trigger = f"observed_after_{next(iter(pending))}"
        else:
            trigger = "observed_after_mixed_or_parallel"
        self.usage_by_resume_trigger[trigger] = _add_breakdown(
            self.usage_by_resume_trigger.get(trigger), breakdown
        )
        self.usage_sample_counts_by_resume_trigger[trigger] = (
            self.usage_sample_counts_by_resume_trigger.get(trigger, 0) + 1
        )
        self.pending_resume_categories.clear()

    def reconcile_terminal_items(self, items: object) -> None:
        if not isinstance(items, list):
            self.degrade("terminal_items_unavailable")
            return
        for item in items:
            identity = _item_identity(item)
            if identity is None:
                self.degrade("malformed_terminal_item")
            elif identity[0] not in self.completed_ids:
                self.observe_completion(item, None, source="terminal")

    def summary(self, turn_duration_ms: object) -> dict[str, Any]:
        reasons = set(self.degradation_reasons)
        if self.starts:
            reasons.add("item_started_without_completion")
        duration = _nonnegative_ms(turn_duration_ms)
        if duration is None:
            reasons.add("turn_duration_unavailable")
        active = {
            category: _interval_union_ms(intervals)
            for category, intervals in sorted(self.intervals_by_category.items())
        }

        def union(categories: Iterable[str]) -> int:
            return _interval_union_ms(
                interval
                for category in categories
                for interval in self.intervals_by_category.get(category, [])
            )

        tool_union = union(_TOOL_OR_CONTROL_CATEGORIES)
        measured_union = union(self.intervals_by_category)
        if duration is not None and (
            tool_union > duration or measured_union > duration
        ):
            reasons.add("item_intervals_exceed_turn_duration")
        sample_counts = dict(sorted(self.usage_sample_counts_by_resume_trigger.items()))
        usage_growth_total: dict[str, int] | None = None
        for breakdown in self.usage_by_resume_trigger.values():
            usage_growth_total = _add_breakdown(usage_growth_total, breakdown)
        output_tokens = (
            usage_growth_total.get("outputTokens", 0)
            if usage_growth_total is not None
            else 0
        )
        reasoning_output_tokens = (
            usage_growth_total.get("reasoningOutputTokens", 0)
            if usage_growth_total is not None
            else 0
        )
        reasoning_union = union({"reasoning_item"})
        return {
            "schema": REASONING_BANDWIDTH_SCHEMA,
            "scope": "root_thread_only",
            "finality": "partial" if reasons else "complete",
            "finality_reasons": sorted(reasons),
            "growth_samples_are_not_schema_attested_inferences": True,
            "item_counts": dict(sorted(self.item_counts.items())),
            "operation_counts": {
                category: dict(sorted(statuses.items()))
                for category, statuses in sorted(self.operation_counts.items())
            },
            "lifecycle": {
                "completed_count": len(self.completed_ids),
                "completed_notification_count": self.completed_notification_count,
                "duplicate_completion_count": self.duplicate_completion_count,
                "duplicate_start_count": self.duplicate_start_count,
                "duration_fallback_count": self.duration_fallback_count,
                "incomplete_started_count": len(self.starts),
                "malformed_notification_count": self.malformed_notification_count,
                "missing_start_count": self.missing_start_count,
                "started_count": self.started_count,
                "terminal_recovered_count": self.terminal_recovered_count,
                "tracking_limit_drop_count": self.tracking_limit_drop_count,
            },
            "active_ms_by_category": active,
            "tool_or_control_union_ms": tool_union,
            "wait_union_ms": union(_WAIT_CATEGORIES),
            "memory_union_ms": union(_MEMORY_CATEGORIES),
            "memory_write_union_ms": union(_MEMORY_WRITE_CATEGORIES),
            "retrieval_union_ms": union(_RETRIEVAL_CATEGORIES),
            "reasoning_item_union_ms": reasoning_union,
            "reasoning_item_wall_share": (
                None if duration in {None, 0} else round(reasoning_union / duration, 6)
            ),
            "measured_item_union_ms": measured_union,
            "non_tool_residual_ms": (
                None
                if duration is None
                else max(0, duration - min(duration, tool_union))
            ),
            "unattributed_residual_ms": (
                None
                if duration is None
                else max(0, duration - min(duration, measured_union))
            ),
            "compaction_count": self.compaction_count,
            "branch_spawn_count": self.branch_spawn_count,
            "spawned_agent_count": self.spawned_agent_count,
            "pending_resume_item_count": len(self.pending_resume_categories),
            "usage_growth_sample_count": sum(sample_counts.values()),
            "usage_growth_sample_counts_by_resume_trigger": sample_counts,
            "usage_growth_tokens_by_resume_trigger": {
                trigger: dict(sorted(values.items()))
                for trigger, values in sorted(self.usage_by_resume_trigger.items())
            },
            "usage_growth_tokens_total": (
                None
                if usage_growth_total is None
                else dict(sorted(usage_growth_total.items()))
            ),
            "observed_reasoning_output_share_of_output": (
                None
                if output_tokens == 0
                else round(reasoning_output_tokens / output_tokens, 6)
            ),
        }
