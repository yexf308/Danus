"""``danus`` — the main agent's control surface over codex workers.

    danus list   [--json]
    danus new    <project> [--roles ROLE_SPEC] [--model M]
                 [--coordination reasoning-first|legacy]
                 [--active-explorers 0|1|2]
    danus assign <project>/<worker> (--task "…" | --file P | --stdin)
    danus say    <project>/<worker> (--text "…" | --file P | --stdin)
    danus encourage <project>/<worker> [--text "…" | --file P | --stdin]
    danus messages <project>[/<worker>] [--json]
    danus interrupt-turn <project>/<worker>
    danus abandon-intent <project>/<worker> --thread-id ID --client-id ID
                       --expected-state STATE --reason TEXT
                       --acknowledge-paid-outcome-unknown
    danus resolve-candidate <project> --receipt ID --outcome OUTCOME
                       --acknowledge-paid-outcome-unknown
    danus resolve-recommendation <project> --recommendation-id ID
                       --resolution adopted-master-guidance|continue-without-advisor
                       --acknowledge-recommendation-id ID
                       --acknowledge-resume-paid-reasoning
                       [--master-guidance-entry-id ID]
    danus cancel-prepared-intent <project>/<worker> --thread-id ID --client-id ID
                       --reason TEXT
    danus reset-thread <project>/<worker> --expected-thread-id ID
    danus rotate-thread <project>/<worker> --expected-thread-id ID --reason TEXT
    danus finalize <project> [--paper <paper_id>] [<fact_id> ...]
    danus start  <project>[/<worker>]
    danus status <project>[/<worker>] [--json]
    danus stop   <project>[/<worker>] [--force]

This module is the verbs/UX only. The worker outer loop, the on-disk layout, and
the scaffolding they drive live in ``danus.execution`` (imported here as a
library). Reads/writes only files under the project dir — the loop is autonomous;
this CLI just assigns / starts / monitors / stops it.

Notes:
  - the layout + scaffolding + config template are imported from ``danus.execution``
    (no duplicated layout / config template);
  - the verbs are mode-agnostic and identical across deployments.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from danus.coordination import (
    CoordinationError,
    CoordinationStore,
    coordination_config,
)
from danus.coordination.store import load_project_metadata
from danus.core import FactGraph, GlobalMemory
from danus.core.schema import clean_consult_provenance
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write, do_new, ensure_real_dir, spawn_loop
from danus.hotjoin import (
    DEFAULT_ENCOURAGEMENT,
    HotJoinError,
    HotJoinStore,
    IdempotencyConflict,
    StaleClaim,
)
from danus.strategy.browser_advisor import BrowserAdvisorError

__all__ = [
    "do_new",
    "do_assign",
    "do_start",
    "do_status",
    "worker_status",
    "do_list",
    "do_stop",
    "do_finalize",
    "do_say",
    "do_encourage",
    "do_messages",
    "do_interrupt_turn",
    "do_abandon_intent",
    "do_cancel_prepared_intent",
    "do_reset_thread",
    "do_rotate_thread",
    "do_resolve_candidate",
    "do_resolve_recommendation",
    "build_parser",
    "main",
]


# --------------------------------------------------------------------------- #
# read helpers                                                                 #
# --------------------------------------------------------------------------- #

_PID_RECORD_SCHEMA_VERSION = 1
_MAX_PID_RECORD_BYTES = 4096
_DARWIN_PROC_PIDTBSDINFO = 3


class ProcessIdentityError(RuntimeError):
    """A supervisor PID record is malformed or names a different process."""


class _DarwinProcBSDInfo(ctypes.Structure):
    """Stable prefix/layout returned by libproc PROC_PIDTBSDINFO."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _darwin_process_birth(
    pid: int, *, libproc: Optional[object] = None
) -> Dict[str, object]:
    """Read a microsecond-resolution kernel birth token from macOS libproc.

    ``ps -o lstart`` is only second-resolution and cannot safely distinguish a
    rapid PID reuse.  A missing/incompatible libproc is deliberately fatal to a
    lifecycle action instead of falling back to the weaker token.
    """
    if libproc is None:
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        except OSError as exc:
            raise ProcessIdentityError(
                f"cannot load macOS kernel process identity API: {exc}"
            ) from exc
    try:
        proc_pidinfo = libproc.proc_pidinfo  # type: ignore[attr-defined]
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = _DarwinProcBSDInfo()
        expected = ctypes.sizeof(info)
        received = proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            expected,
        )
    except (AttributeError, TypeError, ValueError, OSError) as exc:
        raise ProcessIdentityError(
            f"cannot call macOS kernel process identity API: {exc}"
        ) from exc
    if received != expected:
        try:
            os.kill(pid, 0)
        except ProcessLookupError as exc:
            raise ProcessLookupError(pid) from exc
        except PermissionError as exc:
            raise ProcessIdentityError(
                "cannot authenticate worker process owner"
            ) from exc
        raise ProcessIdentityError(
            "macOS kernel process identity API returned an incompatible record"
        )
    if info.pbi_pid != pid or info.pbi_start_tvusec >= 1_000_000:
        raise ProcessIdentityError("macOS kernel process identity record is malformed")
    return {
        "pgid": int(info.pbi_pgid),
        "start_token": (
            f"darwin-libproc:{int(info.pbi_start_tvsec)}:"
            f"{int(info.pbi_start_tvusec):06d}"
        ),
    }


def _load_pid_record(wl: L.WorkerLayout) -> Optional[Dict[str, object]]:
    """Load one host-written PID birth record without accepting legacy integers."""
    pf = wl.pid
    try:
        fd = os.open(str(pf), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > _MAX_PID_RECORD_BYTES
            ):
                raise ProcessIdentityError(
                    "worker PID record is not a safe regular file"
                )
            payload = os.read(fd, _MAX_PID_RECORD_BYTES + 1)
        finally:
            os.close(fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProcessIdentityError(
            f"cannot safely read worker PID record: {exc}"
        ) from exc

    try:
        record = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessIdentityError(
            "legacy or malformed worker PID record; refusing automatic lifecycle action"
        ) from exc
    if not isinstance(record, dict):
        raise ProcessIdentityError("worker PID record must be a JSON object")
    required = {"schema_version", "pid", "pgid", "start_token", "worker_dir"}
    if (
        set(record) != required
        or record.get("schema_version") != _PID_RECORD_SCHEMA_VERSION
    ):
        raise ProcessIdentityError("worker PID record has an unsupported schema")
    pid = record.get("pid")
    pgid = record.get("pgid")
    token = record.get("start_token")
    worker_dir = record.get("worker_dir")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(pgid, bool)
        or not isinstance(pgid, int)
        or pgid != pid
        or not isinstance(token, str)
        or not token
        or len(token.encode("utf-8")) > 512
        or worker_dir != os.path.abspath(str(wl.dir))
    ):
        raise ProcessIdentityError("worker PID record failed identity validation")
    return record


def _read_pid(wl: L.WorkerLayout) -> Optional[int]:
    """Compatibility read view; malformed/legacy records deliberately raise."""
    record = _load_pid_record(wl)
    return int(record["pid"]) if record is not None else None


def _ps_field(pid: int, field: str) -> str:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        completed = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessIdentityError(
            f"cannot inspect worker process identity: {exc}"
        ) from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        try:
            os.kill(pid, 0)
        except ProcessLookupError as exc:
            raise ProcessLookupError(pid) from exc
        except PermissionError as exc:
            raise ProcessIdentityError(
                "cannot authenticate worker process owner"
            ) from exc
        raise ProcessIdentityError("worker process identity probe returned no value")
    return value


