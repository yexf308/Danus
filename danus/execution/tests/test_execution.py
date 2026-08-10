"""Offline tests for danus.execution — layout + scaffolding (no codex, no network).

Covers the pure-function layer: role parsing, the typed WorkerLayout, and what
``do_new`` writes (dirs + AGENTS.md/skills symlinks + .codex/config.toml content +
.role/TASK/.status). The loop's stop-condition behavior (which spawns the loop
subprocess against a stubbed codex) is exercised in the orchestration test suite.

Runs standalone (``python -m danus.execution.tests.test_execution``) and pytest.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

from danus.coordination import DEFAULT_COORDINATION, CoordinationStore
from danus.execution import layout as L
from danus.execution import loop, scaffold


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
def _project_env(tmp: Path):
    """Point the agents root + worker contract/skills at tmp stubs so the tests
    are self-contained (no dependency on the repo's agents/ tree existing)."""
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    with _env(
        DANUS_AGENTS_ROOT=str(tmp / "agents"),
        DANUS_WORKER_CONTRACT=str(contract),
        DANUS_WORKER_SKILLS=str(skills),
    ):
        yield


# --- parse_roles ----------------------------------------------------------- #


def test_parse_roles_default_roster():
    pairs = L.parse_roles("high:3,xhigh:4")
    names = [n for n, _ in pairs]
    assert names == ["high", "high2", "high3", "xhigh", "xhigh2", "xhigh3", "xhigh4"]
    # base role (digits stripped) drives reasoning effort
    assert [b for _, b in pairs] == ["high"] * 3 + ["xhigh"] * 4
    assert dict(pairs)["high2"] == "high" and dict(pairs)["xhigh4"] == "xhigh"


def test_parse_roles_rejects_bad_specs():
    for bad in ["", "   ", "high:0", "high", "high:abc", ":3", "3:high"]:
        try:
            L.parse_roles(bad)
            assert False, f"should reject {bad!r}"
        except ValueError:
            pass


# --- WorkerLayout ---------------------------------------------------------- #


def test_worker_layout_paths():
    wl = L.WorkerLayout(Path("/x/proj/workers/high"))
    assert wl.name == "high" and wl.project == "proj"
    assert wl.project_dir == Path("/x/proj")
    assert wl.task.name == L.TASK_FILE and wl.role.name == L.ROLE_FILE
    assert wl.pid.name == L.PID_FILE and wl.lock.name == L.LOCK_FILE
    assert wl.stop.name == L.STOP_FILE and wl.status.name == L.STATUS_FILE
    assert wl.logs.name == L.LOGS_DIR
    assert wl.codex_config == Path("/x/proj/workers/high/.codex/config.toml")


def test_resolve_and_target():
    assert L.resolve_target("proj") == ("proj", None)
    assert L.resolve_target("proj/high") == ("proj", "high")
    assert L.resolve_target("/proj/high/") == ("proj", "high")
    assert L.resolve_target("proj-1.2/high_3") == ("proj-1.2", "high_3")
    for bad in [
        "",
        ".",
        "..",
        "../high",
        "proj/.",
        "proj/..",
        r"proj\high",
        "proj//high",
        "proj/high/extra",
        "//proj/high/",
        "/proj/high//",
        ".proj/high",
        "proj/-high",
    ]:
        try:
            L.resolve_target(bad)
            assert False, f"should reject unsafe target {bad!r}"
        except ValueError:
            pass


# --- do_new scaffolding ---------------------------------------------------- #


def test_do_new_scaffolds_project(tmp: Path):
    with _project_env(tmp):
        r = scaffold.do_new("P", roles="high:2,xhigh:1", model="gpt-5.5")
        assert r["workers"] == ["high", "high2", "xhigh"]
        pdir = L.project_dir("P")
        assert (pdir / "global_memory").is_dir() and (pdir / "fact_graph").is_dir()
        meta = json.loads((pdir / "project.json").read_text())
        assert (
            meta["workers"] == ["high", "high2", "xhigh"] and meta["model"] == "gpt-5.5"
        )
        assert meta["coordination"] == DEFAULT_COORDINATION
        coordination = CoordinationStore(pdir, meta, create=False).project_status()
        assert coordination["root_worker"] == "xhigh"
        assert coordination["critic_worker"] == "high"
        assert coordination["paid_active"] == 0

        for w, eff in [("high", "high"), ("high2", "high"), ("xhigh", "xhigh")]:
            wl = L.WorkerLayout(L.worker_dir("P", w))
            assert wl.local_memory.is_dir() and wl.logs.is_dir()
            # symlinks resolve to the (stub) contract + skills
            assert (wl.dir / "AGENTS.md").resolve() == L.worker_md().resolve()
            assert (
                wl.dir / ".agents" / "skills"
            ).resolve() == L.worker_skills_dir().resolve()
            cfg = wl.codex_config.read_text()
            parsed = tomllib.loads(cfg)
            assert Path(sys.executable).is_absolute()
            assert parsed == {
                "mcp_servers": {
                    "danus": {
                        "command": sys.executable,
                        "args": ["-I", "-B", "-m", "danus.gateway"],
                        "default_tools_approval_mode": "approve",
                        "required": True,
                        "tool_timeout_sec": 3600,
                        "env": {
                            "DANUS_PROJECT_DIR": str(pdir),
                            "DANUS_AUTHOR": w,
                            "DANUS_ROLE": "worker",
                            "DANUS_HOTJOIN_ENABLED": "1",
                            "DANUS_HOTJOIN_TARGET": w,
                            "DANUS_VERIFY_URL": "http://127.0.0.1:8091/verify",
                        },
                    }
                }
            }
            role = wl.role.read_text()
            assert f"REASONING_EFFORT={eff}" in role and "MODEL=gpt-5.5" in role
            assert "(unassigned" in wl.task.read_text()
            assert json.loads(wl.status.read_text())["state"] == "created"


def test_do_new_reasoning_first_default_pins_max_paid_lanes_and_high_observers(
    tmp: Path,
):
    with _project_env(tmp):
        result = scaffold.do_new("reasoning-default")
        project = L.project_dir("reasoning-default")
        metadata = json.loads((project / "project.json").read_text())
        assert metadata["roles"] == scaffold.DEFAULT_REASONING_FIRST_ROLES
        assert result["workers"] == [
            "max",
            "max2",
            "high",
            "high2",
            "high3",
            "high4",
            "high5",
        ]
        store = CoordinationStore.open_existing(project, metadata)
        assert store is not None
        status = store.project_status()
        assert status["root_worker"] == "max"
        assert status["critic_worker"] == "max2"
        assert store.admit("high") is None
        assert store.project_status("high")["lane"] == "observer"
        assert store.project_status()["paid_active"] == 0
        for worker in ("max", "max2"):
            role = L.WorkerLayout(L.worker_dir("reasoning-default", worker)).role.read_text()
            assert "REASONING_EFFORT=max" in role


def test_do_new_refuses_existing(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        try:
            scaffold.do_new("P")
            assert False, "should refuse an existing project dir"
        except SystemExit:
            pass


def test_do_new_explicit_legacy_writes_mode_without_coordination_database(
    tmp: Path,
):
    with _project_env(tmp):
        scaffold.do_new("legacy", roles="high:1", coordination="legacy")
        project = L.project_dir("legacy")
        metadata = json.loads((project / "project.json").read_text())
        assert metadata["coordination"] == {"mode": "legacy"}
        assert not (project / ".coordination").exists()


def test_do_new_legacy_default_preserves_historical_roster(tmp: Path):
    with _project_env(tmp):
        result = scaffold.do_new("legacy-default", coordination="legacy")
        project = L.project_dir("legacy-default")
        metadata = json.loads((project / "project.json").read_text())
        assert metadata["roles"] == scaffold.DEFAULT_LEGACY_ROLES
        assert result["workers"] == [
            "high",
            "high2",
            "high3",
            "xhigh",
            "xhigh2",
            "xhigh3",
            "xhigh4",
        ]
        assert not (project / ".coordination").exists()


def test_do_new_rejects_project_traversal_before_filesystem_mutation(tmp: Path):
    root = tmp / "projects"
    outside = tmp / "escaped"
    with _env(DANUS_AGENTS_ROOT=str(root)):
        for bad in ("../escaped", "a/b", "/absolute", ".", ".."):
            try:
                scaffold.do_new(bad, roles="high:1")
                assert False, f"should reject unsafe project name {bad!r}"
            except SystemExit as exc:
                assert "invalid project name" in str(exc)
    assert not root.exists()
    assert not outside.exists()


def test_do_new_verify_url_from_env(tmp: Path):
    with _project_env(tmp):
        with _env(DANUS_VERIFY_URL="http://127.0.0.1:9999/verify"):
            scaffold.do_new("Q", roles="high:1")
        cfg = L.WorkerLayout(L.worker_dir("Q", "high")).codex_config.read_text()
        assert 'DANUS_VERIFY_URL = "http://127.0.0.1:9999/verify"' in cfg


# --- loop helpers (pure) --------------------------------------------------- #


def test_parse_last_fact_id(tmp: Path):
    log = tmp / "round.log"

    def completed(item: dict) -> str:
        return json.dumps({"type": "item.completed", "item": item}) + "\n"

    # Context results, fact-file output, agent prose, and a verified-but-not-
    # promoted submission are all non-publications.
    log.write_text(
        completed(
            {
                "server": "danus",
                "tool": "fact_context",
                "result": {"facts": [{"fact_id": "0123456789abcdef"}]},
            }
        )
        + "fact_id: fedcba9876543210\n"
        + json.dumps(
            {
                "event": "item_completed",
                "item": {
                    "type": "agentMessage",
                    "text": '{"fact_id": "1111111111111111"}',
                },
            }
        )
        + "\n"
        + completed(
            {
                "server": "danus",
                "tool": "fact_submit",
                "result": {
                    "structuredContent": {
                        "accepted": True,
                        "promoted": False,
                        "submission_status": "verified_not_promoted",
                        "verification_verdict": "correct",
                        "fact_id": None,
                        "untrusted_nested_tool_payload": {
                            "accepted": True,
                            "promoted": True,
                            "submission_status": "promoted",
                            "verification_verdict": "correct",
                            "fact_id": "aaaaaaaaaaaaaaaa",
                        },
                    }
                },
            }
        )
        + completed(
            {
                "server": "danus",
                "tool": "fact_submit",
                "result": {
                    "structuredContent": {
                        "accepted": True,
                        "promoted": None,
                        "submission_status": "promotion_unknown",
                        "verification_verdict": "correct",
                        "fact_id": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert loop._parse_last_fact_id(log) is None
    unknown_summary = loop._fact_submit_summary(
        {
            "server": "danus",
            "tool": "fact_submit",
            "result": {
                "structuredContent": {
                    "accepted": True,
                    "promoted": None,
                    "submission_status": "promotion_unknown",
                    "verification_verdict": "correct",
                    "fact_id": None,
                }
            },
        }
    )
    assert unknown_summary is not None
    assert unknown_summary["promoted"] is None
    assert unknown_summary["submission_status"] == "promotion_unknown"

    # A typed current response is a promotion only when its explicit boolean and
    # valid id agree. A later failed submit does not erase the last promotion.
    log.write_text(
        completed(
            {
                "server": "danus",
                "tool": "fact_submit",
                "result": {
                    "structuredContent": {
                        "accepted": True,
                        "promoted": True,
                        "submission_status": "promoted",
                        "verification_verdict": "correct",
                        "fact_id": "2222222222222222",
                    }
                },
            }
        )
        + completed(
            {
                "server": "danus",
                "tool": "fact_submit",
                "result": {
                    "structuredContent": {
                        "accepted": True,
                        "promoted": False,
                        "fact_id": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert loop._parse_last_fact_id(log) == "2222222222222222"

    # Rolling compatibility: a valid id in an explicitly attributed legacy
    # fact_submit response is the safe fallback when ``promoted`` is absent.
    log.write_text(
        completed(
            {
                "server": "danus",
                "tool": "fact_submit",
                "result": {
                    "structuredContent": {
                        "accepted": True,
                        "fact_id": "3333333333333333",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert loop._parse_last_fact_id(log) == "3333333333333333"


def test_parse_last_fact_id_streams_past_oversize_jsonl_with_bounded_warning(
    tmp: Path,
):
    wl = L.WorkerLayout(tmp / "P" / "workers" / "high")
    wl.logs.mkdir(parents=True)
    log = wl.logs / "round_1.log"

    def promoted(fact_id: str) -> bytes:
        return (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "server": "danus",
                        "tool": "fact_submit",
                        "result": {
                            "structuredContent": {
                                "accepted": True,
                                "promoted": True,
                                "submission_status": "promoted",
                                "verification_verdict": "correct",
                                "fact_id": fact_id,
                            }
                        },
                    },
                }
            ).encode()
            + b"\n"
        )

    with log.open("wb") as handle:
        handle.write(promoted("1111111111111111"))
        # A single read can be over the cap and already newline-terminated; it
        # is still an oversized event and must never be parsed.
        handle.write(b"{" + b"x" * loop.MAX_EXEC_LOG_EVENT_BYTES + b"\n")
        handle.write(promoted("2222222222222222"))
    assert loop._parse_last_fact_id(log, worker=wl) == "2222222222222222"
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert "skipped 1 JSONL event" in status["exec_log_parse_warning"]


def test_round_log_open_rejects_symlink_hardlink_and_fifo_without_mutation(
    tmp: Path,
):
    logs = tmp / "logs"
    logs.mkdir()
    sentinel = tmp / "sentinel"
    sentinel.write_text("do not truncate\n", encoding="utf-8")
    nodes = {
        "symlink.log": lambda path: path.symlink_to(sentinel),
        "hardlink.log": lambda path: os.link(sentinel, path),
        "fifo.log": lambda path: os.mkfifo(path),
    }
    for name, create in nodes.items():
        path = logs / name
        create(path)
        try:
            loop._open_round_log(path)
            raise AssertionError(f"unsafe round log {name} must be rejected")
        except (OSError, loop.HotJoinError):
            pass
        assert sentinel.read_text(encoding="utf-8") == "do not truncate\n"


def test_deadline_passed(tmp: Path):
    pdir = tmp / "proj"
    pdir.mkdir()
    assert loop._deadline_passed(pdir) is False  # no deadline file
    (pdir / L.DEADLINE_FILE).write_text("1")  # epoch 1 = long past
    assert loop._deadline_passed(pdir) is True
    (pdir / L.DEADLINE_FILE).write_text("garbage")  # bad = not passed
    assert loop._deadline_passed(pdir) is False


def test_write_status_atomic_and_stamps(tmp: Path):
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    loop.write_status(wl, state="running", round=2)
    st = json.loads(wl.status.read_text())
    assert st["state"] == "running" and st["round"] == 2
    assert st["worker"] == "high" and st["pid"] == os.getpid() and "updated_at" in st
    # merge, not overwrite: a second write keeps prior fields
    loop.write_status(wl, last_rc=0)
    st2 = json.loads(wl.status.read_text())
    assert st2["round"] == 2 and st2["last_rc"] == 0


def test_write_status_does_not_follow_planted_links(tmp: Path):
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    victim = tmp / "victim.txt"
    victim.write_text("KEEP", encoding="utf-8")
    wl.status.symlink_to(victim)
    legacy_temp = wl.status.with_suffix(wl.status.suffix + ".tmp")
    legacy_temp.symlink_to(victim)

    loop.write_status(wl, state="running", round=1)

    assert victim.read_text(encoding="utf-8") == "KEEP"
    assert not wl.status.is_symlink()
    assert json.loads(wl.status.read_text(encoding="utf-8"))["state"] == "running"
    assert legacy_temp.is_symlink()


def test_open_append_log_rejects_fifo_without_blocking(tmp: Path):
    log = tmp / "logs" / "loop.log"
    log.parent.mkdir()
    os.mkfifo(log)
    started = time.monotonic()
    try:
        scaffold.open_append_log(log)
        raise AssertionError("FIFO log must be rejected")
    except OSError:
        pass
    assert time.monotonic() - started < 1


def test_read_role_defaults_and_overrides(tmp: Path):
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "xhigh")
    wl.dir.mkdir(parents=True)
    # no .role -> defaults (the neutral DANUS_CODEX_MODEL unset → the built-in
    # gpt-5.6-sol default)
    with _env(DANUS_CODEX_MODEL=None):
        role = loop._read_role(wl)
    assert (
        role["MODEL"] == "gpt-5.6-sol"
        and role["ROLE"] == "high"
        and role["DANUS_AUTHOR"] == "xhigh"
    )
    # the neutral DANUS_CODEX_MODEL is the worker default when .role omits MODEL
    with _env(DANUS_CODEX_MODEL="neutral-model"):
        role = loop._read_role(wl)
    assert role["MODEL"] == "neutral-model"
    wl.role.write_text("# comment\nMODEL=gpt-x\nREASONING_EFFORT=xhigh\n\nROLE=xhigh\n")
    role = loop._read_role(wl)
    assert (
        role["MODEL"] == "gpt-x"
        and role["REASONING_EFFORT"] == "xhigh"
        and role["ROLE"] == "xhigh"
    )


def test_protected_role_ignores_worker_writable_role_projection(tmp: Path):
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "max")
    wl.dir.mkdir(parents=True)
    (wl.project_dir / "project.json").write_text(
        json.dumps(
            {
                "name": "proj",
                "model": "gpt-5.6-sol",
                "roles": "max:1",
                "workers": ["max"],
            }
        ),
        encoding="utf-8",
    )
    wl.role.write_text(
        "MODEL=attacker-model\nREASONING_EFFORT=ultra\nDANUS_AUTHOR=spoofed\n",
        encoding="utf-8",
    )

    role = loop._read_role(wl, protected=True)

    assert role == {
        "MODEL": "gpt-5.6-sol",
        "REASONING_EFFORT": "max",
        "ROLE": "max",
        "DANUS_AUTHOR": "max",
    }


# --- runner ---------------------------------------------------------------- #

_NO_TMP = {
    test_parse_roles_default_roster,
    test_parse_roles_rejects_bad_specs,
    test_worker_layout_paths,
    test_resolve_and_target,
}


def main() -> None:
    for t in [
        test_parse_roles_default_roster,
        test_parse_roles_rejects_bad_specs,
        test_worker_layout_paths,
        test_resolve_and_target,
        test_do_new_scaffolds_project,
        test_do_new_refuses_existing,
        test_do_new_rejects_project_traversal_before_filesystem_mutation,
        test_do_new_verify_url_from_env,
        test_parse_last_fact_id,
        test_deadline_passed,
        test_write_status_atomic_and_stamps,
        test_write_status_does_not_follow_planted_links,
        test_read_role_defaults_and_overrides,
        test_protected_role_ignores_worker_writable_role_projection,
    ]:
        if t in _NO_TMP:
            t()
        else:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL EXECUTION TESTS PASSED")


if __name__ == "__main__":
    main()
