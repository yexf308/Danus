"""Offline concurrency tests for the bounded verifier scheduler."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from danus.verify.scheduler import (
    SchedulerLimits,
    SchedulerRejected,
    SchedulerWorkFailed,
    VerificationScheduler,
)


def _limits(**overrides):
    values = {
        "max_distinct_queue": 4,
        "queue_wait_seconds": 2,
        "max_waiters_per_key": 8,
        "max_total_waiters": 32,
        "cache_max_entries": 64,
        "cache_max_bytes": 1024 * 1024,
        "cache_ttl_seconds": 60,
    }
    values.update(overrides)
    return SchedulerLimits(**values)


def _scheduler(**overrides):
    return VerificationScheduler(
        instance_nonce="test-nonce", limits=_limits(**overrides)
    )


def _wait_for(predicate, *, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("scheduler state did not converge")
        time.sleep(0.005)


def test_exact_inflight_coalescing_launches_once():
    scheduler = _scheduler()
    entered = threading.Event()
    release = threading.Event()
    launches = 0

    def work():
        nonlocal launches
        launches += 1
        entered.set()
        assert release.wait(2)
        return {"answer": "one"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(scheduler.execute, "same", work)
        assert entered.wait(1)
        follower = pool.submit(scheduler.execute, "same", work)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 1)
        release.set()
        receipts = [leader.result(2), follower.result(2)]

    assert launches == 1
    assert {receipt.source for receipt in receipts} == {"launched", "coalesced"}
    assert [receipt.value for receipt in receipts] == [
        {"answer": "one"},
        {"answer": "one"},
    ]


def test_completed_cache_hit_and_exact_key_drift():
    scheduler = _scheduler()
    launches = 0

    def work():
        nonlocal launches
        launches += 1
        return {"launch": launches}

    first = scheduler.execute("key-a", work)
    cached = scheduler.execute("key-a", work)
    drifted = scheduler.execute("key-b", work)

    assert first.source == "launched"
    assert cached.source == "cache_hit"
    assert cached.value == first.value
    assert drifted.source == "launched"
    assert launches == 2


def test_distinct_queue_is_fifo_and_capacity_is_bounded():
    scheduler = _scheduler(max_distinct_queue=2)
    release = threading.Event()
    order = []

    def running():
        order.append("running")
        assert release.wait(2)
        return {"name": "running"}

    def named(name):
        def work():
            order.append(name)
            return {"name": name}

        return work

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(scheduler.execute, "running", running)
        _wait_for(lambda: scheduler.snapshot()["running"] == 1)
        second = pool.submit(scheduler.execute, "second", named("second"))
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 1)
        third = pool.submit(scheduler.execute, "third", named("third"))
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 2)
        with pytest.raises(SchedulerRejected, match="distinct queue is full"):
            scheduler.execute("rejected", named("rejected"))
        release.set()
        first.result(2)
        second.result(2)
        third.result(2)

    assert order == ["running", "second", "third"]


def test_duplicate_joins_even_when_distinct_queue_is_full():
    scheduler = _scheduler(max_distinct_queue=1)
    release = threading.Event()
    launches = 0

    def running():
        nonlocal launches
        launches += 1
        assert release.wait(2)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=3) as pool:
        leader = pool.submit(scheduler.execute, "running", running)
        _wait_for(lambda: scheduler.snapshot()["running"] == 1)
        queued = pool.submit(scheduler.execute, "queued", lambda: {"queued": True})
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 1)
        duplicate = pool.submit(scheduler.execute, "running", running)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 2)
        with pytest.raises(SchedulerRejected) as rejected:
            scheduler.execute("another-distinct", lambda: {"no": True})
        assert rejected.value.reason == "distinct_queue_full"
        release.set()
        assert leader.result(2).source == "launched"
        assert duplicate.result(2).source == "coalesced"
        assert queued.result(2).source == "launched"

    assert launches == 1


def test_failure_fans_out_and_is_not_cached():
    scheduler = _scheduler()
    entered = threading.Event()
    release = threading.Event()
    launches = 0

    def failing():
        nonlocal launches
        launches += 1
        entered.set()
        assert release.wait(2)
        raise ValueError("leader failed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(scheduler.execute, "failure", failing)
        assert entered.wait(1)
        follower = pool.submit(scheduler.execute, "failure", failing)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 1)
        release.set()
        for future in (leader, follower):
            with pytest.raises(SchedulerWorkFailed) as failure:
                future.result(2)
            assert isinstance(failure.value.cause, ValueError)

    retried = scheduler.execute("failure", lambda: {"recovered": True})
    assert retried.source == "launched"
    assert launches == 1


def test_completion_publication_race_never_launches_twice():
    scheduler = _scheduler(max_waiters_per_key=16)
    entered = threading.Event()
    release = threading.Event()
    launches = 0

    def work():
        nonlocal launches
        launches += 1
        entered.set()
        assert release.wait(2)
        return {"stable": True}

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(scheduler.execute, "race", work)]
        assert entered.wait(1)
        futures.extend(pool.submit(scheduler.execute, "race", work) for _ in range(8))
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 8)
        release.set()
        receipts = [future.result(2) for future in futures]
        post_completion = pool.submit(scheduler.execute, "race", work).result(2)

    assert launches == 1
    assert all(receipt.value == {"stable": True} for receipt in receipts)
    assert post_completion.source == "cache_hit"


def test_waiter_caps_and_queue_timeout_fail_before_work():
    scheduler = _scheduler(
        queue_wait_seconds=0.05,
        max_waiters_per_key=1,
        max_total_waiters=2,
    )
    release = threading.Event()
    entered = threading.Event()

    def running():
        entered.set()
        assert release.wait(2)
        return {"running": True}

    with ThreadPoolExecutor(max_workers=4) as pool:
        leader = pool.submit(scheduler.execute, "running", running)
        assert entered.wait(1)
        follower = pool.submit(scheduler.execute, "running", running)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 1)
        with pytest.raises(SchedulerRejected) as per_key:
            scheduler.execute("running", running)
        assert per_key.value.reason == "per_key_waiters_full"
        queued = pool.submit(
            scheduler.execute, "will-timeout", lambda: {"must": "not run"}
        )
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 2)
        with pytest.raises(SchedulerRejected) as total:
            scheduler.execute("total-cap", lambda: {"must": "not run"})
        assert total.value.reason == "total_waiters_full"
        with pytest.raises(SchedulerRejected) as timeout:
            queued.result(1)
        assert timeout.value.reason == "queue_wait_timeout"
        release.set()
        leader.result(2)
        follower.result(2)


def test_cache_lru_ttl_and_byte_bounds():
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    scheduler = VerificationScheduler(
        instance_nonce="nonce",
        limits=_limits(
            cache_max_entries=2,
            cache_max_bytes=64,
            cache_ttl_seconds=10,
        ),
        clock=clock,
    )
    scheduler.execute("one", lambda: {"v": "1"})
    scheduler.execute("two", lambda: {"v": "2"})
    assert scheduler.execute("one", lambda: {"bad": True}).source == "cache_hit"
    scheduler.execute("three", lambda: {"v": "3"})
    assert scheduler.execute("two", lambda: {"relaunched": True}).source == "launched"
    snapshot = scheduler.snapshot()
    assert snapshot["cache_entries"] <= 2
    assert snapshot["cache_bytes"] <= 64

    clock.now = 11
    expired = scheduler.execute("one", lambda: {"after": "ttl"})
    assert expired.source == "launched"
    assert scheduler.snapshot()["counters"]["expired"] >= 1


def test_single_result_larger_than_cache_budget_is_not_cached():
    scheduler = _scheduler(cache_max_bytes=16)
    launches = 0

    def work():
        nonlocal launches
        launches += 1
        return {"payload": "x" * 64}

    assert scheduler.execute("large", work).source == "launched"
    assert scheduler.execute("large", work).source == "launched"
    assert launches == 2
    assert scheduler.snapshot()["cache_entries"] == 0


def test_queue_wait_limit_rejects_platform_timeout_overflow():
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        _limits(queue_wait_seconds=threading.TIMEOUT_MAX * 2)


def test_queued_wait_overflow_cleans_exact_flight_and_preserves_fifo(
    monkeypatch: pytest.MonkeyPatch,
):
    scheduler = _scheduler()
    running_entered = threading.Event()
    release_running = threading.Event()
    order: list[str] = []
    abandoned_work_called = False

    def running():
        order.append("running")
        running_entered.set()
        assert release_running.wait(2)
        return {"name": "running"}

    def abandoned():
        nonlocal abandoned_work_called
        abandoned_work_called = True
        return {"name": "abandoned"}

    def named(name):
        def work():
            order.append(name)
            return {"name": name}

        return work

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(scheduler.execute, "running", running)
        assert running_entered.wait(1)
        original_wait = scheduler._condition.wait

        def overflow(*_args, **_kwargs):
            raise OverflowError("injected Condition.wait overflow")

        monkeypatch.setattr(scheduler._condition, "wait", overflow)
        with pytest.raises(OverflowError, match="injected Condition.wait overflow"):
            scheduler.execute("abandoned", abandoned)

        assert abandoned_work_called is False
        after_overflow = scheduler.snapshot()
        assert after_overflow["running"] == 1
        assert after_overflow["distinct_queue_depth"] == 0
        assert after_overflow["active_keys"] == 1
        assert after_overflow["waiting_clients"] == 0

        monkeypatch.setattr(scheduler._condition, "wait", original_wait)
        second = pool.submit(scheduler.execute, "second", named("second"))
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 1)
        third = pool.submit(scheduler.execute, "third", named("third"))
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 2)
        release_running.set()
        assert first.result(2).source == "launched"
        assert second.result(2).value == {"name": "second"}
        assert third.result(2).value == {"name": "third"}

    assert order == ["running", "second", "third"]
    final = scheduler.snapshot()
    assert final["running"] == 0
    assert final["distinct_queue_depth"] == 0
    assert final["active_keys"] == 0
    assert final["waiting_clients"] == 0


def test_coalesced_wait_cancellation_releases_only_follower_accounting(
    monkeypatch: pytest.MonkeyPatch,
):
    class InjectedCancellation(BaseException):
        pass

    scheduler = _scheduler()
    running_entered = threading.Event()
    release_running = threading.Event()
    launches = 0

    def running():
        nonlocal launches
        launches += 1
        running_entered.set()
        assert release_running.wait(2)
        return {"name": "running"}

    with ThreadPoolExecutor(max_workers=3) as pool:
        leader = pool.submit(scheduler.execute, "running", running)
        assert running_entered.wait(1)
        original_wait = scheduler._condition.wait

        def cancel(*_args, **_kwargs):
            raise InjectedCancellation()

        monkeypatch.setattr(scheduler._condition, "wait", cancel)
        with pytest.raises(InjectedCancellation):
            scheduler.execute("running", running)

        interrupted = scheduler.snapshot()
        assert interrupted["running"] == 1
        assert interrupted["active_keys"] == 1
        assert interrupted["waiting_clients"] == 0

        monkeypatch.setattr(scheduler._condition, "wait", original_wait)
        follower = pool.submit(scheduler.execute, "running", running)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 1)
        queued = pool.submit(
            scheduler.execute, "after-running", lambda: {"name": "after-running"}
        )
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 1)
        release_running.set()
        assert leader.result(2).source == "launched"
        assert follower.result(2).source == "coalesced"
        assert queued.result(2).source == "launched"

    assert launches == 1
    final = scheduler.snapshot()
    assert final["running"] == 0
    assert final["distinct_queue_depth"] == 0
    assert final["active_keys"] == 0
    assert final["waiting_clients"] == 0


def test_duplicate_of_queued_flight_coalesces_and_observes_same_timeout():
    scheduler = _scheduler(queue_wait_seconds=0.05)
    running_entered = threading.Event()
    release_running = threading.Event()
    queued_work_called = False

    def running():
        running_entered.set()
        assert release_running.wait(2)
        return {"name": "running"}

    def queued():
        nonlocal queued_work_called
        queued_work_called = True
        return {"name": "queued"}

    with ThreadPoolExecutor(max_workers=3) as pool:
        active = pool.submit(scheduler.execute, "active", running)
        assert running_entered.wait(1)
        queued_leader = pool.submit(scheduler.execute, "queued", queued)
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 1)
        queued_duplicate = pool.submit(scheduler.execute, "queued", queued)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 2)

        with pytest.raises(SchedulerRejected) as leader_timeout:
            queued_leader.result(1)
        assert leader_timeout.value.reason == "queue_wait_timeout"
        with pytest.raises(SchedulerWorkFailed) as duplicate_timeout:
            queued_duplicate.result(1)
        assert duplicate_timeout.value.source == "coalesced"
        assert isinstance(duplicate_timeout.value.cause, SchedulerRejected)
        assert duplicate_timeout.value.cause.reason == "queue_wait_timeout"
        assert queued_work_called is False

        release_running.set()
        assert active.result(2).source == "launched"

    final = scheduler.snapshot()
    assert final["running"] == 0
    assert final["distinct_queue_depth"] == 0
    assert final["active_keys"] == 0
    assert final["waiting_clients"] == 0


@pytest.mark.parametrize(
    "failing_work",
    [
        pytest.param(
            lambda: (_ for _ in ()).throw(RuntimeError("leader failed")),
            id="work-failure",
        ),
        pytest.param(
            lambda: {"not-json": {"a-set"}},
            id="serialization-failure",
        ),
    ],
)
def test_failed_leader_releases_slot_to_fifo_successors(failing_work):
    scheduler = _scheduler()
    leader_entered = threading.Event()
    release_leader = threading.Event()
    order: list[str] = []

    def gated_failure():
        order.append("leader")
        leader_entered.set()
        assert release_leader.wait(2)
        return failing_work()

    def named(name):
        def work():
            order.append(name)
            return {"name": name}

        return work

    with ThreadPoolExecutor(max_workers=3) as pool:
        leader = pool.submit(scheduler.execute, "leader", gated_failure)
        assert leader_entered.wait(1)
        first_successor = pool.submit(scheduler.execute, "first", named("first"))
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 1)
        second_successor = pool.submit(scheduler.execute, "second", named("second"))
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 2)
        release_leader.set()

        with pytest.raises(SchedulerWorkFailed):
            leader.result(2)
        assert first_successor.result(2).value == {"name": "first"}
        assert second_successor.result(2).value == {"name": "second"}

    assert order == ["leader", "first", "second"]
    final = scheduler.snapshot()
    assert final["running"] == 0
    assert final["distinct_queue_depth"] == 0
    assert final["active_keys"] == 0
    assert final["waiting_clients"] == 0


def test_injected_service_death_fans_out_and_preserves_queued_fifo():
    class InjectedServiceDeath(BaseException):
        pass

    scheduler = _scheduler()
    running_entered = threading.Event()
    release_running = threading.Event()
    order: list[str] = []
    launches = {"running": 0, "queued": 0}

    def dying_service():
        launches["running"] += 1
        order.append("running")
        running_entered.set()
        assert release_running.wait(2)
        raise InjectedServiceDeath("verifier service disappeared")

    def queued_successor():
        launches["queued"] += 1
        order.append("queued")
        return {"recovered": True}

    with ThreadPoolExecutor(max_workers=4) as pool:
        leader = pool.submit(scheduler.execute, "running", dying_service)
        assert running_entered.wait(1)
        running_duplicate = pool.submit(scheduler.execute, "running", dying_service)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 1)
        queued = pool.submit(scheduler.execute, "queued", queued_successor)
        _wait_for(lambda: scheduler.snapshot()["distinct_queue_depth"] == 1)
        queued_duplicate = pool.submit(scheduler.execute, "queued", queued_successor)
        _wait_for(lambda: scheduler.snapshot()["waiting_clients"] == 3)
        release_running.set()

        for future, source in (
            (leader, "launched"),
            (running_duplicate, "coalesced"),
        ):
            with pytest.raises(SchedulerWorkFailed) as failure:
                future.result(2)
            assert failure.value.source == source
            assert isinstance(failure.value.cause, InjectedServiceDeath)
        queued_receipts = [queued.result(2), queued_duplicate.result(2)]

    assert launches == {"running": 1, "queued": 1}
    assert order == ["running", "queued"]
    assert {receipt.source for receipt in queued_receipts} == {
        "launched",
        "coalesced",
    }
    assert all(receipt.value == {"recovered": True} for receipt in queued_receipts)
    final = scheduler.snapshot()
    assert final["running"] == 0
    assert final["distinct_queue_depth"] == 0
    assert final["active_keys"] == 0
    assert final["waiting_clients"] == 0