def _process_identity(pid: int) -> Dict[str, object]:
    """Return the kernel birth token, PGID, and state for one exact PID."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ProcessIdentityError("refusing an unsafe worker PID")
    try:
        os.kill(pid, 0)
        observed_pgid = os.getpgid(pid)
    except ProcessLookupError as exc:
        raise ProcessLookupError(pid) from exc
    except PermissionError as exc:
        raise ProcessIdentityError("cannot authenticate worker process owner") from exc

    proc_stat = Path(f"/proc/{pid}/stat")
    if Path("/proc").is_dir():
        try:
            fields = proc_stat.read_text(encoding="ascii").rsplit(")", 1)[1].split()
            state = fields[0]
            proc_pgid = int(fields[2])
            start_ticks = fields[19]
        except FileNotFoundError as exc:
            raise ProcessLookupError(pid) from exc
        except (OSError, IndexError, ValueError) as exc:
            raise ProcessIdentityError(
                "cannot parse kernel worker process identity"
            ) from exc
        if proc_pgid != observed_pgid:
            raise ProcessIdentityError("worker PGID changed during identity inspection")
        return {
            "pid": pid,
            "pgid": observed_pgid,
            "start_token": f"linux-procfs:{start_ticks}",
            "state": state,
        }

    # macOS has no procfs. Use libproc's microsecond-resolution kernel birth
    # timestamp; `ps -o lstart` is only second-resolution and is not a safe PID
    # reuse fence. Other BSDs retain the C-locale ps fallback.
    if sys.platform == "darwin":
        birth = _darwin_process_birth(pid)
        ps_pgid = int(birth["pgid"])
        start_token = str(birth["start_token"])
    else:
        ps_pgid = int(_ps_field(pid, "pgid"))
        start_token = f"ps-lstart:{_ps_field(pid, 'lstart')}"
    state = _ps_field(pid, "state").split()[0]
    if ps_pgid != observed_pgid:
        raise ProcessIdentityError("worker PGID changed during identity inspection")
    return {
        "pid": pid,
        "pgid": observed_pgid,
        "start_token": start_token,
        "state": state,
    }


def _capture_pid_record(wl: L.WorkerLayout, pid: int) -> Dict[str, object]:
    try:
        identity = _process_identity(pid)
    except ProcessLookupError as exc:
        raise ProcessIdentityError(
            "spawned worker exited before supervisor registration"
        ) from exc
    if identity["pgid"] != pid:
        raise ProcessIdentityError("spawned worker is not its own process-group leader")
    if str(identity["state"]).startswith("Z"):
        raise ProcessIdentityError(
            "spawned worker exited before supervisor registration"
        )
    return {
        "schema_version": _PID_RECORD_SCHEMA_VERSION,
        "pid": pid,
        "pgid": pid,
        "start_token": identity["start_token"],
        "worker_dir": os.path.abspath(str(wl.dir)),
    }


def _pid_record_is_live(record: Dict[str, object]) -> bool:
    pid = int(record["pid"])
    try:
        observed = _process_identity(pid)
    except ProcessLookupError:
        return False
    if (
        observed["pgid"] != record["pgid"]
        or observed["start_token"] != record["start_token"]
    ):
        raise ProcessIdentityError(
            "worker PID was reused or its process identity no longer matches"
        )
    return not str(observed["state"]).startswith("Z")


def _write_pid_record(wl: L.WorkerLayout, record: Dict[str, object]) -> None:
    atomic_write(
        wl.pid,
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _unlink_pid_record(wl: L.WorkerLayout, expected: Dict[str, object]) -> None:
    """Remove only the record this lifecycle operation authenticated."""
    try:
        current = _load_pid_record(wl)
    except ProcessIdentityError:
        raise
    if current is None:
        return
    if current != expected:
        raise ProcessIdentityError("worker PID record changed concurrently")
    wl.pid.unlink()


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    # The pid exists — but a zombie (killed, not yet reaped by its parent) is
    # effectively dead. Linux /proc tells us the process state.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.rsplit(")", 1)[1].split()[0]  # field after "(comm)"
        return state != "Z"
    except (OSError, IndexError):
        return True


def _read_status(wl: L.WorkerLayout) -> Dict:
    sp = wl.status
    try:
        fd = os.open(str(sp), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 1_048_576
            ):
                return {}
            payload = bytearray()
            while len(payload) <= 1_048_576:
                chunk = os.read(fd, min(65_536, 1_048_577 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > 1_048_576:
                return {}
            decoded = json.loads(payload.decode("utf-8", errors="strict"))
            return decoded if isinstance(decoded, dict) else {}
        finally:
            os.close(fd)
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return {}


# --------------------------------------------------------------------------- #
# assign                                                                       #
# --------------------------------------------------------------------------- #


def do_assign(target: str, task: str) -> Dict:
    """Overwrite (replace, NOT append) a worker's TASK.md, ensuring a trailing
    newline. Rejects a bare project, a nonexistent worker, and an empty task."""
    project, worker = L.resolve_target(target)
    if not worker:
        raise SystemExit("assign needs a specific worker: <project>/<worker>")
    dirs = L.target_worker_dirs(target)
    if len(dirs) != 1:
        raise SystemExit(f"no such worker: {project}/{worker}")
    wl = L.WorkerLayout(dirs[0])
    if not task.strip():
        raise SystemExit("refusing to assign an empty task")
    normalized_task = task if task.endswith("\n") else task + "\n"
    generation_assignment = None
    reasoning_first = False
    try:
        metadata = load_project_metadata(wl.project_dir)
        config = coordination_config(metadata)
        if config.reasoning_first:
            reasoning_first = True
            store = CoordinationStore.open_existing(wl.project_dir, metadata)
            if store is None:
                raise CoordinationError(
                    "reasoning-first coordination database is missing"
                )
            coordination_status = store.project_status()
            if coordination_status["candidate"] is not None:
                raise SystemExit(
                    "active candidate freezes retask until its paid outcome is known"
                )
            task_staging = coordination_status.get("task_staging")
            paid_workers = set(
                task_staging.get("required_workers", [])
                if isinstance(task_staging, dict)
                else []
            )
            if worker in paid_workers:
                # The database is authoritative for paid work.  Stage it first;
                # TASK.md below is only an operator-facing projection and may
                # be recreated from the exact slot snapshot at launch.
                generation_assignment = store.stage_task_assignment(
                    worker,
                    normalized_task,
                )
    except (CoordinationError, OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit(f"coordination state unavailable: {exc}") from exc
    atomic_write(wl.task, normalized_task)
    result = {
        "worker": f"{project}/{worker}",
        "task_file": str(wl.task),
        "generation_staged": generation_assignment is not None,
    }
    if generation_assignment is not None:
        result.update(
            {
                "task_generation": generation_assignment.get("generation"),
                "task_sha256": generation_assignment.get("task_sha256"),
                "task_bytes": generation_assignment.get("task_bytes"),
            }
        )
    elif reasoning_first:
        result["assignment_scope"] = "dormant_observer_projection"
    return result


def do_resolve_candidate(
    project: str,
    candidate_receipt_id: str,
    *,
    outcome: str,
    acknowledge_paid_outcome_unknown: bool,
) -> Dict:
    """Release an unknown candidate only through an explicit owner audit seam."""

    try:
        project = L.validate_segment(project, label="project")
        project_dir = L.existing_project_dir(project)
    except ValueError as exc:
        raise SystemExit(f"invalid project: {exc}") from exc
    if project_dir is None:
        raise SystemExit(f"no such project: {project}")
    if outcome not in {"known-no-promotion", "abandon-unknown"}:
        raise SystemExit("unsupported candidate owner outcome")
    if acknowledge_paid_outcome_unknown is not True:
        raise SystemExit(
            "resolve-candidate requires --acknowledge-paid-outcome-unknown"
        )
    try:
        metadata = load_project_metadata(project_dir)
        if not coordination_config(metadata).reasoning_first:
            raise CoordinationError("legacy project has no candidate overlay")
        store = CoordinationStore.open_existing(project_dir, metadata)
        if store is None:
            raise CoordinationError("reasoning-first coordination database is missing")
        candidate = store.candidate_entry(candidate_receipt_id)
        if candidate is None:
            raise CoordinationError("candidate receipt does not exist")
        resolution = outcome.replace("-", "_")
        prior_resolution = candidate.get("owner_resolution")
        if prior_resolution is not None:
            fact_active = candidate.get("candidate_fact_active_at_resolution")
            if not isinstance(fact_active, bool):
                raise CoordinationError("candidate owner audit is incomplete")
            return store.resolve_candidate_outcome_unknown(
                candidate_receipt_id,
                resolution=resolution,
                acknowledge_paid_outcome_unknown=True,
                candidate_fact_active=fact_active,
            )
        candidate_fact_id = candidate.get("candidate_fact_id")
        if not isinstance(candidate_fact_id, str):
            raise CoordinationError("candidate has no canonical fact id")
        candidate_fact_identity = candidate.get("candidate_fact_identity")
        if (
            not isinstance(candidate_fact_identity, str)
            or len(candidate_fact_identity) != 64
            or any(
                character not in "0123456789abcdef"
                for character in candidate_fact_identity
            )
        ):
            raise CoordinationError("candidate has no canonical full fact identity")
        graph = FactGraph(project_dir)
        with graph.locked_active_fact_identity(candidate_fact_id) as active_identity:
            return store.resolve_candidate_outcome_unknown(
                candidate_receipt_id,
                resolution=resolution,
                acknowledge_paid_outcome_unknown=True,
                candidate_fact_active=active_identity == candidate_fact_identity,
            )
    except (CoordinationError, OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit(f"candidate resolution failed: {exc}") from exc


def do_resolve_recommendation(
    project: str,
    recommendation_id: str,
    *,
    resolution: str,
    acknowledge_recommendation_id: str,
    acknowledge_resume_paid_reasoning: bool,
    master_guidance_entry_id: str | None,
) -> Dict:
    """Owner-only exact-CAS resume after one terminal critic review."""

    try:
        project = L.validate_segment(project, label="project")
        project_dir = L.existing_project_dir(project)
    except ValueError as exc:
        raise SystemExit(f"invalid project: {exc}") from exc
    if project_dir is None:
        raise SystemExit(f"no such project: {project}")
    if acknowledge_recommendation_id != recommendation_id:
        raise SystemExit(
            "--acknowledge-recommendation-id must exactly equal --recommendation-id"
        )
    if acknowledge_resume_paid_reasoning is not True:
        raise SystemExit(
            "resolve-recommendation requires --acknowledge-resume-paid-reasoning"
        )
    if resolution not in {
        "adopted-master-guidance",
        "continue-without-advisor",
    }:
        raise SystemExit("unsupported recommendation owner resolution")
    if resolution == "adopted-master-guidance" and master_guidance_entry_id is None:
        raise SystemExit("adopted-master-guidance requires --master-guidance-entry-id")
    if (
        resolution == "continue-without-advisor"
        and master_guidance_entry_id is not None
    ):
        raise SystemExit(
            "continue-without-advisor cannot include --master-guidance-entry-id"
        )

    try:
        metadata = load_project_metadata(project_dir)
        if not coordination_config(metadata).reasoning_first:
            raise CoordinationError(
                "legacy project has no advisor recommendation state"
            )
        store = CoordinationStore.open_existing(project_dir, metadata)
        if store is None:
            raise CoordinationError("reasoning-first coordination database is missing")

        from danus.strategy.browser_advisor import BrowserAdvisorBroker

        with BrowserAdvisorBroker.project_memory_fence(project_dir):
            normalized_resolution = resolution.replace("-", "_")
            prior_resolution = store.recommendation_resolution(recommendation_id)
            if prior_resolution is not None:
                if (
                    prior_resolution["resolution"] == normalized_resolution
                    and prior_resolution["master_guidance_entry_id"]
                    == master_guidance_entry_id
                ):
                    return prior_resolution
                raise CoordinationError(
                    "recommendation already has a conflicting owner resolution"
                )

            if resolution == "continue-without-advisor":
                BrowserAdvisorBroker.assert_recommendation_releasable(
                    project_dir,
                    recommendation_id=recommendation_id,
                )

            record_sha256 = None
            browser_request_id = None
            browser_receipt_sha256 = None
            if master_guidance_entry_id is not None:
                guidance = GlobalMemory(project_dir).get(master_guidance_entry_id)
                if guidance.get("kind") != "master_guidance":
                    raise CoordinationError(
                        "owner resolution entry is not exact master_guidance"
                    )
                links = guidance.get("links")
                if (
                    not isinstance(links, dict)
                    or links.get("recommendation_id") != recommendation_id
                ):
                    raise CoordinationError(
                        "master guidance must link the exact recommendation id"
                    )
                encoded = json.dumps(
                    guidance,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                record_sha256 = hashlib.sha256(encoded).hexdigest()
                provenance_raw = guidance.get("consult_provenance")
                if provenance_raw is not None:
                    provenance = clean_consult_provenance(provenance_raw)
                    if provenance["transport"] == "chatgpt_pro_browser":
                        if provenance.get("recommendation_id") != recommendation_id:
                            raise CoordinationError(
                                "browser guidance must bind the exact recommendation id"
                            )
                        evidence = guidance.get("evidence")
                        if not isinstance(evidence, str):
                            raise CoordinationError(
                                "browser master guidance has invalid evidence"
                            )
                        BrowserAdvisorBroker.validate_adopted_master_guidance(
                            project_dir,
                            provenance=provenance,
                            evidence=evidence,
                        )
                        browser_request_id = provenance.get("request_id")
                        browser_receipt_sha256 = provenance.get("receipt_sha256")
                        if not isinstance(browser_request_id, str) or not isinstance(
                            browser_receipt_sha256, str
                        ):
                            raise CoordinationError(
                                "browser master guidance has incomplete adopted receipt"
                            )
            return store.resolve_recommendation(
                recommendation_id,
                resolution=normalized_resolution,
                owner_acknowledgement=acknowledge_recommendation_id,
                master_guidance_entry_id=master_guidance_entry_id,
                master_guidance_record_sha256=record_sha256,
                browser_request_id=browser_request_id,
                browser_receipt_sha256=browser_receipt_sha256,
            )
    except (
        BrowserAdvisorError,
        CoordinationError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        raise SystemExit(f"recommendation resolution failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# human hot-join mailbox                                                       #
# --------------------------------------------------------------------------- #


def _hotjoin_target(target: str) -> tuple[str, str, HotJoinStore]:
    project, worker = L.resolve_target(target)
    if not worker:
        raise SystemExit("hot-join needs a specific worker: <project>/<worker>")
    dirs = L.target_worker_dirs(target)
    if len(dirs) != 1:
        raise SystemExit(f"no such worker: {project}/{worker}")
    wl = L.WorkerLayout(dirs[0])
    return project, worker, HotJoinStore(wl.project_dir)


def do_say(
    target: str,
    text: str,
    *,
    client_id: Optional[str] = None,
    fallback: str = "queue",
) -> Dict:
    """Durably enqueue one owner message for an existing worker.

    A live app-server worker attempts same-turn ``turn/steer``.  If no active
    turn exists, the explicit fallback is either queue or fail.  Plain message
    text never triggers stop/pause/interrupt semantics.
    """
    _project, worker, store = _hotjoin_target(target)
    try:
        return store.enqueue(
            target=worker,
            body=text,
            client_id=client_id,
            fallback=fallback,
            kind="message",
        )
    except (ValueError, IdempotencyConflict) as exc:
        raise SystemExit(f"cannot enqueue human message: {exc}") from exc


def do_encourage(
    target: str,
    text: str = DEFAULT_ENCOURAGEMENT,
    *,
    client_id: Optional[str] = None,
) -> Dict:
    """Send non-authoritative morale support to this exact live paid turn.

    The authenticated worker birth record is checked under its lifecycle lock.
    The message store then atomically snapshots the canonical started intent's
    thread and turn IDs. Delivery is fail-only and the broker refuses any later
    turn, so this command can neither queue future guidance nor retarget a race.
    """
    lock = None
    locked = False
    try:
        project, worker, store = _hotjoin_target(target)
        worker_dir = L.existing_worker_dir(project, worker)
        assert worker_dir is not None
        wl = L.WorkerLayout(worker_dir)
        lock = _open_worker_lock(wl)
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise HotJoinError(
                "worker lifecycle lock is busy; retry after its current "
                "start, stop, or owner action"
            ) from exc
        pid_record = _load_pid_record(wl)
        if pid_record is None or not _pid_record_is_live(pid_record):
            raise HotJoinError("worker is not authoritatively live")
        return store.enqueue_encouragement(
            target=worker,
            note=text,
            client_id=client_id,
        )
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        HotJoinError,
        ProcessIdentityError,
    ) as exc:
        raise SystemExit(f"cannot encourage worker: {exc}") from exc
    finally:
        if lock is not None:
            if locked:
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def do_interrupt_turn(target: str, *, client_id: Optional[str] = None) -> Dict:
    """Enqueue an explicit owner control request to interrupt one active turn."""
    _project, worker, store = _hotjoin_target(target)
    try:
        return store.enqueue(
            target=worker,
            body="",
            client_id=client_id,
            fallback="fail",
            kind="interrupt",
        )
    except (ValueError, IdempotencyConflict) as exc:
        raise SystemExit(f"cannot enqueue interrupt: {exc}") from exc


def do_abandon_intent(
    target: str,
    *,
    thread_id: str,
    client_id: str,
    expected_state: str,
    reason: str,
    acknowledge_paid_outcome_unknown: bool,
) -> Dict:
    """Owner-only terminal CAS for a paid outcome that cannot be reconciled.

    The worker must be proven fail-stopped under its lifecycle lock.  This
    command records risk acceptance; it never signals, retries, rotates, resets,
    or deletes any conversation/message/audit history.
    """
    project, worker, store = _hotjoin_target(target)
    worker_dir = L.existing_worker_dir(project, worker)
    assert worker_dir is not None
    wl = L.WorkerLayout(worker_dir)
    lock = None
    locked = False
    try:
        lock = _open_worker_lock(wl)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise HotJoinError(
                "worker lifecycle lock is busy; retry only after the current "
                "start/registration or owner lifecycle action has finished"
            ) from exc
        pid_record = _load_pid_record(wl)
        if pid_record is not None and _pid_record_is_live(pid_record):
            raise HotJoinError(
                "worker is still live; stop it before abandoning a paid outcome"
            )
        return store.abandon_round_intent(
            target=worker,
            thread_id=thread_id,
            client_id=client_id,
            expected_state=expected_state,
            reason=reason,
            acknowledge_paid_outcome_unknown=acknowledge_paid_outcome_unknown,
        )
    except KeyError as exc:
        raise SystemExit(
            f"cannot abandon paid-turn intent: no such client id {exc.args[0]}"
        ) from exc
    except (ValueError, StaleClaim, HotJoinError, ProcessIdentityError) as exc:
        raise SystemExit(f"cannot abandon paid-turn intent: {exc}") from exc
    finally:
        if lock is not None:
            if locked:
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def do_cancel_prepared_intent(
    target: str,
    *,
    thread_id: str,
    client_id: str,
    reason: str,
) -> Dict:
    """Owner-only exact CAS for an authoritatively unspent prepared intent."""
    project, worker, store = _hotjoin_target(target)
    worker_dir = L.existing_worker_dir(project, worker)
    assert worker_dir is not None
    wl = L.WorkerLayout(worker_dir)
    lock = None
    locked = False
    try:
        lock = _open_worker_lock(wl)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise HotJoinError(
                "worker lifecycle lock is busy; retry only after the current "
                "start/registration or owner lifecycle action has finished"
            ) from exc
        pid_record = _load_pid_record(wl)
        if pid_record is not None and _pid_record_is_live(pid_record):
            raise HotJoinError(
                "worker is still live; stop it before cancelling a prepared intent"
            )
        return store.cancel_prepared_round_intent(
            target=worker,
            thread_id=thread_id,
            client_id=client_id,
            reason=reason,
        )
    except KeyError as exc:
        raise SystemExit(
            f"cannot cancel prepared intent: no such client id {exc.args[0]}"
        ) from exc
    except (ValueError, StaleClaim, HotJoinError, ProcessIdentityError) as exc:
        raise SystemExit(f"cannot cancel prepared intent: {exc}") from exc
    finally:
        if lock is not None:
            if locked:
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def do_reset_thread(target: str, *, expected_thread_id: str) -> Dict:
    """Owner-authorized recovery for a server-deleted app-server thread.

    The exact expected id is mandatory and the store refuses the operation
    while any paid turn is unfinished.  This never interrupts or retries work.
    """
    return _remove_thread_mapping_under_lifecycle_lock(
        target,
        expected_thread_id=expected_thread_id,
        action="reset",
        detail="explicit danus reset-thread command",
    )


def do_rotate_thread(target: str, *, expected_thread_id: str, reason: str) -> Dict:
    """Owner-authorized terminal thread rotation after a bounded resume failure.

    This only clears the CAS-fenced conversation mapping.  It neither starts a
    replacement thread nor mutates the worker's persisted research memory.
    """
    return _remove_thread_mapping_under_lifecycle_lock(
        target,
        expected_thread_id=expected_thread_id,
        action="rotate",
        detail=reason,
    )


def _remove_thread_mapping_under_lifecycle_lock(
    target: str,
    *,
    expected_thread_id: str,
    action: str,
    detail: str,
) -> Dict:
    """Remove one mapping only while the worker is proven fail-stopped.

    Reset and rotation have different audit semantics but the same lifecycle
    safety boundary.  The lock is held across authenticated PID liveness and the
    SQLite expected-thread CAS, including ``start``'s spawn/registration window.
    """
    if action not in {"reset", "rotate"}:
        raise ValueError("unsupported thread mapping action")
    project, worker, store = _hotjoin_target(target)
    worker_dir = L.existing_worker_dir(project, worker)
    assert worker_dir is not None
    wl = L.WorkerLayout(worker_dir)
    lock = None
    locked = False
    try:
        # Serialize the complete liveness decision and thread-mapping CAS with
        # ``start``.  In particular, ``_start_one`` holds this same lock from
        # before spawn until after its authenticated PID record is durable, so
        # rotation can never mistake that registration window for a dead
        # worker and delete the conversation that the new worker will resume.
        lock = _open_worker_lock(wl)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise HotJoinError(
                "worker lifecycle lock is busy; retry after start/registration "
                "has finished"
            ) from exc
        pid_record = _load_pid_record(wl)
        if pid_record is not None and _pid_record_is_live(pid_record):
            raise HotJoinError(
                "worker is still live; wait for fail-stop before rotating its thread"
            )
        if action == "reset":
            return store.clear_thread_id(
                worker,
                expected_thread_id=expected_thread_id,
                detail=detail,
            )
        return store.rotate_thread_id(
            worker, expected_thread_id=expected_thread_id, reason=detail
        )
    except (ValueError, StaleClaim, HotJoinError, ProcessIdentityError) as exc:
        raise SystemExit(f"cannot {action} app-server thread: {exc}") from exc
    finally:
        if lock is not None:
            if locked:
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def do_messages(target: str, *, limit: int = 100) -> List[Dict]:
    project, worker = L.resolve_target(target)
    try:
        pdir = L.existing_project_dir(project)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if pdir is None:
        raise SystemExit(f"no such project: {project}")
    if worker is not None and L.existing_worker_dir(project, worker) is None:
        raise SystemExit(f"no such worker: {project}/{worker}")
    store = HotJoinStore(pdir)
    try:
        return store.list_messages(target=worker, limit=limit)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _fmt_messages(rows: List[Dict]) -> str:
    if not rows:
        return "(no human intervention messages)"
    lines = []
    for row in reversed(rows):
        summary = "<interrupt>" if row["kind"] == "interrupt" else row["body"]
        summary = summary.replace("\n", " ")[:80]
        lines.append(
            f"{row['message_id']}  {row['target']:<12} {row['state']:<18} {summary}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# finalize                                                                     #
# --------------------------------------------------------------------------- #


def do_finalize(
    project: str, fact_ids: List[str], paper_id: Optional[str] = None
) -> Dict:
    """Record the finalized target theorem(s) for a PAPER of a project in that
    paper's TARGET.md — the durable slot write-paper reads (never a guess). The
    default paper writes the LEGACY ``<project>/TARGET.md``; a non-default
    ``paper_id`` writes ``<project>/papers/<paper_id>/TARGET.md`` (its own
    workspace). One fact graph per project; per-paper targets.

    Resolves the project dir, VALIDATES every ``fact_id`` against that project's
    fact graph (refuses an id the graph does not have — you cannot record a
    phantom target), then writes the ids to the paper's TARGET.md.

    With NO ``fact_ids`` (suggestion mode): prints the candidate terminal facts
    (facts that are no other fact's predecessor — the ``assemble._terminal_facts``
    helper) as SUGGESTIONS and writes NOTHING (returns ``{"suggested": [...]}``).

    Rejections raise ``SystemExit`` (nonzero exit) with a clear message."""
    from danus.core import FactGraph
    from danus.write_paper import assemble

    try:
        pdir = L.existing_project_dir(project)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if pdir is None:
        raise SystemExit(f"no such project: {project}")
    fg = FactGraph(pdir)

    if not fact_ids:
        # suggestion mode: never auto-pick — just list candidate terminal facts.
        return {
            "project": project,
            "paper_id": paper_id,
            "suggested": assemble._terminal_facts(fg),
        }

    unknown = [fid for fid in fact_ids if not fg.exists(fid)]
    if unknown:
        raise SystemExit(
            f"cannot finalize: unknown fact id(s) in {project}: {', '.join(unknown)} "
            f"(a target must be a verified fact in the project's graph)"
        )
    # validate a non-default paper_id as a single safe path segment before writing.
    try:
        if not assemble._is_default_paper(paper_id):
            assemble._validate_paper_id(paper_id)  # type: ignore[arg-type]
    except ValueError as e:
        raise SystemExit(f"cannot finalize: {e}")
    # de-dup while preserving order
    seen: set = set()
    ids: List[str] = []
    for fid in fact_ids:
        if fid not in seen:
            seen.add(fid)
            ids.append(fid)
    path = assemble.write_target_fact_ids(pdir, ids, paper_id)
    return {
        "project": project,
        "paper_id": paper_id,
        "target_file": str(path),
        "target_fact_ids": ids,
    }


# --------------------------------------------------------------------------- #
# start                                                                        #
# --------------------------------------------------------------------------- #


def _open_worker_lock(wl: L.WorkerLayout):
    lock_fd = os.open(
        str(wl.lock),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    lock_info = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
        os.close(lock_fd)
        raise OSError("refusing unsafe worker pid lock")
    return os.fdopen(lock_fd, "r+")


def _cleanup_unregistered_child(proc: subprocess.Popen) -> None:
    """Kill/reap a retained direct child that could not be registered.

    The leader is observed with WNOWAIT and kept unreaped through a final group
    KILL, so a fast-exit worker cannot leave a same-group descendant behind or
    expose a PID/PGID reuse window during failed registration cleanup.
    """
    pid = proc.pid
    if proc.returncode is not None:
        # A caller that already reaped this handle has released the PID/PGID
        # fence.  Never signal a numeric identity in that state.
        return
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ProcessIdentityError("spawned worker has an unsafe PID")
    try:
        leader_exited = (
            os.waitid(
                os.P_PID,
                pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            is not None
        )
    except InterruptedError:
        leader_exited = False
    except ChildProcessError:
        # Without the retained direct-child fence, a numeric group signal could
        # target a reused identity.  Fail closed and send no signal.
        return
    try:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = pid if leader_exited else None
        group_owned = pgid == pid
        if group_owned:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                if not leader_exited:
                    raise
            deadline = time.monotonic() + 0.25
            while not leader_exited and time.monotonic() < deadline:
                try:
                    leader_exited = (
                        os.waitid(
                            os.P_PID,
                            pid,
                            os.WEXITED | os.WNOHANG | os.WNOWAIT,
                        )
                        is not None
                    )
                except InterruptedError:
                    continue
                if not leader_exited:
                    time.sleep(0.02)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if not leader_exited:
                    raise
        elif pgid is not None:
            # Popen(start_new_session=True) should make pgid == pid.  If that
            # invariant failed, signal only our unreaped direct child rather
            # than risking an unrelated process group.
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        try:
            proc.wait()
        except (ChildProcessError, ProcessLookupError):
            pass


def _start_one(wl: L.WorkerLayout) -> str:
    """Returns 'started' / 'already-running' / 'locked'. Idempotent via an flock
    on .pid.lock; clears a stale .stop before spawning."""
    wl.dir.mkdir(parents=True, exist_ok=True)
    ensure_real_dir(wl.logs)
    lock = _open_worker_lock(wl)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        previous = _load_pid_record(wl)
        if previous is not None:
            if _pid_record_is_live(previous):
                return "already-running"
            _unlink_pid_record(wl, previous)
        wl.stop.unlink(missing_ok=True)  # clear a stale stop flag
        previous_sigchld = signal.getsignal(signal.SIGCHLD)
        # SIG_IGN/SA_NOCLDWAIT inherited from an embedding shell would let a
        # fast child auto-reap before its birth record is durable.  Preserve the
        # caller's setting, but hold SIG_DFL across spawn and registration.
        sigchld_changed = False
        if threading.current_thread() is threading.main_thread():
            # Call even when the visible handler is already SIG_DFL: this also
            # clears an inherited SA_NOCLDWAIT flag.
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            sigchld_changed = True
        elif previous_sigchld != signal.SIG_DFL:
            raise ProcessIdentityError(
                "cannot establish a SIGCHLD child-reaping fence outside the main thread"
            )
        proc: Optional[subprocess.Popen] = None
        record: Optional[Dict[str, object]] = None
        registered = False
        try:
            proc = spawn_loop(wl.dir)
            record = _capture_pid_record(wl, proc.pid)
            _write_pid_record(wl, record)
            registered = True
            if not _pid_record_is_live(record):
                raise ProcessIdentityError(
                    "spawned worker exited during supervisor registration"
                )
        except BaseException:
            try:
                if registered and record is not None:
                    _unlink_pid_record(wl, record)
            finally:
                if proc is not None:
                    _cleanup_unregistered_child(proc)
            raise
        finally:
            if sigchld_changed:
                signal.signal(signal.SIGCHLD, previous_sigchld)
        return "started"
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def do_start(target: str, stagger: float = 0.2) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    out = []
    for i, wdir in enumerate(dirs):
        if i and stagger:
            time.sleep(stagger)
        wl = L.WorkerLayout(wdir)
        try:
            result = _start_one(wl)
        except ProcessIdentityError as exc:
            raise SystemExit(
                f"cannot start {wl.project}/{wl.name}: {exc}. Inspect {wl.pid}; "
                "after confirming no matching worker is running, archive the old "
                "PID record and retry"
            ) from exc
        out.append({"worker": wdir.name, "result": result})
    return out


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #


def _bounded_status_json(value: object, *, max_bytes: int) -> object:
    """Project one host-written diagnostic without allowing status ballooning."""

    if value is None:
        return None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return {"omitted": True, "reason": "malformed_status_telemetry"}
    if len(encoded) > max_bytes:
        return {
            "omitted": True,
            "reason": "status_telemetry_exceeds_projection_limit",
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return json.loads(encoded.decode("utf-8"))


def _bounded_status_scalar(value: object, *, max_bytes: int = 512) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            return None
        return value if len(encoded) <= max_bytes else None
    return None


def worker_status(wl: L.WorkerLayout) -> Dict:
    pid = None
    alive = False
    identity_error = None
    try:
        record = _load_pid_record(wl)
        if record is not None:
            pid = int(record["pid"])
            alive = _pid_record_is_live(record)
    except ProcessIdentityError as exc:
        identity_error = str(exc)
    st = _read_status(wl)
    coordination_error = None
    coordination_view: Dict[str, object] = {
        "mode": "legacy",
        "lane": "legacy",
        "generation": None,
        "phase": None,
        "paid_active": None,
        "waiting_admission": 0,
        "phase_deadline_at": None,
        "phase_deadline_exceeded": False,
        "advisor_reachable": False,
        "advisor_recommendation_present": False,
        "advisor_recommendation_ready": False,
        "fail_stop_reason": None,
        "review": None,
        "recommendation": None,
        "resolution": None,
        "candidate": None,
        "task_staging": None,
        "explorer_workers": [],
    }
    try:
        metadata = load_project_metadata(wl.project_dir)
        config = coordination_config(metadata)
        if config.reasoning_first:
            store = CoordinationStore.open_existing(wl.project_dir, metadata)
            if store is None:
                raise CoordinationError(
                    "reasoning-first coordination database is missing"
                )
            coordination_view = store.project_status(wl.name)
    except (CoordinationError, OSError, sqlite3.Error, ValueError) as exc:
        coordination_error = str(exc)[:512]
    unfinished_paid_intent = None
    intent_ledger_error = None
    database = wl.project_dir / ".human-intervention" / "events.sqlite3"
    try:
        os.lstat(database)
    except FileNotFoundError:
        pass
    except OSError as exc:
        intent_ledger_error = f"cannot inspect paid-intent ledger: {exc}"
    else:
        try:
            intent = HotJoinStore(wl.project_dir).unfinished_round_intent(wl.name)
            if intent is not None:
                unfinished_paid_intent = {
                    key: intent.get(key)
                    for key in (
                        "client_id",
                        "thread_id",
                        "state",
                        "turn_id",
                        "requested_model",
                        "requested_effort",
                        "prompt_sha256",
                    )
                }
        except (HotJoinError, OSError, sqlite3.Error, ValueError) as exc:
            intent_ledger_error = (
                "cannot read canonical paid-intent ledger: " + str(exc)[:512]
            )
    state = st.get("state", "—")
    now = time.time()
    last = st.get("last_round_at") or st.get("round_started_at") or st.get("updated_at")
    age = (now - last) if isinstance(last, (int, float)) else None

    if identity_error is not None:
        label = "pid-unsafe"
    elif alive:
        if state == "waiting_admission":
            label = "waiting"
        else:
            # a round legitimately runs for hours; only flag truly stale running rounds
            rs = st.get("round_started_at")
            hard = (
                2700
                if coordination_view.get("mode") == "reasoning_first_v1"
                else int(os.environ.get("DANUS_ROUND_HARD_TIMEOUT", "14400"))
            )
            if (
                state == "running"
                and isinstance(rs, (int, float))
                and (now - rs) > hard * 1.5
            ):
                label = "stuck?"
            else:
                label = "working"
    else:
        label = (
            state
            if state
            in ("stopped", "deadline", "max_rounds", "error", "terminated", "created")
            else "dead"
        )
    recovery_required = st.get("recovery_required")
    paid_intent_status = None
    if unfinished_paid_intent is not None and alive:
        # A live worker owns this intent and may be inside the normal
        # dispatch/start/streaming gap.  Publishing an owner recovery action
        # here is actively unsafe: abandon-intent is intentionally restricted
        # to a fail-stopped worker, and the CLI must not make ordinary paid work
        # look like an outcome-unknown incident while it is still running.
        recovery_required = None
        paid_intent_status = (
            "outcome_unknown_while_worker_live"
            if unfinished_paid_intent["state"] == "delivery_unknown"
            else "in_progress"
        )
    elif unfinished_paid_intent is not None:
        # PID-unsafe is deliberately handled like the existing fail-stopped
        # projection: the recovery command itself re-authenticates lifecycle
        # state under the worker lock and refuses an unsafe process identity.
        paid_intent_status = "recovery_required"
        if unfinished_paid_intent["state"] == "prepared":
            recovery_required = {
                "action": "resume_or_cancel_prepared_intent",
                "paid_dispatch_state": "not_dispatched",
                "paid_outcome_unknown": False,
                "argv": ["bin/danus", "start", f"{wl.project}/{wl.name}"],
                "cancel_argv": [
                    "bin/danus",
                    "cancel-prepared-intent",
                    f"{wl.project}/{wl.name}",
                    "--thread-id",
                    str(unfinished_paid_intent["thread_id"]),
                    "--client-id",
                    str(unfinished_paid_intent["client_id"]),
                    "--reason",
                    "<OWNER_REASON>",
                ],
                "detail": (
                    "prepared is authoritatively pre-dispatch; restart resumes "
                    "the same immutable prompt/model/effort intent; if those "
                    "inputs drifted, cancel the exact unspent intent first"
                ),
            }
        else:
            recovery_required = {
                "action": "abandon_intent",
                "requires_fail_stopped_worker": True,
                "paid_outcome_unknown": True,
                "argv": [
                    "bin/danus",
                    "abandon-intent",
                    f"{wl.project}/{wl.name}",
                    "--thread-id",
                    str(unfinished_paid_intent["thread_id"]),
                    "--client-id",
                    str(unfinished_paid_intent["client_id"]),
                    "--expected-state",
                    str(unfinished_paid_intent["state"]),
                    "--reason",
                    "<OWNER_REASON>",
                    "--acknowledge-paid-outcome-unknown",
                ],
                "next_action": "reset-thread or rotate-thread before restart",
            }
    return {
        "worker": wl.name,
        "pid": pid,
        "alive": alive,
        "state": state,
        "round": st.get("round", 0),
        "age_s": round(age, 1) if age is not None else None,
        "last_fact_id": st.get("last_fact_id"),
        "label": label,
        # Kept out of the compact table but exposed in --json so an owner can
        # CAS-fence an explicit recovery after a server-side thread deletion.
        "app_server_thread_id": st.get("app_server_thread_id"),
        "active_turn_id": st.get("active_turn_id"),
        "error": st.get("error"),
        "last_attempt": st.get("last_attempt"),
        "last_paid_turn": _bounded_status_json(
            st.get("last_paid_turn"), max_bytes=262_144
        ),
        "last_turn_reasoning_bandwidth": _bounded_status_json(
            st.get("last_turn_reasoning_bandwidth"), max_bytes=131_072
        ),
        "last_turn_token_usage": _bounded_status_json(
            st.get("last_turn_token_usage"), max_bytes=32_768
        ),
        "last_turn_token_usage_observed": _bounded_status_scalar(
            st.get("last_turn_token_usage_observed")
        ),
        "last_turn_token_usage_finality": _bounded_status_scalar(
            st.get("last_turn_token_usage_finality")
        ),
        "last_turn_status": _bounded_status_scalar(st.get("last_turn_status")),
        "last_turn_model": _bounded_status_scalar(st.get("last_turn_model")),
        "last_turn_effort": _bounded_status_scalar(st.get("last_turn_effort")),
        "last_turn_model_rerouted": _bounded_status_scalar(
            st.get("last_turn_model_rerouted")
        ),
        "recovery_required": recovery_required,
        "paid_intent_status": paid_intent_status,
        "unfinished_paid_intent": unfinished_paid_intent,
        "intent_ledger_error": intent_ledger_error,
        "pid_record_error": identity_error,
        "live_processes": 1 if alive else 0,
        "paid_active": coordination_view.get("paid_active"),
        "waiting_admission": coordination_view.get("waiting_admission", 0),
        "lane": coordination_view.get("lane"),
        "generation": coordination_view.get("generation"),
        "phase": coordination_view.get("phase"),
        "phase_deadline_at": coordination_view.get("phase_deadline_at"),
        "phase_deadline_exceeded": coordination_view.get(
            "phase_deadline_exceeded", False
        ),
        "advisor_reachable": coordination_view.get("advisor_reachable", False),
        "advisor_recommendation_present": coordination_view.get(
            "advisor_recommendation_present", False
        ),
        "advisor_recommendation_ready": coordination_view.get(
            "advisor_recommendation_ready", False
        ),
        "fail_stop_reason": coordination_view.get("fail_stop_reason"),
        "review": coordination_view.get("review"),
        "recommendation": coordination_view.get("recommendation"),
        "resolution": coordination_view.get("resolution"),
        "candidate": coordination_view.get("candidate"),
        "task_staging": coordination_view.get("task_staging"),
        "explorer_workers": coordination_view.get("explorer_workers", []),
        "coordination_mode": coordination_view.get("mode"),
        "coordination_error": coordination_error,
    }


def do_status(target: str) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [worker_status(L.WorkerLayout(d)) for d in dirs]


# --------------------------------------------------------------------------- #
# list                                                                         #
# --------------------------------------------------------------------------- #


def do_list() -> List[Dict]:
    """One row per project: roster + how many workers are live + model."""
    out: List[Dict] = []
    for project in L.list_projects():
        coordination_error = None
        try:
            meta = load_project_metadata(L.project_dir(project))
        except (CoordinationError, OSError, ValueError) as exc:
            meta = {}
            coordination_error = str(exc)[:512]
        workers = L.list_workers(project)
        live = 0
        waiting_live = 0
        for worker in workers:
            wl = L.WorkerLayout(L.worker_dir(project, worker))
            try:
                record = _load_pid_record(wl)
                if record is not None and _pid_record_is_live(record):
                    live += 1
                    if _read_status(wl).get("state") == "waiting_admission":
                        waiting_live += 1
            except ProcessIdentityError:
                # Listing stays read-only, but an unsafe record is never counted
                # as live and start/stop will refuse to act on it.
                pass
        coordination_view: Dict[str, object] = {
            "mode": "legacy",
            "generation": None,
            "phase": None,
            "root_worker": None,
            "critic_worker": None,
            "explorer_workers": [],
            "paid_active": None,
            "phase_deadline_at": None,
            "phase_deadline_exceeded": False,
            "advisor_reachable": False,
            "advisor_recommendation_present": False,
            "advisor_recommendation_ready": False,
            "fail_stop_reason": None,
            "review": None,
            "recommendation": None,
            "resolution": None,
            "candidate": None,
            "task_staging": None,
        }
        try:
            if coordination_error is None:
                config = coordination_config(meta)
                if config.reasoning_first:
                    store = CoordinationStore.open_existing(
                        L.project_dir(project), meta
                    )
                    if store is None:
                        raise CoordinationError(
                            "reasoning-first coordination database is missing"
                        )
                    coordination_view = store.project_status()
        except (CoordinationError, OSError, sqlite3.Error, ValueError) as exc:
            coordination_error = str(exc)[:512]
        out.append(
            {
                "project": project,
                "workers": len(workers),
                "live": live,
                "live_processes": live,
                "paid_active": coordination_view.get("paid_active"),
                "waiting_admission": waiting_live,
                "lane": {
                    "root": coordination_view.get("root_worker"),
                    "critic": coordination_view.get("critic_worker"),
                    **{
                        f"explorer{index}": worker
                        for index, worker in enumerate(
                            coordination_view.get("explorer_workers") or [],
                            start=1,
                        )
                    },
                },
                "explorer_workers": coordination_view.get("explorer_workers", []),
                "generation": coordination_view.get("generation"),
                "phase": coordination_view.get("phase"),
                "phase_deadline_at": coordination_view.get("phase_deadline_at"),
                "phase_deadline_exceeded": coordination_view.get(
                    "phase_deadline_exceeded", False
                ),
                "advisor_reachable": coordination_view.get("advisor_reachable", False),
                "advisor_recommendation_present": coordination_view.get(
                    "advisor_recommendation_present", False
                ),
                "advisor_recommendation_ready": coordination_view.get(
                    "advisor_recommendation_ready", False
                ),
                "fail_stop_reason": coordination_view.get("fail_stop_reason"),
                "review": coordination_view.get("review"),
                "recommendation": coordination_view.get("recommendation"),
                "resolution": coordination_view.get("resolution"),
                "candidate": coordination_view.get("candidate"),
                "task_staging": coordination_view.get("task_staging"),
                "coordination_mode": coordination_view.get("mode"),
                "coordination_error": coordination_error,
                "model": meta.get("model", "—"),
            }
        )
    return out


def _fmt_list(rows: List[Dict]) -> str:
    head = (
        f"{'PROJECT':<24}{'WORKERS':>8}{'LIVE':>6}{'PAID':>6}{'WAIT':>6}  "
        f"{'PHASE':<22}{'CANDIDATE':<16}{'MODEL':<12}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        paid = r.get("paid_active")
        lines.append(
            f"{r['project']:<24}{r['workers']:>8}{r['live']:>6}"
            f"{str(paid if paid is not None else '—'):>6}"
            f"{str(r.get('waiting_admission', 0)):>6}  "
            f"{str(r.get('phase') or '—'):<22}"
            f"{str((r.get('candidate') or {}).get('state', '—')):<16}"
            f"{str(r['model']):<12}"
        )
    return "\n".join(lines) if rows else "(no projects under the agents root)"


def _fmt_status(rows: List[Dict]) -> str:
    head = (
        f"{'WORKER':<14}{'LANE':<10}{'LABEL':<12}{'STATE':<19}"
        f"{'ROUND':>6}  {'AGE':>7}  {'CANDIDATE':<16}{'LAST_FACT':<16}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        age = f"{r['age_s']:.0f}s" if r["age_s"] is not None else "—"
        lines.append(
            f"{r['worker']:<14}{str(r.get('lane') or '—'):<10}"
            f"{r['label']:<12}{r['state']:<19}{r['round']:>6}  {age:>7}  "
            f"{str((r.get('candidate') or {}).get('state', '—')):<16}"
            f"{str(r['last_fact_id'] or '—'):<16}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# stop                                                                         #
# --------------------------------------------------------------------------- #


def _stop_one(wl: L.WorkerLayout, force: bool) -> str:
    lock = _open_worker_lock(wl)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = _load_pid_record(wl)
        if record is None:
            return "not-running"
        if not _pid_record_is_live(record):
            _unlink_pid_record(wl, record)
            return "not-running"
        if not force:
            atomic_write(wl.stop, "1\n")  # loop exits at the round boundary
            return "stopping (graceful)"
        # External ``inspect birth -> killpg(numeric_pid)`` has an unavoidable
        # exit/reap/PGID-reuse race on platforms without an atomic process
        # handle.  Publish the same durable request; the worker owns retained
        # Popen handles and performs the prompt cooperative interrupt/direct-
        # child termination itself.
        atomic_write(wl.stop, "force\n")
        return "stopping (cooperative force)"
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def do_stop(target: str, force: bool = False) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    out = []
    for wdir in dirs:
        wl = L.WorkerLayout(wdir)
        try:
            result = _stop_one(wl, force)
        except ProcessIdentityError as exc:
            raise SystemExit(
                f"cannot stop {wl.project}/{wl.name}: {exc}. Inspect {wl.pid}; "
                "after confirming no matching worker is running, archive the old "
                "PID record and retry"
            ) from exc
        out.append({"worker": wdir.name, "result": result})
    return out


# --------------------------------------------------------------------------- #
# argparse                                                                      #
# --------------------------------------------------------------------------- #


def _task_from_args(args) -> str:
    import sys

    if args.task is not None:
        return args.task
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    raise SystemExit("assign needs one of --task, --file, or --stdin")


def _message_from_args(args) -> str:
    import sys

    if args.text is not None:
        return args.text
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    raise SystemExit("say needs one of --text, --file, or --stdin")


def _encouragement_from_args(args) -> str:
    import sys

    if args.text is not None:
        return args.text
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    return DEFAULT_ENCOURAGEMENT


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="danus", description="Control codex workers.")
    sub = p.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("list", help="list all projects + live worker counts")
    li.add_argument("--json", action="store_true")

    n = sub.add_parser("new", help="scaffold a project + worker dirs")
    n.add_argument("project")
    n.add_argument(
        "--roles",
        default=None,
        help=(
            "explicit worker roster; defaults to max:2,high:5 for reasoning-first "
            "and high:3,xhigh:4 for legacy"
        ),
    )
    n.add_argument("--model", default=None)
    n.add_argument(
        "--coordination",
        choices=("legacy", "reasoning-first"),
        default="reasoning-first",
    )
    n.add_argument(
        "--active-explorers",
        type=int,
        choices=(0, 1, 2),
        default=0,
        help=(
            "additional protected reasoning-first explorer lanes (default: 0; "
            "legacy accepts only 0)"
        ),
    )

    a = sub.add_parser("assign", help="write a worker's per-round TASK.md")
    a.add_argument("target", help="<project>/<worker>")
    a.add_argument("--task", default=None)
    a.add_argument("--file", default=None)
    a.add_argument("--stdin", action="store_true")

    say = sub.add_parser("say", help="durably hot-join one worker turn")
    say.add_argument("target", help="<project>/<worker>")
    say.add_argument("--text", default=None)
    say.add_argument("--file", default=None)
    say.add_argument("--stdin", action="store_true")
    say.add_argument("--client-id", default=None, help="stable idempotency key")
    say.add_argument(
        "--fallback",
        choices=("queue", "fail"),
        default="queue",
        help="when the worker has no active turn (default: queue)",
    )

    encourage = sub.add_parser(
        "encourage",
        help="send non-authoritative morale support to this exact live turn",
    )
    encourage.add_argument("target", help="<project>/<worker>")
    encouragement_source = encourage.add_mutually_exclusive_group()
    encouragement_source.add_argument("--text", default=None)
    encouragement_source.add_argument("--file", default=None)
    encouragement_source.add_argument("--stdin", action="store_true")
    encourage.add_argument("--client-id", default=None, help="stable idempotency key")

    msgs = sub.add_parser("messages", help="show human-message delivery receipts")
    msgs.add_argument("target", help="<project> or <project>/<worker>")
    msgs.add_argument("--limit", type=int, default=100)
    msgs.add_argument("--json", action="store_true")

    intr = sub.add_parser(
        "interrupt-turn", help="explicitly interrupt one active app-server turn"
    )
    intr.add_argument("target", help="<project>/<worker>")
    intr.add_argument("--client-id", default=None, help="stable idempotency key")

    abandon = sub.add_parser(
        "abandon-intent",
        help=(
            "explicitly terminalize one outcome-unknown paid intent; worker "
            "must be fail-stopped and exact CAS/risk acknowledgement are required"
        ),
    )
    abandon.add_argument("target", help="<project>/<worker>")
    abandon.add_argument("--thread-id", required=True)
    abandon.add_argument("--client-id", required=True)
    abandon.add_argument(
        "--expected-state",
        required=True,
        choices=("dispatching", "started", "delivery_unknown"),
    )
    abandon.add_argument("--reason", required=True)
    abandon.add_argument(
        "--acknowledge-paid-outcome-unknown",
        action="store_true",
        help="accept that the paid turn may have completed remotely",
    )

    resolve_candidate = sub.add_parser(
        "resolve-candidate",
        help="explicitly resolve a crash/outcome-unknown candidate overlay",
    )
    resolve_candidate.add_argument("project")
    resolve_candidate.add_argument("--receipt", required=True)
    resolve_candidate.add_argument(
        "--outcome",
        required=True,
        choices=("known-no-promotion", "abandon-unknown"),
    )
    resolve_candidate.add_argument(
        "--acknowledge-paid-outcome-unknown",
        action="store_true",
    )

    resolve_recommendation = sub.add_parser(
        "resolve-recommendation",
        help="owner-resolve one terminal reviewed advisor recommendation",
    )
    resolve_recommendation.add_argument("project")
    resolve_recommendation.add_argument("--recommendation-id", required=True)
    resolve_recommendation.add_argument(
        "--resolution",
        required=True,
        choices=("adopted-master-guidance", "continue-without-advisor"),
    )
    resolve_recommendation.add_argument(
        "--acknowledge-recommendation-id",
        required=True,
        help="must exactly repeat --recommendation-id",
    )
    resolve_recommendation.add_argument(
        "--acknowledge-resume-paid-reasoning",
        action="store_true",
        help="explicitly authorize a fresh reasoning generation after resolution",
    )
    resolve_recommendation.add_argument("--master-guidance-entry-id")

    cancel_prepared = sub.add_parser(
        "cancel-prepared-intent",
        help=(
            "terminalize one exact pre-dispatch intent without paid-risk "
            "acknowledgement; worker must be fail-stopped"
        ),
    )
    cancel_prepared.add_argument("target", help="<project>/<worker>")
    cancel_prepared.add_argument("--thread-id", required=True)
    cancel_prepared.add_argument("--client-id", required=True)
    cancel_prepared.add_argument("--reason", required=True)

    reset = sub.add_parser(
        "reset-thread",
        help="explicitly clear a lost app-server thread mapping (never automatic)",
    )
    reset.add_argument("target", help="<project>/<worker>")
    reset.add_argument("--expected-thread-id", required=True)

    rotate = sub.add_parser(
        "rotate-thread",
        help=(
            "explicitly drop terminal conversation context after a bounded "
            "resume failure (never automatic)"
        ),
    )
    rotate.add_argument("target", help="<project>/<worker>")
    rotate.add_argument("--expected-thread-id", required=True)
    rotate.add_argument("--reason", required=True)

    f = sub.add_parser(
        "finalize",
        help="record the finalized target fact_id(s) in "
        "a paper's TARGET.md (write-paper reads this)",
    )
    f.add_argument("project")
    f.add_argument(
        "--paper",
        default=None,
        help="the paper_id (multiple papers per project). Default / 'main' "
        "→ legacy <project>/TARGET.md; else "
        "<project>/papers/<paper_id>/TARGET.md",
    )
    f.add_argument(
        "fact_ids",
        nargs="*",
        help="the target fact id(s); omit to print candidate terminal facts",
    )

    s = sub.add_parser("start", help="launch worker loop(s)")
    s.add_argument("target", help="<project> or <project>/<worker>")

    st = sub.add_parser("status", help="liveness + progress")
    st.add_argument("target", help="<project> or <project>/<worker>")
    st.add_argument("--json", action="store_true")

    sp = sub.add_parser("stop", help="stop worker loop(s)")
    sp.add_argument("target", help="<project> or <project>/<worker>")
    sp.add_argument(
        "--force",
        action="store_true",
        help=(
            "durably request cooperative active-turn/owned-child interruption "
            "(else finish current round); never externally signals a numeric PID"
        ),
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list":
        rows = do_list()
        print(
            json.dumps(rows, ensure_ascii=False, indent=2)
            if args.json
            else _fmt_list(rows)
        )
    elif args.cmd == "new":
        r = do_new(
            args.project,
            roles=args.roles,
            model=args.model,
            coordination=args.coordination,
            active_explorers=args.active_explorers,
        )
        print(
            f"created {args.project} with {len(r['workers'])} workers: "
            f"{', '.join(r['workers'])}\n  {r['project_dir']}"
        )
    elif args.cmd == "assign":
        r = do_assign(args.target, _task_from_args(args))
        detail = (
            f"; staged generation {r['task_generation']} task_sha256={r['task_sha256']}"
            if r.get("generation_staged")
            else (
                "; dormant observer projection only"
                if r.get("assignment_scope") == "dormant_observer_projection"
                else ""
            )
        )
        print(f"assigned {r['worker']} -> {r['task_file']}{detail}")
    elif args.cmd == "say":
        r = do_say(
            args.target,
            _message_from_args(args),
            client_id=args.client_id,
            fallback=args.fallback,
        )
        print(f"{r['message_id']}: {r['state']} -> {r['target']}")
    elif args.cmd == "encourage":
        r = do_encourage(
            args.target,
            _encouragement_from_args(args),
            client_id=args.client_id,
        )
        print(
            f"{r['message_id']}: {r['state']} -> {r['target']} "
            f"(non-authoritative encouragement for {r['expected_turn_id']})"
        )
    elif args.cmd == "messages":
        rows = do_messages(args.target, limit=args.limit)
        print(
            json.dumps(rows, ensure_ascii=False, indent=2)
            if args.json
            else _fmt_messages(rows)
        )
    elif args.cmd == "interrupt-turn":
        r = do_interrupt_turn(args.target, client_id=args.client_id)
        print(f"{r['message_id']}: {r['state']} -> {r['target']} (interrupt)")
    elif args.cmd == "abandon-intent":
        r = do_abandon_intent(
            args.target,
            thread_id=args.thread_id,
            client_id=args.client_id,
            expected_state=args.expected_state,
            reason=args.reason,
            acknowledge_paid_outcome_unknown=(args.acknowledge_paid_outcome_unknown),
        )
        print(
            f"{r['target']}: abandoned outcome-unknown paid intent "
            f"{r['client_id']} ({r['prior_state']} -> {r['terminal_status']}); "
            "reset or rotate the thread before restarting"
        )
    elif args.cmd == "resolve-candidate":
        r = do_resolve_candidate(
            args.project,
            args.receipt,
            outcome=args.outcome,
            acknowledge_paid_outcome_unknown=(args.acknowledge_paid_outcome_unknown),
        )
        print(
            f"{args.project}: owner-resolved candidate "
            f"{r['candidate_receipt_id']} as {r['owner_resolution']}"
        )
    elif args.cmd == "resolve-recommendation":
        r = do_resolve_recommendation(
            args.project,
            args.recommendation_id,
            resolution=args.resolution,
            acknowledge_recommendation_id=args.acknowledge_recommendation_id,
            acknowledge_resume_paid_reasoning=(args.acknowledge_resume_paid_reasoning),
            master_guidance_entry_id=args.master_guidance_entry_id,
        )
        print(
            f"{args.project}: owner-resolved recommendation "
            f"{r['recommendation_id']} as {r['resolution']}"
        )
    elif args.cmd == "cancel-prepared-intent":
        r = do_cancel_prepared_intent(
            args.target,
            thread_id=args.thread_id,
            client_id=args.client_id,
            reason=args.reason,
        )
        print(
            f"{r['target']}: cancelled unspent prepared intent "
            f"{r['client_id']} ({r['terminal_status']}); reset or rotate the "
            "thread before restarting"
        )
    elif args.cmd == "reset-thread":
        r = do_reset_thread(args.target, expected_thread_id=args.expected_thread_id)
        print(f"{r['target']}: cleared lost app-server thread {r['cleared_thread_id']}")
    elif args.cmd == "rotate-thread":
        r = do_rotate_thread(
            args.target,
            expected_thread_id=args.expected_thread_id,
            reason=args.reason,
        )
        print(
            f"{r['target']}: rotated terminal app-server thread "
            f"{r['rotated_thread_id']}; persisted research memory was retained"
        )
    elif args.cmd == "finalize":
        r = do_finalize(args.project, args.fact_ids, paper_id=args.paper)
        paper_note = f" (paper {args.paper})" if args.paper else ""
        paper_flag = f" --paper {args.paper}" if args.paper else ""
        if "suggested" in r:
            sug = r["suggested"]
            if sug:
                print(
                    f"no fact_id given — candidate target facts for {r['project']}{paper_note} "
                    f"(terminal facts; nothing depends on them):"
                )
                for fid in sug:
                    print(f"  {fid}")
                print(
                    f"\nrun: danus finalize {r['project']}{paper_flag} <fact_id> [<fact_id> ...] to record"
                )
            else:
                print(
                    f"no candidate terminal facts in {r['project']} "
                    f"(is the fact graph empty?); nothing recorded"
                )
        else:
            print(
                f"finalized target for {r['project']}{paper_note}: {', '.join(r['target_fact_ids'])}\n"
                f"  wrote {r['target_file']}"
            )
    elif args.cmd == "start":
        for r in do_start(args.target):
            print(f"{r['worker']}: {r['result']}")
    elif args.cmd == "status":
        rows = do_status(args.target)
        print(
            json.dumps(rows, ensure_ascii=False, indent=2)
            if args.json
            else _fmt_status(rows)
        )
    elif args.cmd == "stop":
        for r in do_stop(args.target, force=args.force):
            print(f"{r['worker']}: {r['result']}")
    return 0
