"""Bounded in-process scheduling for the single paid verifier slot.

The scheduler is deliberately synchronous.  FastAPI runs ``/verify`` in its
thread pool, and the thread that owns a distinct request keeps ownership until
the verifier process group is terminal.  Exact duplicates join that flight;
they never allocate a result directory or launch another verifier.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Mapping, Optional


SCHEDULER_SNAPSHOT_SCHEMA_VERSION = 1
SCHEDULER_KEY_SCHEMA = "danus.verify.scheduler-key.v1"


def canonical_sha256(value: Mapping[str, Any]) -> str:
    """Hash one exact, strict-JSON scheduler-key envelope."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SchedulerLimits:
    max_distinct_queue: int = 4
    queue_wait_seconds: float = 1800
    max_waiters_per_key: int = 8
    max_total_waiters: int = 32
    cache_max_entries: int = 64
    cache_max_bytes: int = 16 * 1024 * 1024
    cache_ttl_seconds: float = 3600

    def __post_init__(self) -> None:
        for name in (
            "max_distinct_queue",
            "max_waiters_per_key",
            "max_total_waiters",
            "cache_max_entries",
            "cache_max_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("queue_wait_seconds", "cache_ttl_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive")
        if self.queue_wait_seconds > threading.TIMEOUT_MAX:
            raise ValueError("queue_wait_seconds must not exceed threading.TIMEOUT_MAX")


class SchedulerRejected(RuntimeError):
    """A bounded admission or queue-wait rejection before paid work."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class SchedulerWorkFailed(RuntimeError):
    """A leader failure annotated for the leader or a coalesced follower."""

    def __init__(
        self,
        *,
        cause: BaseException,
        source: Literal["launched", "coalesced"],
        key: str,
        wait_ms: int,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.source = source
        self.key = key
        self.wait_ms = wait_ms


@dataclass(frozen=True)
class SchedulerReceipt:
    value: Dict[str, Any]
    source: Literal["launched", "coalesced", "cache_hit"]
    key: str
    wait_ms: int


@dataclass
class _Flight:
    key: str
    state: Literal["queued", "running", "completed", "failed"]
    joined_waiters: int = 0
    result: Optional[Dict[str, Any]] = None
    error: Optional[BaseException] = None


@dataclass(frozen=True)
class _CacheEntry:
    encoded: bytes
    expires_at: float

    @property
    def size(self) -> int:
        return len(self.encoded)


@dataclass
class _Counters:
    submitted: int = 0
    launched: int = 0
    coalesced: int = 0
    cache_hits: int = 0
    rejected: int = 0
    completed: int = 0
    failed: int = 0
    expired: int = 0
    evicted: int = 0


class VerificationScheduler:
    """One paid slot with FIFO distinct work and exact duplicate coalescing."""

    def __init__(
        self,
        *,
        instance_nonce: str,
        limits: SchedulerLimits,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._instance_nonce = instance_nonce
        self._limits = limits
        self._clock = clock
        self._condition = threading.Condition()
        self._queue: deque[str] = deque()
        self._active: dict[str, _Flight] = {}
        self._running_key: Optional[str] = None
        self._total_waiters = 0
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._counters = _Counters()

    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    def _purge_expired_locked(self, now: float) -> None:
        expired = [key for key, entry in self._cache.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._cache.pop(key)
            self._cache_bytes -= entry.size
            self._counters.expired += 1

    def _cache_result_locked(self, key: str, encoded: bytes, *, now: float) -> None:
        if len(encoded) > self._limits.cache_max_bytes:
            return
        old = self._cache.pop(key, None)
        if old is not None:
            self._cache_bytes -= old.size
        entry = _CacheEntry(
            encoded=encoded,
            expires_at=now + self._limits.cache_ttl_seconds,
        )
        self._cache[key] = entry
        self._cache_bytes += entry.size
        while (
            len(self._cache) > self._limits.cache_max_entries
            or self._cache_bytes > self._limits.cache_max_bytes
        ):
            _evicted_key, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.size
            self._counters.evicted += 1

    @staticmethod
    def _decoded(entry: _CacheEntry) -> Dict[str, Any]:
        value = json.loads(entry.encoded)
        if not isinstance(value, dict):  # defensive; inserts only accept dicts
            raise RuntimeError("scheduler cache entry is not a JSON object")
        return value

    @staticmethod
    def _copy_error(error: BaseException) -> BaseException:
        if isinstance(error, SchedulerRejected):
            return SchedulerRejected(error.reason, error.detail)
        try:
            return copy.copy(error)
        except Exception:
            return RuntimeError(f"scheduled verifier failed: {type(error).__name__}")

    def _reject_locked(self, reason: str, detail: str) -> SchedulerRejected:
        self._counters.rejected += 1
        return SchedulerRejected(reason, detail)

    def execute(
        self,
        key: str,
        work: Callable[[], Dict[str, Any]],
    ) -> SchedulerReceipt:
        """Return a cached/coalesced result or execute one FIFO leader.

        Distinct queue capacity is checked only after the active-flight lookup,
        so an exact duplicate may join even while every distinct queue slot is
        occupied.  A successful leader result is cached in the same critical
        section that removes the active key and wakes followers.  Exceptions
        are fanned out to current followers but never enter the cache.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("scheduler key must be a non-empty string")
        joined_at = self._clock()
        leader = False
        queued_leader_accounted = False
        flight: _Flight

        with self._condition:
            self._counters.submitted += 1
            self._purge_expired_locked(joined_at)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self._counters.cache_hits += 1
                return SchedulerReceipt(
                    value=self._decoded(cached),
                    source="cache_hit",
                    key=key,
                    wait_ms=0,
                )

            flight = self._active.get(key)  # exact in-flight coalescing
            if flight is not None:
                if flight.joined_waiters >= self._limits.max_waiters_per_key:
                    raise self._reject_locked(
                        "per_key_waiters_full",
                        "too many callers are already waiting for this verification",
                    )
                if self._total_waiters >= self._limits.max_total_waiters:
                    raise self._reject_locked(
                        "total_waiters_full",
                        "verification scheduler waiter capacity is full",
                    )
                flight.joined_waiters += 1
                self._total_waiters += 1
                self._counters.coalesced += 1
            else:
                flight = _Flight(key=key, state="queued")
                self._active[key] = flight
                leader = True
                if self._running_key is None and not self._queue:
                    flight.state = "running"
                    self._running_key = key
                    self._counters.launched += 1
                else:
                    if len(self._queue) >= self._limits.max_distinct_queue:
                        del self._active[key]
                        raise self._reject_locked(
                            "distinct_queue_full",
                            "verification scheduler distinct queue is full",
                        )
                    if self._total_waiters >= self._limits.max_total_waiters:
                        del self._active[key]
                        raise self._reject_locked(
                            "total_waiters_full",
                            "verification scheduler waiter capacity is full",
                        )
                    self._queue.append(key)
                    self._total_waiters += 1
                    queued_leader_accounted = True

            if not leader:
                try:
                    while flight.state not in {"completed", "failed"}:
                        self._condition.wait()
                    wait_ms = max(0, int((self._clock() - joined_at) * 1000))
                    if flight.state == "failed":
                        assert flight.error is not None
                        raise SchedulerWorkFailed(
                            cause=self._copy_error(flight.error),
                            source="coalesced",
                            key=key,
                            wait_ms=wait_ms,
                        )
                    assert flight.result is not None
                    return SchedulerReceipt(
                        value=copy.deepcopy(flight.result),
                        source="coalesced",
                        key=key,
                        wait_ms=wait_ms,
                    )
                finally:
                    flight.joined_waiters -= 1
                    self._total_waiters -= 1
                    # A cancelled/interrupted follower owns no flight state, but
                    # releasing its exact waiter slot may unblock admission and
                    # should wake every observer of the bounded counters.
                    self._condition.notify_all()

            if flight.state == "queued":
                try:
                    deadline = joined_at + self._limits.queue_wait_seconds
                    while True:
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            try:
                                self._queue.remove(key)
                            except ValueError:
                                pass
                            if queued_leader_accounted:
                                self._total_waiters -= 1
                                queued_leader_accounted = False
                            error = self._reject_locked(
                                "queue_wait_timeout",
                                "verification scheduler queue wait timed out",
                            )
                            flight.state = "failed"
                            flight.error = error
                            if self._active.get(key) is flight:
                                del self._active[key]
                            self._condition.notify_all()
                            raise error
                        if (
                            self._running_key is None
                            and self._queue
                            and self._queue[0] == key
                        ):
                            popped = self._queue.popleft()
                            assert popped == key
                            if queued_leader_accounted:
                                self._total_waiters -= 1
                                queued_leader_accounted = False
                            flight.state = "running"
                            self._running_key = key
                            self._counters.launched += 1
                            break
                        self._condition.wait(
                            timeout=min(remaining, threading.TIMEOUT_MAX)
                        )
                except BaseException as error:
                    # The queued caller is the sole prospective owner of this
                    # flight.  If its wait is cancelled or raises, retire only
                    # that exact queued flight; never clear a running leader.
                    if flight.state == "queued":
                        try:
                            self._queue.remove(key)
                        except ValueError:
                            pass
                        if queued_leader_accounted:
                            self._total_waiters -= 1
                            queued_leader_accounted = False
                        flight.state = "failed"
                        flight.error = error
                        if self._active.get(key) is flight:
                            del self._active[key]
                        self._condition.notify_all()
                    raise

        queue_wait_ms = max(0, int((self._clock() - joined_at) * 1000))
        try:
            value = work()
            if not isinstance(value, dict):
                raise TypeError("scheduled verifier result must be a dict")
            # Canonicalize before taking the publication lock. This proves the
            # result is strict JSON and gives cache accounting the exact bytes
            # it will store, without a second fallible serialization inside the
            # terminal critical section.
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            published = json.loads(encoded)
            if not isinstance(published, dict):
                raise TypeError("scheduled verifier result must be a JSON object")
        except BaseException as error:
            with self._condition:
                flight.state = "failed"
                flight.error = error
                if self._running_key == key:
                    self._running_key = None
                self._active.pop(key, None)
                self._counters.failed += 1
                self._condition.notify_all()
            raise SchedulerWorkFailed(
                cause=error,
                source="launched",
                key=key,
                wait_ms=queue_wait_ms,
            ) from error

        with self._condition:
            now = self._clock()
            self._purge_expired_locked(now)
            self._cache_result_locked(key, encoded, now=now)
            flight.result = published
            flight.state = "completed"
            if self._running_key == key:
                self._running_key = None
            self._active.pop(key, None)
            self._counters.completed += 1
            # Cache publication, terminal flight state, active-key removal, and
            # the wakeup are one Condition critical section.
            self._condition.notify_all()
        return SchedulerReceipt(
            value=copy.deepcopy(published),
            source="launched",
            key=key,
            wait_ms=queue_wait_ms,
        )

    def snapshot(self) -> Dict[str, Any]:
        """Return counters and bounded sizes without exposing request keys."""
        with self._condition:
            self._purge_expired_locked(self._clock())
            return {
                "schema_version": SCHEDULER_SNAPSHOT_SCHEMA_VERSION,
                "instance_nonce": self._instance_nonce,
                "paid_concurrency_limit": 1,
                "running": int(self._running_key is not None),
                "distinct_queue_depth": len(self._queue),
                "active_keys": len(self._active),
                "waiting_clients": self._total_waiters,
                "cache_entries": len(self._cache),
                "cache_bytes": self._cache_bytes,
                "limits": {
                    "max_distinct_queue": self._limits.max_distinct_queue,
                    "queue_wait_seconds": self._limits.queue_wait_seconds,
                    "max_waiters_per_key": self._limits.max_waiters_per_key,
                    "max_total_waiters": self._limits.max_total_waiters,
                    "cache_max_entries": self._limits.cache_max_entries,
                    "cache_max_bytes": self._limits.cache_max_bytes,
                    "cache_ttl_seconds": self._limits.cache_ttl_seconds,
                },
                "counters": {
                    "submitted": self._counters.submitted,
                    "launched": self._counters.launched,
                    "coalesced": self._counters.coalesced,
                    "cache_hits": self._counters.cache_hits,
                    "rejected": self._counters.rejected,
                    "completed": self._counters.completed,
                    "failed": self._counters.failed,
                    "expired": self._counters.expired,
                    "evicted": self._counters.evicted,
                },
            }
