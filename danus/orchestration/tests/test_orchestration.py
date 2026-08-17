"""Offline tests for danus.orchestration — the ``danus`` CLI verbs.

Filesystem verbs (new/assign/status/list) are deterministic. The loop tests are
integration: they spawn the real ``python -m danus.execution`` loop subprocess but
stub codex with a fake shell binary (``DANUS_CODEX_BIN``) so nothing real is
invoked and no API is spent. All processes are force-cleaned in ``finally``.

Runs standalone (``python -m danus.orchestration.tests.test_orchestration``) and
under pytest.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from danus.coordination import CoordinationStore, candidate_receipt_id
from danus.core import FactGraph
from danus.execution import layout as L
from danus.execution import loop as execution_loop
from danus.orchestration import cli


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _project_env(tmp: Path, **extra):
    """Agents root + stub worker contract/skills so tests never touch the repo's
    agents/ tree; merge any extra env (codex stub, round vars)."""
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    env = {
        "DANUS_AGENTS_ROOT": str(tmp / "agents"),
        "DANUS_WORKER_CONTRACT": str(contract),
        "DANUS_WORKER_SKILLS": str(skills),
        # Integration stubs implement `codex exec`, while production
        # reasoning-first projects deliberately default to app-server.
        "DANUS_WORKER_TRANSPORT": "exec",
    }
    env.update(extra)
    with _env(**env):
        yield


def _fake_codex(d: Path) -> Path:
    """A stub codex: print a round marker, sleep FAKE_CODEX_SLEEP, exit 0."""
    p = d / "fake_codex.sh"
    p.write_text(
        '#!/usr/bin/env bash\necho "fake codex round"\n'
        'sleep "${FAKE_CODEX_SLEEP:-0}"\nexit 0\n'
    )
    p.chmod(0o755)
    return p


def _wait_until(pred, timeout=15.0, interval=0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _st(project: str, worker: str) -> dict:
    return cli.worker_status(L.WorkerLayout(L.worker_dir(project, worker)))


def _pinned_prompt(project: str, admission) -> str:
    return execution_loop.kickoff(
        project,
        admission.worker,
        admission.directive,
        coordination_slot_id=admission.slot_id,
        generation=admission.generation,
        task_sha256=admission.task_sha256,
    )


def _kill_project(project: str):
    try:
        cli.do_stop(project, force=True)
    except SystemExit:
        pass
    for d in L.target_worker_dirs(project):
        pid = cli._read_pid(L.WorkerLayout(d))
        if pid:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass


# --- filesystem verb tests ------------------------------------------------- #


def test_assign_replace_and_rejects(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "explore direction 3: the symplectic-rank route")
        assert (
            L.WorkerLayout(L.worker_dir("P", "high")).task.read_text()
            == "explore direction 3: the symplectic-rank route\n"
        )
        cli.do_assign("P/high", "switch to direction 5")  # replace, not append
        assert (
            L.WorkerLayout(L.worker_dir("P", "high")).task.read_text()
            == "switch to direction 5\n"
        )
        for bad in ["P", "P/nope"]:
            try:
                cli.do_assign(bad, "x")
                assert False, f"should reject {bad!r}"
            except SystemExit:
                pass
        try:
            cli.do_assign("P/high", "   ")
            assert False, "should reject empty task"
        except SystemExit:
            pass


def test_status_before_start(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        s = _st("P", "high")
        assert (
            s["alive"] is False and s["state"] == "created" and s["label"] == "created"
        )
        assert s["live_processes"] == 0 and s["paid_active"] == 0
        assert s["lane"] == "root" and s["generation"] == 1
        assert s["phase"] == "root_critic_reasoning"
        assert s["recommendation"] is None
        assert s["candidate"] is None


def test_list(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:2", model="gpt-5.5")
        cli.do_new("Q", roles="xhigh:1", model="gpt-x")
        rows = {r["project"]: r for r in cli.do_list()}
        assert (
            rows["P"]["workers"] == 2
            and rows["P"]["live"] == 0
            and rows["P"]["model"] == "gpt-5.5"
        )
        assert rows["Q"]["workers"] == 1 and rows["Q"]["model"] == "gpt-x"
        assert rows["P"]["live_processes"] == 0
        assert rows["P"]["paid_active"] == 0
        assert rows["P"]["lane"] == {"root": "high", "critic": "high2"}
        assert rows["P"]["generation"] == 1
        assert rows["P"]["candidate"] is None


def test_explorer_assignment_and_status_distinguish_paid_lanes_from_observers(
    tmp: Path,
):
    with _project_env(tmp):
        cli.do_new("P", active_explorers=2)
        first = cli.do_assign("P/high", "independent alternate route")
        second = cli.do_assign("P/high2", "supporting lemma route")
        observer = cli.do_assign("P/high3", "dormant projection")

        assert first["generation_staged"] is True
        assert second["generation_staged"] is True
        assert "assignment_scope" not in first
        assert "assignment_scope" not in second
        assert observer["generation_staged"] is False
        assert observer["assignment_scope"] == "dormant_observer_projection"

        status = {row["worker"]: row for row in cli.do_status("P")}
        assert status["max"]["lane"] == "root"
        assert status["max2"]["lane"] == "critic"
        assert status["high"]["lane"] == "explorer1"
        assert status["high2"]["lane"] == "explorer2"
        assert status["high3"]["lane"] == "observer"
        assert status["high"]["explorer_workers"] == ["high", "high2"]

        listed = {row["project"]: row for row in cli.do_list()}["P"]
        assert listed["explorer_workers"] == ["high", "high2"]
        assert listed["lane"] == {
            "root": "max",
            "critic": "max2",
            "explorer1": "high",
            "explorer2": "high2",
        }


def test_status_and_list_expose_active_candidate_overlay(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "candidate overlay assignment")
        project = L.project_dir("P")
        store = CoordinationStore(project, create=False)
        root = store.admit("high")
        assert root is not None
        store.pin_prompt(root.slot_id, _pinned_prompt("P", root))
        store.activate(root.slot_id)
        receipt = candidate_receipt_id(
            slot_id=root.slot_id,
            candidate_fact_id="a" * 16,
            candidate_fact_identity="c" * 64,
            source_id=None,
            context_digest="b" * 64,
        )
        candidate = store.register_candidate(
            "high",
            receipt,
            slot_id=root.slot_id,
            candidate_fact_id="a" * 16,
            candidate_fact_identity="c" * 64,
            source_id=None,
            context_digest="b" * 64,
        )

        assert _st("P", "high")["candidate"] == candidate
        listed = {row["project"]: row for row in cli.do_list()}
        assert listed["P"]["candidate"] == candidate
        before = L.WorkerLayout(L.worker_dir("P", "high")).task.read_text()
        try:
            cli.do_assign("P/high", "retask while candidate is active")
            assert False, "active candidate must freeze retask"
        except SystemExit as exc:
            assert "freezes retask" in str(exc)
        assert L.WorkerLayout(L.worker_dir("P", "high")).task.read_text() == before
        try:
            cli.do_resolve_candidate(
                "P",
                receipt,
                outcome="known-no-promotion",
                acknowledge_paid_outcome_unknown=False,
            )
            assert False, "candidate resolution must require paid-risk acknowledgement"
        except SystemExit as exc:
            assert "acknowledge-paid-outcome-unknown" in str(exc)
        try:
            cli.do_resolve_candidate(
                "P",
                receipt,
                outcome="known-no-promotion",
                acknowledge_paid_outcome_unknown=True,
            )
            assert False, "live active source slot must not be owner-resolved"
        except SystemExit as exc:
            assert "source slot is not terminal" in str(exc)
        store.terminalize_candidate(
            "high",
            receipt,
            slot_id=root.slot_id,
            outcome="outcome_unknown",
        )
        try:
            cli.do_resolve_candidate(
                "P",
                receipt,
                outcome="known-no-promotion",
                acknowledge_paid_outcome_unknown=True,
            )
            assert False, "outcome-unknown candidate cannot release a live source slot"
        except SystemExit as exc:
            assert "source slot is not terminal" in str(exc)
        store.mark_ambiguous(root.slot_id)
        try:
            cli.do_resolve_candidate(
                "P",
                receipt,
                outcome="known-no-promotion",
                acknowledge_paid_outcome_unknown=True,
            )
            assert False, "outcome-unknown candidate cannot release an ambiguous slot"
        except SystemExit as exc:
            assert "source slot is not terminal" in str(exc)
        store.complete(root.slot_id, outcome="terminal_rc_126")
        resolved = cli.do_resolve_candidate(
            "P",
            receipt,
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=True,
        )
        assert resolved["owner_resolution"] == "known_no_promotion"
        assert resolved["candidate_fact_active_at_resolution"] is False
        assert _st("P", "high")["candidate"] is None
        cli.do_assign("P/high", "retask after explicit owner resolution")


def test_owner_resolution_checks_exact_full_fact_identity(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "candidate identity assignment")
        project = L.project_dir("P")
        store = CoordinationStore(project, create=False)
        root = store.admit("high")
        assert root is not None
        store.pin_prompt(root.slot_id, _pinned_prompt("P", root))
        store.activate(root.slot_id)
        graph = FactGraph(project)
        fact_id = graph.add(
            problem_id="P",
            author="high",
            statement="A durably promoted candidate",
            proof="A complete proof.",
        )
        with graph.locked_active_fact_identity(fact_id) as fact_identity:
            assert isinstance(fact_identity, str)
        receipt = candidate_receipt_id(
            slot_id=root.slot_id,
            candidate_fact_id=fact_id,
            candidate_fact_identity=fact_identity,
            source_id=None,
            context_digest="b" * 64,
        )
        store.register_candidate(
            "high",
            receipt,
            slot_id=root.slot_id,
            candidate_fact_id=fact_id,
            candidate_fact_identity=fact_identity,
            source_id=None,
            context_digest="b" * 64,
        )
        store.complete(root.slot_id, outcome="terminal_rc_126")

        try:
            cli.do_resolve_candidate(
                "P",
                receipt,
                outcome="known-no-promotion",
                acknowledge_paid_outcome_unknown=True,
            )
            assert False, "known-no-promotion must reject the exact active identity"
        except SystemExit as exc:
            assert "active candidate fact" in str(exc)
        resolved = cli.do_resolve_candidate(
            "P",
            receipt,
            outcome="abandon-unknown",
            acknowledge_paid_outcome_unknown=True,
        )
        assert resolved["candidate_fact_active_at_resolution"] is True


def test_owner_resolution_ignores_active_short_id_collision(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "candidate collision assignment")
        project = L.project_dir("P")
        store = CoordinationStore(project, create=False)
        root = store.admit("high")
        assert root is not None
        store.pin_prompt(root.slot_id, _pinned_prompt("P", root))
        store.activate(root.slot_id)
        graph = FactGraph(project)
        fact_id = graph.add(
            problem_id="P",
            author="high",
            statement="An unrelated fact sharing a forced short id",
            proof="A complete unrelated proof.",
        )
        with graph.locked_active_fact_identity(fact_id) as active_identity:
            assert isinstance(active_identity, str)
        colliding_identity = (
            "0" if active_identity[0] != "0" else "1"
        ) + active_identity[1:]
        receipt = candidate_receipt_id(
            slot_id=root.slot_id,
            candidate_fact_id=fact_id,
            candidate_fact_identity=colliding_identity,
            source_id=None,
            context_digest="c" * 64,
        )
        store.register_candidate(
            "high",
            receipt,
            slot_id=root.slot_id,
            candidate_fact_id=fact_id,
            candidate_fact_identity=colliding_identity,
            source_id=None,
            context_digest="c" * 64,
        )
        store.complete(root.slot_id, outcome="terminal_rc_126")

        resolved = cli.do_resolve_candidate(
            "P",
            receipt,
            outcome="known-no-promotion",
            acknowledge_paid_outcome_unknown=True,
        )
        assert resolved["candidate_fact_active_at_resolution"] is False


# --- loop integration tests (stubbed codex) -------------------------------- #


def test_loop_runs_rounds_then_exits(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(
        tmp,
        DANUS_CODEX_BIN=str(fc),
        DANUS_ROUND_BEAT="0",
        DANUS_MAX_ROUNDS="2",
        FAKE_CODEX_SLEEP="0",
    ):
        # This test exercises the historical exec/max-rounds loop. Unset
        # reasoning-first transport intentionally defaults to app-server and is
        # covered by execution/tests/test_loop.py.
        cli.do_new("P", roles="high:1", coordination="legacy")
        try:
            res = cli.do_start("P/high")
            assert res[0]["result"] == "started"
            # PID publication is asynchronous. First observe that the worker
            # actually left its scaffolded state; otherwise an immediate
            # ``not alive`` read can pass before the child registers itself.
            assert _wait_until(
                lambda: _st("P", "high")["state"] != "created"
                or _st("P", "high")["round"] > 0
            ), "loop should publish a launched round or terminal state"
            assert _wait_until(lambda: not _st("P", "high")["alive"]), (
                "loop should exit at backstop"
            )
            s = _st("P", "high")
            assert s["state"] == "max_rounds" and s["round"] == 2
            wl = L.WorkerLayout(L.worker_dir("P", "high"))
            assert (wl.logs / "round_1.log").exists() and (
                wl.logs / "round_2.log"
            ).exists()
        finally:
            _kill_project("P")


def test_graceful_stop(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(
        tmp,
        DANUS_CODEX_BIN=str(fc),
        DANUS_ROUND_BEAT="0.1",
        DANUS_MAX_ROUNDS="0",
        FAKE_CODEX_SLEEP="0.1",
    ):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "Run the graceful-stop integration round.")
        try:
            cli.do_start("P/high")
            assert _wait_until(lambda: _st("P", "high")["round"] >= 1), (
                "should start a round"
            )
            assert _st("P", "high")["alive"] is True
            r = cli.do_stop("P/high")  # graceful
            assert "graceful" in r[0]["result"]
            assert _wait_until(lambda: not _st("P", "high")["alive"]), (
                "loop should exit after .stop"
            )
            assert (
                cli._read_pid(L.WorkerLayout(L.worker_dir("P", "high"))) is None
            )  # pid cleaned
        finally:
            _kill_project("P")


def test_force_stop(tmp: Path, monkeypatch):
    fc = _fake_codex(tmp)
    with _project_env(
        tmp,
        DANUS_CODEX_BIN=str(fc),
        DANUS_ROUND_BEAT="0",
        DANUS_MAX_ROUNDS="0",
        FAKE_CODEX_SLEEP="30",
    ):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "Run the force-stop integration round.")
        wl = L.WorkerLayout(L.worker_dir("P", "high"))
        external_signals = []
        real_kill = os.kill
        real_killpg = os.killpg

        def audited_kill(pid, sig):
            if sig != 0:
                external_signals.append(("pid", pid, sig))
            return real_kill(pid, sig)

        def audited_killpg(pgid, sig):
            external_signals.append(("pgid", pgid, sig))
            return real_killpg(pgid, sig)

        try:
            cli.do_start("P/high")
            assert _wait_until(lambda: _st("P", "high")["state"] == "running"), (
                "round should run"
            )
            # The CLI may authenticate liveness with signal 0. It must never
            # send TERM/KILL to a numeric PID/PGID; only the worker owns
            # retained child handles and performs cooperative cleanup.
            with monkeypatch.context() as stop_patch:
                stop_patch.setattr(os, "kill", audited_kill)
                stop_patch.setattr(os, "killpg", audited_killpg)
                r = cli.do_stop("P/high", force=True)
            assert r[0]["result"] == "stopping (cooperative force)"
            assert _wait_until(lambda: not _st("P", "high")["alive"], timeout=8), (
                "worker should promptly honor the durable force request"
            )
            assert _st("P", "high")["state"] == "stopped"
            assert cli._read_pid(wl) is None
            assert external_signals == []
        finally:
            _kill_project("P")


def test_idempotent_start(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(
        tmp,
        DANUS_CODEX_BIN=str(fc),
        DANUS_ROUND_BEAT="0",
        DANUS_MAX_ROUNDS="0",
        FAKE_CODEX_SLEEP="30",
    ):
        cli.do_new("P", roles="high:1")
        try:
            assert cli.do_start("P/high")[0]["result"] == "started"
            assert _wait_until(lambda: _st("P", "high")["alive"])
            assert cli.do_start("P/high")[0]["result"] == "already-running"
        finally:
            _kill_project("P")


def test_project_wide_targets(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(
        tmp,
        DANUS_CODEX_BIN=str(fc),
        DANUS_ROUND_BEAT="0",
        DANUS_MAX_ROUNDS="1",
        FAKE_CODEX_SLEEP="0",
    ):
        cli.do_new("P", roles="high:2")
        try:
            res = cli.do_start("P")  # whole project
            assert {r["worker"] for r in res} == {"high", "high2"}
            assert _wait_until(
                lambda: all(not _st("P", w)["alive"] for w in ("high", "high2"))
            )
            assert len(cli.do_status("P")) == 2
        finally:
            _kill_project("P")


def test_missing_codex_returns_error_state(tmp: Path):
    with _project_env(
        tmp,
        DANUS_CODEX_BIN="/nonexistent/codex-bin",
        DANUS_ROUND_BEAT="0",
        DANUS_MAX_ROUNDS="0",
    ):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "Exercise the missing-codex error path.")
        try:
            cli.do_start("P/high")
            # rc 127 => loop must not spin; it errors out immediately
            assert _wait_until(lambda: not _st("P", "high")["alive"]), (
                "loop should exit on missing codex"
            )
            s = _st("P", "high")
            assert s["state"] == "error"
        finally:
            _kill_project("P")


# --- runner ---------------------------------------------------------------- #


def main() -> None:
    fs_tests = [test_assign_replace_and_rejects, test_status_before_start, test_list]
    loop_tests = [
        test_loop_runs_rounds_then_exits,
        test_graceful_stop,
        test_force_stop,
        test_idempotent_start,
        test_project_wide_targets,
        test_missing_codex_returns_error_state,
    ]
    for t in fs_tests + loop_tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL ORCHESTRATION TESTS PASSED")


if __name__ == "__main__":
    main()
