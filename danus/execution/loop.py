"""The per-worker autonomous outer loop — the round driver.

Launched detached by ``danus start`` (``python -m danus.execution <worker_dir>``).
Self-contained. Each round runs ONE ``codex exec`` session
whose internal control loop (worker.md + the worker skills) drives toward a full
verified result — a round is *continue solving from persisted memory*, NOT one
increment. The round ends when codex's session ends (its stopping rule, the
per-round hard timeout, or it bails); the loop then relaunches a fresh session
that resumes from memory. Stops on the ``.stop`` flag (graceful, at a round
boundary), the project deadline, or a round backstop.

The worker gateway is passed to every ``codex exec`` as one complete inline
``mcp_servers.danus={...}`` object. The scaffolded ``.codex/config.toml`` remains
useful for interactive inspection, but production execution never depends on
Codex discovering project-local configuration.

Config:
  - codex binary resolved via the shared ``danus.codex`` launcher
    (``DANUS_CODEX_BIN`` / ``CODEX_BIN`` alias / PATH);
  - all config read at CALL time from env (matches core/gateway/verify).

Env (all optional; tests inject these):
  DANUS_CODEX_BIN            codex binary (default "codex")
  DANUS_ROUND_BEAT           seconds to sleep between rounds (default 5)
  DANUS_ROUND_HARD_TIMEOUT   per-round hard timeout, seconds (default 14400 = 4h)
  DANUS_MAX_ROUNDS           round backstop, 0 = unlimited (default 0)
  DANUS_MAX_CONSEC_FAILURES  bail after this many consecutive failed rounds (default 5)
  DANUS_WORKER_TRANSPORT     exec (compatibility default) or app-server (hot-join)
"""

from __future__ import annotations

import json
import hashlib
import fcntl
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import layout as L
from . import scaffold
from danus import codex
from danus.gateway_runtime import GatewayRuntimeUnavailable, require_gateway_runtime
from danus.hotjoin import (
    AppServerClient,
    AppServerClosed,
    HotJoinBroker,
    HotJoinError,
    HotJoinStore,
    MAX_ROUND_AUDIT_BYTES,
    OwnedChildHostLost,
    ProtocolError,
    RpcError,
    app_server_argv,
    in_progress_turn_id,
    message_turn,
    preflight_app_server,
    resolved_executable,
    turn_snapshot,
)
from danus.redaction import redact_external_error
from danus.owned_child import (
    owned_child_exited_no_reap,
    owned_child_returncode,
    spawn_owned_child,
    stop_owned_child,
)

_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_ROUND_AUDIT_ITEM_BYTES = 256_000
_AUDIT_MARKER_RESERVE_BYTES = 4096
POST_TERMINAL_SETTLE_SECONDS = 0.25
APP_SERVER_PROTOCOL_FAILURE_RC = 123
APP_SERVER_MODEL_REROUTED_RC = 125
WORKER_STOP_REQUESTED_RC = 130
THREAD_HISTORY_OVERSIZE_CODE = "thread_history_exceeds_transport_limit"
MAX_EXEC_LOG_EVENT_BYTES = 8 * 1024 * 1024


def _bounded_protocol_identity(value: str, label: str) -> str:
    """Validate exact app-server identity bytes without rewriting the key."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProtocolError(f"{label} is not valid UTF-8") from exc
    if not value or len(encoded) > 512:
        raise ProtocolError(f"{label} is empty or exceeds hard limit")
    return value


# --- the per-round prompt (continuation semantics; see worker.md) ----------- #

def kickoff(project: str, worker: str) -> str:
    return (
        f"You are worker '{worker}' on project '{project}'. Continue solving the "
        f"problem (this is a continuation round, not a fresh start).\n"
        f"1. Read TASK.md — your current assignment (which direction/subgoal is yours).\n"
        f"2. Follow AGENTS.md (worker.md) exactly — your standing contract (the adaptive "
        f"control loop, memory discipline, the fact_submit gate). Drive toward a full "
        f"verified result.\n"
        f"3. Resume from state: gm_search relevant findings + dead ends, read the fact "
        f"graph and the latest master_guidance — DO NOT restart from zero; build on what "
        f"is already there.\n"
        f"4. Keep going: assess -> pick skills adaptively -> act -> persist, repeatedly. "
        f"An open problem is not a reason to stop. Do NOT finalize prematurely.\n"
        f"5. Persist as you go: rough progress to local memory; shareable findings via "
        f"gm_add; any verified result via fact_submit."
    )


# --- config (read at call time) -------------------------------------------- #

# codex binary + model/effort defaults are resolved via the shared danus.codex
# launcher (DANUS_CODEX_BIN / DANUS_CODEX_MODEL / DANUS_CODEX_EFFORT).


# --- small helpers --------------------------------------------------------- #

def _read_role(wl: L.WorkerLayout, *, protected: bool = False) -> dict:
    if protected:
        try:
            metadata = json.loads(_read_regular_text(wl.project_dir / "project.json"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, HotJoinError) as exc:
            raise HotJoinError(f"protected project role metadata unavailable: {exc}") from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("model"), str):
            raise HotJoinError("protected project role metadata is malformed")
        roles = metadata.get("roles")
        workers = metadata.get("workers")
        if not isinstance(roles, str) or not isinstance(workers, list):
            raise HotJoinError("protected project roster metadata is malformed")
        role_map = dict(L.parse_roles(roles))
        base = role_map.get(wl.name)
        if base is None or wl.name not in workers:
            raise HotJoinError("worker is absent from the protected project roster")
        return {
            "MODEL": metadata["model"],
            "REASONING_EFFORT": base,
            "ROLE": base,
            "DANUS_AUTHOR": wl.name,
        }
    out = {"MODEL": codex.model(),
           "REASONING_EFFORT": "high", "ROLE": "high", "DANUS_AUTHOR": wl.name}
    rp = wl.role
    try:
        role_text = _read_regular_text(rp)
    except FileNotFoundError:
        role_text = ""
    if role_text:
        for line in role_text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _atomic_write_real_parent(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Atomic write pinned to a real directory without following temp symlinks."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise HotJoinError("host lacks no-follow directory operations")
    parent_fd = os.open(
        str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    temp_name = f".{path.name}.danus-{os.getpid()}-{os.urandom(12).hex()}"
    temp_fd: Optional[int] = None
    try:
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            raise HotJoinError("write parent is not a real directory")
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        payload = text.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(temp_fd, payload[offset:])
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(
            temp_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _read_regular_text(path: Path, *, max_bytes: int = 1_000_000) -> str:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HotJoinError("refusing non-regular supervisor state file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise HotJoinError("supervisor state file exceeds hard limit")
        return payload.decode("utf-8", errors="strict")
    finally:
        os.close(fd)


def _ensure_real_dir(path: Path, *, mode: int = 0o700) -> None:
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HotJoinError(f"refusing unsafe supervisor directory: {path.name}")
    os.chmod(path, mode)


def _refresh_workspace_symlink(link: Path, target: Path) -> None:
    try:
        info = os.lstat(link)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            raise HotJoinError(f"model workspace path is an unexpected directory: {link.name}")
        os.unlink(link)
    os.symlink(str(target), str(link), target_is_directory=target.is_dir())


def _prepare_model_workspace(wl: L.WorkerLayout) -> Path:
    """Refresh the model-only cwd while keeping host controls in its parent."""
    workspace = wl.dir / "model_workspace"
    _ensure_real_dir(workspace)
    _ensure_real_dir(wl.local_memory)
    _atomic_write_real_parent(
        workspace / L.TASK_FILE,
        _read_regular_text(wl.task, max_bytes=1_000_000),
    )
    _refresh_workspace_symlink(workspace / "AGENTS.md", L.worker_md())
    agents = workspace / ".agents"
    _ensure_real_dir(agents)
    _refresh_workspace_symlink(agents / "skills", L.worker_skills_dir())
    _refresh_workspace_symlink(workspace / "local_memory", wl.local_memory)
    return workspace


def _best_effort_worker_projection(path: Path, text: str) -> None:
    """Write non-authoritative worker output only when its parent is still safe."""
    try:
        _atomic_write_real_parent(path, text)
    except (HotJoinError, OSError):
        pass


def _open_round_log(path: Path):
    """Open one host log without following/truncating an unsafe filesystem node."""
    _ensure_real_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise HotJoinError("refusing unsafe worker round log")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def write_status(wl: L.WorkerLayout, **fields) -> None:
    """Atomic status write (so `danus status` never reads a half-written file)."""
    path = wl.status
    cur = {}
    try:
        current = _read_regular_text(path)
    except FileNotFoundError:
        current = None
    except (HotJoinError, UnicodeDecodeError, OSError):
        current = None
    if current is not None:
        try:
            loaded = json.loads(current)
            cur = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            cur = {}
    cur.update(fields)
    cur["worker"] = wl.name
    cur["pid"] = os.getpid()
    cur["updated_at"] = time.time()
    _atomic_write_real_parent(path, json.dumps(cur, ensure_ascii=False, indent=2))


def _read_status_snapshot(wl: L.WorkerLayout) -> dict:
    """Return one bounded supervisor status snapshot, or an empty mapping.

    Status is an operator-facing projection rather than lifecycle authority.  A
    malformed or unavailable projection must therefore never authorize a paid
    retry; callers use this helper only to preserve already-written diagnostic
    fields when layering the outer-loop outcome.
    """
    try:
        value = json.loads(_read_regular_text(wl.status, max_bytes=1_000_000))
    except (
        FileNotFoundError,
        HotJoinError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
    ):
        return {}
    return value if isinstance(value, dict) else {}


def _deadline_passed(project_dir: Path) -> bool:
    f = project_dir / L.DEADLINE_FILE
    if not f.exists():
        return False
    try:
        return time.time() >= float(f.read_text().strip())
    except (ValueError, OSError):
        return False


def _fact_submit_summary(item: dict) -> Optional[dict]:
    """Return a bounded semantic summary for one explicit Danus submit item."""
    if item.get("server") != "danus" or item.get("tool") != "fact_submit":
        return None
    result = item.get("result")
    value = result.get("structuredContent") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return None
    fact_id = value.get("fact_id")
    accepted = value.get("accepted")
    if fact_id is not None and not (
        isinstance(fact_id, str) and _FACT_ID_RE.fullmatch(fact_id)
    ):
        return None
    if not isinstance(accepted, bool):
        return None
    # ``accepted`` is the legacy verifier-verdict field. Promotion is
    # end-to-end and therefore requires a concrete fact id even if an older
    # gateway response does not yet carry the explicit boolean.
    has_reported_promoted = "promoted" in value
    reported_promoted = value.get("promoted")
    if not has_reported_promoted:
        promoted = fact_id is not None
    elif isinstance(reported_promoted, bool):
        promoted = reported_promoted and fact_id is not None
    elif reported_promoted is None and fact_id is None:
        promoted = None
    else:
        promoted = False
    verification_verdict = value.get("verification_verdict")
    if not isinstance(verification_verdict, str) or (
        verification_verdict not in {"correct", "wrong"}
    ):
        legacy_verdict = value.get("verdict")
        verification_verdict = (
            legacy_verdict
            if isinstance(legacy_verdict, str)
            and legacy_verdict in {"correct", "wrong"}
            else None
        )
    expected_status = (
        "promotion_unknown"
        if promoted is None
        else "promoted"
        if promoted
        else "verified_not_promoted"
        if accepted
        else "rejected"
        if verification_verdict == "wrong"
        else "error"
    )
    reported_status = value.get("submission_status")
    submission_status = (
        reported_status if reported_status == expected_status else expected_status
    )
    summary = {
        "accepted": accepted,
        "promoted": promoted,
        "submission_status": submission_status,
        "verification_verdict": verification_verdict,
        "fact_id": fact_id,
    }
    for key in ("verdict", "adaptive_rounds", "verification_calls"):
        if isinstance(value.get(key), (str, int)) and not isinstance(
            value.get(key), bool
        ):
            summary[key] = value[key]
    expanded = value.get("expanded_proof_ids")
    if isinstance(expanded, list) and all(
        isinstance(entry, str) and _FACT_ID_RE.fullmatch(entry)
        for entry in expanded
    ):
        summary["expanded_proof_ids"] = list(expanded)
    return summary


def _last_promoted_fact_id(text: str) -> Optional[str]:
    """Read only explicit completed ``fact_submit`` results from JSONL text.

    Arbitrary ``fact_id`` text is not evidence of publication: exec logs can
    contain fact-context results, fact frontmatter, diffs, and agent prose.  Both
    the Codex JSON event shape and Danus's protected app-server audit projection
    carry a typed completed-item envelope, which is the only accepted source.
    """
    last_fact_id: Optional[str] = None
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        item: object = None
        if record.get("event") == "item_completed":
            item = record.get("item")
        elif record.get("type") in {"item.completed", "item_completed"}:
            item = record.get("item")
        elif record.get("method") == "item/completed":
            params = record.get("params")
            if isinstance(params, dict):
                item = params.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("server") != "danus" or item.get("tool") != "fact_submit":
            continue
        summary = item.get("fact_submit_result")
        if not isinstance(summary, dict):
            summary = _fact_submit_summary(item)
        if not isinstance(summary, dict) or summary.get("promoted") is not True:
            continue
        fact_id = summary.get("fact_id")
        if isinstance(fact_id, str) and _FACT_ID_RE.fullmatch(fact_id):
            last_fact_id = fact_id
    return last_fact_id


def _parse_last_fact_id(
    log_path: Path, *, worker: Optional[L.WorkerLayout] = None
) -> Optional[str]:
    last_fact_id: Optional[str] = None
    oversize_lines = 0
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(log_path, flags)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            os.close(fd)
            return None
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            while True:
                line = handle.readline(MAX_EXEC_LOG_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_EXEC_LOG_EVENT_BYTES:
                    oversize_lines += 1
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(MAX_EXEC_LOG_EVENT_BYTES + 1)
                    continue
                try:
                    text = line.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    continue
                observed = _last_promoted_fact_id(text)
                if observed is not None:
                    last_fact_id = observed
    except OSError:
        return None
    if oversize_lines and worker is not None:
        write_status(
            worker,
            exec_log_parse_warning=(
                f"skipped {oversize_lines} JSONL event(s) exceeding "
                f"{MAX_EXEC_LOG_EVENT_BYTES} bytes"
            ),
        )
    return last_fact_id


def _canonical_app_server_fact_id(project_dir: Path, worker: str) -> Optional[str]:
    try:
        audit = HotJoinStore(project_dir).latest_round_audit(worker)
    except (HotJoinError, OSError):
        return None
    if audit is None:
        return None
    return _last_promoted_fact_id(str(audit["payload"]))


def _worker_mcp_config_arg(wl: L.WorkerLayout) -> str:
    """Return one complete Codex CLI override for the worker gateway.

    Keep the whole server object in one override: a collection of partial
    ``mcp_servers.danus.*`` overrides can be merged differently across Codex
    releases. In particular, do not rely on ``wl/.codex/config.toml`` being
    auto-loaded. ``sys.executable`` is intentionally used byte-for-byte (not
    ``Path.resolve()``) so the MCP child is bound to the interpreter running this
    loop, including a virtual-environment launcher path.
    """
    return (
        "mcp_servers={danus={command="
        + json.dumps(sys.executable, ensure_ascii=False)
        + ',args=["-I","-B","-m","danus.gateway"],env={DANUS_PROJECT_DIR='
        + json.dumps(str(wl.project_dir), ensure_ascii=False)
        + ",DANUS_AUTHOR="
        + json.dumps(wl.name, ensure_ascii=False)
        + ',DANUS_ROLE="worker",DANUS_HOTJOIN_ENABLED="1",DANUS_HOTJOIN_TARGET='
        + json.dumps(wl.name, ensure_ascii=False)
        + ',DANUS_VERIFY_URL='
        + json.dumps(scaffold._verify_url(), ensure_ascii=False)
        + "},tool_timeout_sec=3600,"
        + 'default_tools_approval_mode="approve",required=true}}'
    )


# --- one round ------------------------------------------------------------- #

class _Child:
    """Retains the paid-child host so termination can revoke its lease."""
    proc: "subprocess.Popen | None" = None


def _force_stop_requested(wl: L.WorkerLayout) -> bool:
    try:
        return _read_regular_text(wl.stop, max_bytes=32).strip() == "force"
    except FileNotFoundError:
        return False
    except (HotJoinError, OSError, UnicodeDecodeError):
        # An unsafe/unreadable control file must never be interpreted as
        # permission to keep spending through an explicit force-stop boundary.
        return True


def _acquire_paid_authority(wl: L.WorkerLayout) -> int:
    """Acquire the worker's non-overlap fence for one paid subprocess host."""
    path = wl.dir / ".paid.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HotJoinError("worker paid-authority lock is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HotJoinError(
                "prior paid subprocess cleanup is still in progress"
            ) from exc
        return fd
    except BaseException:
        os.close(fd)
        raise


def _terminate_owned_child(
    proc: subprocess.Popen,
    *,
    grace: float = 10.0,
    leader_exited: bool = False,
) -> None:
    """Ask the retained host to stop/reap its paid child process group."""
    if proc.returncode is not None:
        return
    # ``leader_exited`` now refers to the host.  The host has already swept
    # and reaped its own paid child before exposing a terminal state.
    del leader_exited
    stop_owned_child(proc, grace=max(5.0, grace + 4.0))


def _owned_child_exited_no_reap(proc: subprocess.Popen) -> bool:
    """Poll the retained host without releasing its child-cleanup fence."""
    return owned_child_exited_no_reap(proc)


def run_round(wl: L.WorkerLayout, role: dict, prompt: str, log_path: Path,
              hard_timeout: int) -> int:
    """Exec one ``codex exec`` continuation session. Returns codex's rc, 124 on
    hard-timeout (terminate → wait 10s → kill), or 127 if the codex binary is
    missing."""
    wdir = wl.dir
    try:
        require_gateway_runtime()
    except GatewayRuntimeUnavailable as exc:
        write_status(
            wl,
            attempt_phase="gateway_preflight",
            attempt_dispatch_state="none",
            attempt_failure_code="gateway_runtime_unavailable",
            attempt_failure=redact_external_error(exc),
        )
        try:
            with _open_round_log(log_path) as logf:
                logf.write(
                    "[worker_loop] gateway runtime unavailable: "
                    f"{redact_external_error(exc)}\n"
                )
        except (HotJoinError, OSError):
            pass
        return 126
    codex_bin = codex.resolve_bin()
    cmd = codex.exec_cmd(
        codex_bin, role["MODEL"], role["REASONING_EFFORT"],
        "-C", str(wdir),
        # Inject the complete gateway object explicitly. Codex does not
        # consistently auto-load ``<worker>/.codex/config.toml`` for exec/MCP
        # discovery, so that file cannot be the production authority.
        "--config", _worker_mcp_config_arg(wl),
        # on an install without .git (tarball download), codex's
        # trusted-directory check refuses to run the worker round
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        # Status attribution consumes only typed completed fact_submit events;
        # free-form terminal text, fact context, and diffs are never scraped.
        "--json",
        prompt,
    )
    try:
        log_context = _open_round_log(log_path)
    except (HotJoinError, OSError):
        write_status(
            wl,
            attempt_phase="round_log_open",
            attempt_dispatch_state="none",
            attempt_failure_code="unsafe_round_log",
            attempt_failure="worker round log could not be opened safely",
        )
        return 126
    with log_context as logf:
        paid_authority_fd = -1
        try:
            paid_authority_fd = _acquire_paid_authority(wl)
        except (HotJoinError, OSError) as exc:
            write_status(
                wl,
                attempt_phase="paid_authority",
                attempt_dispatch_state="none",
                attempt_failure_code="prior_paid_cleanup_in_progress",
                attempt_failure=redact_external_error(exc),
            )
            logf.write(f"[worker_loop] paid launch fail-stopped: {exc}\n")
            return 126
        try:
            _Child.proc = spawn_owned_child(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(wdir),
                env=codex.subprocess_env(codex_bin),
                popen=subprocess.Popen,
                hold_fds=(paid_authority_fd,),
            )
        except FileNotFoundError:
            if paid_authority_fd >= 0:
                os.close(paid_authority_fd)
            logf.write(f"[worker_loop] codex binary not found: {cmd[0]}\n")
            return 127
        except BaseException:
            if paid_authority_fd >= 0:
                os.close(paid_authority_fd)
            raise
        try:
            deadline = time.monotonic() + hard_timeout if hard_timeout > 0 else None
            while True:
                if _force_stop_requested(wl):
                    _terminate_owned_child(_Child.proc)
                    logf.write("\n[worker_loop] cooperative owner stop requested\n")
                    return WORKER_STOP_REQUESTED_RC
                remaining = (
                    0.25
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if deadline is not None and remaining <= 0:
                    _terminate_owned_child(_Child.proc)
                    logf.write(
                        f"\n[worker_loop] round hard-timeout after {hard_timeout}s\n"
                    )
                    return 124
                if _owned_child_exited_no_reap(_Child.proc):
                    # Codex may exit while an MCP helper/grandchild remains.
                    # Sweep the dedicated group while the unreaped leader still
                    # fences its PID/PGID, then reap and return the real rc.
                    _terminate_owned_child(_Child.proc, leader_exited=True)
                    child_rc = owned_child_returncode(_Child.proc)
                    if child_rc is None:
                        raise RuntimeError(
                            "owned-child host omitted the Codex return code"
                        )
                    return child_rc
                time.sleep(min(0.25, remaining))
        finally:
            proc = _Child.proc
            try:
                if proc is not None and proc.returncode is None:
                    # No exception after spawn may discard the liveness lease
                    # or retained host handle while paid work can still run.
                    _terminate_owned_child(proc)
            finally:
                cleanup_complete = proc is None or proc.returncode is not None
                if cleanup_complete:
                    _Child.proc = None
                    if paid_authority_fd >= 0:
                        os.close(paid_authority_fd)


def _attest_bounded_thread_state(
    response: object, *, expected_thread_id: str
) -> str:
    """Validate ``thread/read(includeTurns=false)`` before a terminal resume.

    Codex 0.147's generated schema guarantees that this RPC returns the Thread
    metadata with an empty ``turns`` list.  It is the only bounded way to check
    whether a persisted thread is inactive: ``thread/resume`` necessarily
    returns every historical turn and may exceed the JSONL transport ceiling
    after a multi-hour turn.
    """
    if not isinstance(response, dict):
        raise ProtocolError("bounded thread/read response is not an object")
    thread = response.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != expected_thread_id:
        raise ProtocolError("bounded thread/read returned a different thread")
    turns = thread.get("turns")
    if turns != []:
        raise ProtocolError("thread/read(includeTurns=false) returned turn history")
    status = thread.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("type"), str):
        raise ProtocolError("bounded thread/read omitted thread status")
    status_type = str(status["type"])
    if status_type == "active":
        raise ProtocolError(
            "persisted thread is active before the next round; refusing a new paid turn"
        )
    if status_type == "systemError":
        raise ProtocolError("persisted thread reports systemError before resume")
    if status_type not in {"idle", "notLoaded"}:
        raise ProtocolError("bounded thread/read returned an unknown thread status")
    return status_type


def _app_server_failure_code(exc: BaseException, *, phase: str) -> str:
    if isinstance(exc, OwnedChildHostLost) or phase == "app_server_host_lost":
        return "app_server_host_lost"
    if (
        phase == "thread_resume"
        and "app-server JSONL line exceeds hard limit" in str(exc)
    ):
        return THREAD_HISTORY_OVERSIZE_CODE
    return "app_server_failure"


def _build_app_server_audit(
    client: AppServerClient,
    *,
    thread_id: str,
    turn_id: str,
    terminal: Optional[dict],
    requested_model: str,
    requested_effort: str,
    actual_model: Optional[str],
    thread_reasoning_effort: Optional[str],
    failure: Optional[str] = None,
    reroute_snapshot: Optional[dict] = None,
    post_terminal_settle_bound_ms: Optional[int] = None,
    token_usage_finality_override: Optional[str] = None,
) -> str:
    """Write bounded service metadata and trusted model/tool completions only.

    Human userMessage items are deliberately excluded so the durable research
    log cannot become a second copy of the intervention transcript.
    """
    usage = client.token_usage(thread_id, turn_id)
    reroutes = (
        reroute_snapshot
        if reroute_snapshot is not None
        else client.model_reroutes(thread_id, turn_id)
    )
    reroute_observed = reroutes.get("observed")
    if reroute_observed is True:
        reroute_observation = "observed_live_stream"
    elif reroute_observed is False:
        reroute_observation = "not_observed_live_stream"
    else:
        reroute_observation = str(reroutes.get("observation", "unknown"))
    if token_usage_finality_override is not None:
        token_usage_finality = token_usage_finality_override
    elif usage is not None:
        token_usage_finality = "observed_not_schema_attested_final"
    elif post_terminal_settle_bound_ms is not None:
        token_usage_finality = "not_observed_after_bounded_post_terminal_settle"
    else:
        token_usage_finality = "not_observed"

    def bounded_scalar(
        value: object, *, limit: int = 2048, redact: bool = True
    ) -> object:
        if value is None:
            return None
        text = redact_external_error(value) if redact else str(value)
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text
        return (
            f"[omitted bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}]"
        )

    def bounded_json(value: object, *, limit: int = 64 * 1024) -> object:
        if value is None:
            return None
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(raw) <= limit:
            return value
        return {
            "omitted": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    records: list[bytes] = []
    retained_bytes = 0
    omitted_items = 0
    omitted_bytes = 0
    omitted_hash = hashlib.sha256()

    def encode(record: dict) -> bytes:
        return (
            json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")

    def append_record(record: dict, *, item_projection: bool = False) -> None:
        nonlocal retained_bytes, omitted_items, omitted_bytes
        line = encode(record)
        if item_projection and (
            len(line) > MAX_ROUND_AUDIT_ITEM_BYTES
            or retained_bytes + len(line)
            > MAX_ROUND_AUDIT_BYTES - _AUDIT_MARKER_RESERVE_BYTES
        ):
            omitted_items += 1
            omitted_bytes += len(line)
            omitted_hash.update(len(line).to_bytes(8, "big"))
            omitted_hash.update(line)
            return
        if retained_bytes + len(line) > MAX_ROUND_AUDIT_BYTES:
            raise ProtocolError("round audit metadata exceeds hard limit")
        records.append(line)
        retained_bytes += len(line)

    append_record(
        {
            "event": "turn_completed",
            # Protocol identities are exact CAS/provenance keys. They are
            # strictly type/length validated at ingress and never redacted.
            "thread_id": bounded_scalar(thread_id, redact=False),
            "turn_id": bounded_scalar(turn_id, redact=False),
            "terminal_observed": terminal is not None,
            "status": bounded_scalar(
                terminal.get("status") if terminal is not None else "unknown"
            ),
            "duration_ms": bounded_json(
                terminal.get("durationMs") if terminal is not None else None,
                limit=128,
            ),
            "token_usage": bounded_json(usage),
            "token_usage_observed": usage is not None,
            "token_usage_finality": token_usage_finality,
            "post_terminal_settle_bound_ms": post_terminal_settle_bound_ms,
            "requested_model": bounded_scalar(requested_model),
            "requested_effort": bounded_scalar(requested_effort),
            "actual_model": bounded_scalar(actual_model),
            "thread_reasoning_effort": bounded_scalar(thread_reasoning_effort),
            # ``None`` is intentional: thread/read does not replay historical
            # model/rerouted notifications, so absence after adapter recovery
            # cannot honestly be represented as False.
            "model_rerouted": (
                True
                if reroute_observed is True
                else False
                if reroute_observed is False
                else None
            ),
            "model_reroute_observation": bounded_scalar(reroute_observation),
            "model_reroutes": bounded_json(reroutes),
            "failure": bounded_scalar(failure, limit=4096),
        }
    )

    def project_item(item: object) -> Optional[dict]:
        if not isinstance(item, dict) or item.get("type") not in {
            "agentMessage",
            "mcpToolCall",
        }:
            return None
        if item.get("type") == "mcpToolCall":
            projected = {
                key: (
                    redact_external_error(item[key])
                    if isinstance(item[key], str)
                    else item[key]
                )
                for key in ("type", "id", "server", "tool", "name", "status")
                if key in item and isinstance(item[key], (str, int, float, bool))
            }
            summary = _fact_submit_summary(item)
            if summary is not None:
                projected["fact_submit_result"] = summary
            return projected
        return item

    projected_ids: set[str] = set()
    for note in client.notifications():
        if note.get("method") != "item/completed":
            continue
        params = note.get("params", {})
        if (
            not isinstance(params, dict)
            or params.get("threadId") != thread_id
            or params.get("turnId") != turn_id
        ):
            continue
        projected = project_item(params.get("item", {}))
        if projected is None:
            continue
        if isinstance(projected.get("id"), str):
            projected_ids.add(projected["id"])
        # Bound individual projections; raw command/model streams are never
        # persisted here. Agent messages remain research output and may quote
        # owner guidance, so the log is not a confidentiality boundary.
        append_record(
            {"event": "item_completed", "item": projected},
            item_projection=True,
        )

    # Crash recovery may obtain a terminal turn from thread/read without replayed
    # item notifications. Retain the same bounded projection from that turn.
    if terminal is not None and isinstance(terminal.get("items"), list):
        for raw_item in terminal["items"]:
            projected = project_item(raw_item)
            if projected is None or projected.get("id") in projected_ids:
                continue
            append_record(
                {"event": "item_completed", "item": projected},
                item_projection=True,
            )

    omissions = client.notification_omissions()
    if omissions["count"]:
        append_record({"event": "notifications_omitted", **omissions})
    if omitted_items:
        append_record(
            {
                "event": "audit_items_omitted",
                "count": omitted_items,
                "bytes": omitted_bytes,
                "sha256": omitted_hash.hexdigest(),
            }
        )
    return b"".join(records).decode("utf-8")


def _attest_thread_runtime(
    response: object,
    *,
    worker_dir: Path,
    requested_model: str,
    expected_thread_id: Optional[str] = None,
) -> tuple[str, str, Optional[str]]:
    """Validate the server's effective model, cwd, approval, and sandbox."""
    if not isinstance(response, dict):
        raise ProtocolError("thread start/resume response is not an object")
    thread = response.get("thread")
    if (
        not isinstance(thread, dict)
        or not isinstance(thread.get("id"), str)
        or not thread.get("id")
    ):
        raise ProtocolError("thread start/resume returned no thread id")
    thread_id = thread["id"]
    _bounded_protocol_identity(thread_id, "thread id")
    if expected_thread_id is not None and thread_id != expected_thread_id:
        raise ProtocolError("thread/resume returned a different thread id")
    if response.get("model") != requested_model:
        raise ProtocolError("thread start/resume did not attest the exact model")
    expected_cwd = worker_dir.resolve()

    def canonical(value: object) -> Optional[Path]:
        if not isinstance(value, str) or not os.path.isabs(value):
            return None
        try:
            return Path(value).resolve()
        except (OSError, ValueError):
            # Local protocol data may contain embedded NULs or paths that
            # exceed platform limits. Treat those as failed attestation, not
            # an uncaught worker exception.
            return None

    if canonical(response.get("cwd")) != expected_cwd or canonical(
        thread.get("cwd")
    ) != expected_cwd:
        raise ProtocolError("thread start/resume did not attest the exact worker cwd")
    if response.get("approvalPolicy") != "never":
        raise ProtocolError("thread start/resume weakened approvalPolicy")
    sandbox = response.get("sandbox")
    if (
        not isinstance(sandbox, dict)
        or sandbox.get("type") != "workspaceWrite"
        or sandbox.get("networkAccess") is not False
    ):
        raise ProtocolError("thread start/resume weakened workspace sandbox")
    writable_roots = sandbox.get("writableRoots")
    if not isinstance(writable_roots, list):
        raise ProtocolError("thread sandbox omitted writableRoots attestation")
    for raw_root in writable_roots:
        root = canonical(raw_root)
        if root is None:
            raise ProtocolError("thread sandbox returned a non-absolute writable root")
        try:
            root.relative_to(expected_cwd)
        except ValueError as exc:
            raise ProtocolError("thread sandbox writable root escapes worker cwd") from exc
    runtime_roots = response.get("runtimeWorkspaceRoots", [])
    if not isinstance(runtime_roots, list):
        raise ProtocolError("thread runtimeWorkspaceRoots attestation is malformed")
    for raw_root in runtime_roots:
        root = canonical(raw_root)
        if root is None:
            raise ProtocolError("thread runtime workspace root is not absolute")
        try:
            root.relative_to(expected_cwd)
        except ValueError as exc:
            raise ProtocolError("thread runtime workspace root escapes worker cwd") from exc
    effort = response.get("reasoningEffort")
    return thread_id, requested_model, str(effort) if effort is not None else None


def _model_catalog_entry(
    client: AppServerClient, requested_model: str, requested_effort: str
) -> dict:
    """Zero-spend validation that the exact paid model/effort is advertised."""
    response = client.rpc("model/list", {"includeHidden": True})
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ProtocolError("model/list returned no model catalog")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if requested_model not in {row.get("id"), row.get("model")}:
            continue
        efforts = row.get("supportedReasoningEfforts")
        supported = {
            entry.get("reasoningEffort")
            for entry in efforts
            if isinstance(entry, dict)
        } if isinstance(efforts, list) else set()
        if requested_effort not in supported:
            raise ProtocolError(
                f"model {requested_model} does not advertise effort {requested_effort}"
            )
        return row
    raise ProtocolError(f"model/list does not advertise exact model {requested_model}")


def run_round_app_server(
    wl: L.WorkerLayout,
    role: dict,
    prompt: str,
    log_path: Path,
    hard_timeout: int,
) -> int:
    """Run one worker turn through app-server with durable human hot-join.

    This transport is opt-in via ``DANUS_WORKER_TRANSPORT=app-server`` while the
    legacy ``codex exec`` path remains available for rollback.  No model request
    is made until the local binary's generated protocol schema passes preflight.
    """
    try:
        require_gateway_runtime()
    except GatewayRuntimeUnavailable as exc:
        _best_effort_worker_projection(
            log_path,
            "[worker_loop] gateway runtime unavailable: "
            f"{redact_external_error(exc)}\n",
        )
        return 126
    try:
        codex_bin = resolved_executable(codex.resolve_bin())
    except FileNotFoundError:
        _best_effort_worker_projection(
            log_path, "[worker_loop] codex binary not found\n"
        )
        return 127
    env = codex.subprocess_env(codex_bin)
    try:
        preflight_app_server(codex_bin, env=env)
    except ProtocolError as exc:
        _best_effort_worker_projection(
            log_path,
            "[worker_loop] app-server protocol unavailable: "
            f"{redact_external_error(exc)}\n",
        )
        return 126

    store = HotJoinStore(wl.project_dir)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    pending_intent = store.unfinished_round_intent(wl.name)
    stored_thread = store.thread_id(wl.name)
    if pending_intent is not None:
        expected = {
            "thread_id": stored_thread,
            "prompt_sha256": prompt_sha256,
            "requested_model": role["MODEL"],
            "requested_effort": role["REASONING_EFFORT"],
        }
        if stored_thread is None or any(
            pending_intent.get(key) != value for key, value in expected.items()
        ):
            _best_effort_worker_projection(
                log_path,
                "[worker_loop] unfinished paid-turn intent conflicts with the "
                "current thread, prompt, model, or effort\n",
            )
            return 126
    try:
        model_workspace = _prepare_model_workspace(wl)
    except (HotJoinError, OSError, UnicodeDecodeError) as exc:
        _best_effort_worker_projection(
            log_path,
            "[worker_loop] unsafe model workspace: "
            f"{redact_external_error(exc)}\n",
        )
        return 126
    argv = app_server_argv(codex_bin, _worker_mcp_config_arg(wl))
    try:
        paid_authority_fd = _acquire_paid_authority(wl)
    except (HotJoinError, OSError) as exc:
        write_status(
            wl,
            attempt_phase="paid_authority",
            attempt_dispatch_state="none",
            attempt_failure_code="prior_paid_cleanup_in_progress",
            attempt_failure=redact_external_error(exc),
        )
        _best_effort_worker_projection(
            log_path,
            "[worker_loop] paid launch fail-stopped: "
            f"{redact_external_error(exc)}\n",
        )
        return 126
    # Raw app-server stderr can contain untrusted prompt/model/tool material and
    # has no useful production size bound. Structured lifecycle metadata below
    # is the only persistent audit; stderr is drained directly to DEVNULL.
    try:
        client = AppServerClient(
            argv,
            cwd=model_workspace,
            env=env,
            hold_fds=(paid_authority_fd,),
        )
    except BaseException:
        os.close(paid_authority_fd)
        raise
    broker: Optional[HotJoinBroker] = None
    thread_id = ""
    turn_id = ""
    round_client_id = ""
    actual_model: Optional[str] = None
    thread_reasoning_effort: Optional[str] = None
    terminal: Optional[dict] = None
    requested_model = role["MODEL"]
    requested_effort = role["REASONING_EFFORT"]
    audit_written = False
    round_rejected_for_reroute = False
    attempt_phase = "pre_dispatch"
    attempt_dispatch_state = "none"
    unset = object()

    def mark_attempt(
        *,
        phase: Optional[str] = None,
        dispatch_state: Optional[str] = None,
        failure_code: object = unset,
        failure: object = unset,
    ) -> None:
        nonlocal attempt_phase, attempt_dispatch_state
        if phase is not None:
            attempt_phase = phase
        if dispatch_state is not None:
            attempt_dispatch_state = dispatch_state
        fields: dict[str, object] = {
            "attempt_phase": attempt_phase,
            "attempt_dispatch_state": attempt_dispatch_state,
            "attempt_client_id": round_client_id
            or (
                pending_intent.get("client_id")
                if pending_intent is not None
                else None
            ),
            "attempt_thread_id": thread_id or stored_thread,
            "attempt_turn_id": turn_id
            or (
                pending_intent.get("turn_id")
                if pending_intent is not None
                else None
            ),
        }
        if failure_code is not unset:
            fields["attempt_failure_code"] = failure_code
        if failure is not unset:
            fields["attempt_failure"] = failure
        write_status(wl, **fields)

    mark_attempt(failure_code=None, failure=None)

    def finalize_turn(
        terminal: Optional[dict], *, failure: Optional[str] = None,
        reroute_observation_unknown: bool = False,
        cached_after_transport_loss: Optional[str] = None,
    ) -> bool:
        nonlocal audit_written, round_rejected_for_reroute
        if not thread_id or not round_client_id:
            return round_rejected_for_reroute
        if audit_written:
            return round_rejected_for_reroute
        audit_turn_id = turn_id or "unknown"
        settle_bound_ms: Optional[int] = None
        if terminal is not None and turn_id and cached_after_transport_loss is None:
            client.settle_after_terminal(
                thread_id, turn_id, POST_TERMINAL_SETTLE_SECONDS
            )
            settle_bound_ms = round(POST_TERMINAL_SETTLE_SECONDS * 1000)
        if cached_after_transport_loss is not None:
            reroutes = client.model_reroutes(thread_id, audit_turn_id)
            if reroutes.get("observed") is not True:
                reroutes = {
                    "observed": None,
                    "observation": f"unknown_after_{cached_after_transport_loss}",
                    "events": [],
                    "omitted": reroutes.get(
                        "omitted",
                        {
                            "count": 0,
                            "bytes": 0,
                            "sha256": hashlib.sha256().hexdigest(),
                        },
                    ),
                }
                if terminal is not None:
                    round_rejected_for_reroute = True
                    quarantine_failure = (
                        "model reroute observation unavailable after "
                        f"{cached_after_transport_loss.replace('_', ' ')}; "
                        "round quarantined"
                    )
                    failure = (
                        f"{failure}; {quarantine_failure}"
                        if failure
                        else quarantine_failure
                    )
        elif reroute_observation_unknown:
            # thread/read can recover the paid turn and its terminal items, but
            # the app-server protocol does not replay historical model/rerouted
            # notifications. Quarantine instead of converting unknown to false.
            reroutes = {
                "observed": None,
                "observation": "unknown_after_adapter_interruption",
                "events": [],
                "event_count": 0,
                "omitted_count": 0,
                "omitted_bytes": 0,
                "omitted_sha256": None,
            }
            round_rejected_for_reroute = True
            reroute_failure = (
                "model reroute observation unavailable after adapter interruption; "
                "round quarantined"
            )
            failure = f"{failure}; {reroute_failure}" if failure else reroute_failure
        else:
            reroutes = client.model_reroutes(thread_id, audit_turn_id)
        if reroutes.get("observed") is True:
            round_rejected_for_reroute = True
            reroute_failure = (
                "app-server model/rerouted observed for exact thread/turn; "
                "round rejected"
            )
            failure = f"{failure}; {reroute_failure}" if failure else reroute_failure
        audit_payload = _build_app_server_audit(
            client,
            thread_id=thread_id,
            turn_id=audit_turn_id,
            terminal=terminal,
            requested_model=requested_model,
            requested_effort=requested_effort,
            actual_model=actual_model,
            thread_reasoning_effort=thread_reasoning_effort,
            failure=failure,
            reroute_snapshot=reroutes,
            post_terminal_settle_bound_ms=settle_bound_ms,
            token_usage_finality_override=(
                f"not_attested_after_{cached_after_transport_loss}"
                if cached_after_transport_loss is not None
                else None
            ),
        )
        if not round_client_id:
            raise HotJoinError("cannot persist round audit without a paid-turn intent")
        terminal_status = (
            str(terminal.get("status")) if terminal is not None else "unknown"
        )
        # This SQLite store is outside the worker workspace-write root and is
        # the canonical lifecycle/token audit. Final audit, human-message
        # receipts, and paid-intent state commit in one transaction.
        if terminal is not None:
            if not turn_id:
                raise ProtocolError("terminal audit is missing its turn id")
            audit_event = store.finalize_round(
                round_client_id,
                audit_payload,
                thread_id=thread_id,
                turn_id=turn_id,
                terminal_status=terminal_status,
            )
        else:
            audit_event = store.record_round_attempt_audit(
                round_client_id, audit_payload
            )
        canonical_payload = str(audit_event["payload"])
        try:
            _atomic_write_real_parent(log_path, canonical_payload)
        except (HotJoinError, OSError):
            pass
        canonical_meta = json.loads(canonical_payload.splitlines()[0])
        write_status(
            wl,
            active_turn_id=None,
            last_turn_id=turn_id or None,
            last_turn_status=terminal_status,
            last_turn_token_usage=canonical_meta.get("token_usage"),
            last_turn_token_usage_observed=canonical_meta.get("token_usage_observed"),
            last_turn_token_usage_finality=canonical_meta.get("token_usage_finality"),
            last_turn_model=canonical_meta.get("actual_model"),
            last_turn_effort=canonical_meta.get("requested_effort"),
            last_turn_model_rerouted=canonical_meta.get("model_rerouted"),
        )
        audit_written = True
        return round_rejected_for_reroute

    try:
        if _force_stop_requested(wl):
            mark_attempt(
                phase="owner_stop",
                failure_code="owner_stop_requested",
                failure="cooperative owner stop requested before adapter start",
            )
            return WORKER_STOP_REQUESTED_RC
        mark_attempt(phase="app_server_start")
        client.start()
        _Child.proc = client.process
        client.initialize()
        _model_catalog_entry(client, requested_model, requested_effort)
        if stored_thread:
            if pending_intent is None:
                mark_attempt(phase="thread_state_check")
                bounded_state = client.rpc(
                    "thread/read",
                    {"threadId": stored_thread, "includeTurns": False},
                )
                _attest_bounded_thread_state(
                    bounded_state, expected_thread_id=stored_thread
                )
            mark_attempt(phase="thread_resume")
            try:
                response = client.rpc(
                    "thread/resume",
                    {
                        "threadId": stored_thread,
                        "cwd": str(model_workspace),
                        "model": requested_model,
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                    },
                )
            except RpcError as exc:
                # Losing a persisted mathematical conversation is not a benign
                # fallback.  A human may clear the mapping explicitly after
                # inspecting it; the worker never silently opens a fresh thread.
                mark_attempt(
                    failure_code="persisted_thread_resume_failed",
                    failure=redact_external_error(exc),
                )
                _best_effort_worker_projection(
                    log_path,
                    "[worker_loop] persisted thread resume failed: "
                    f"{redact_external_error(exc)}\n",
                )
                return 126
            thread_id, actual_model, thread_reasoning_effort = _attest_thread_runtime(
                response,
                worker_dir=model_workspace,
                requested_model=requested_model,
                expected_thread_id=stored_thread,
            )
        else:
            mark_attempt(phase="thread_start")
            response = client.rpc(
                "thread/start",
                {
                    "cwd": str(model_workspace),
                    "model": requested_model,
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "ephemeral": False,
                    "allowProviderModelFallback": False,
                },
            )
            thread_id, actual_model, thread_reasoning_effort = _attest_thread_runtime(
                response,
                worker_dir=model_workspace,
                requested_model=requested_model,
            )
            # Persist only a fully attested thread mapping.
            store.set_thread_id(wl.name, thread_id)

        mark_attempt(phase="intent_prepare")
        intent = store.round_intent(
            wl.name,
            thread_id,
            prompt_sha256=prompt_sha256,
            requested_model=requested_model,
            requested_effort=requested_effort,
        )
        requested_model = str(intent["requested_model"])
        requested_effort = str(intent["requested_effort"])
        round_client_id = str(intent["client_id"])
        mark_attempt(phase="intent_reconcile")
        # A newly started thread is not materialized by app-server until its
        # first user message; production 0.147 rejects includeTurns before then.
        # The durable ``prepared`` state proves turn/start has not been sent, so
        # there is nothing to reconcile and an empty local history is exact.
        history = (
            {"thread": {"id": thread_id, "turns": []}}
            if intent["state"] == "prepared"
            else client.rpc(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
        )
        located = message_turn(
            history, round_client_id, expected_thread_id=thread_id
        )
        recovered_prior_turn = located is not None
        if located is not None:
            mark_attempt(phase="intent_recovery", dispatch_state="recovered")
            turn_id, recovered_status = located
            _bounded_protocol_identity(turn_id, "recovered turn id")
            recovered = turn_snapshot(
                history, turn_id, expected_thread_id=thread_id
            )
            if recovered_status not in {"inProgress", "in_progress"}:
                terminal = recovered
            else:
                if intent["state"] != "started":
                    store.record_round_intent(
                        round_client_id,
                        "started",
                        turn_id=turn_id,
                        expected_states={intent["state"]},
                    )
                client.adopt_active_turn(thread_id, turn_id)
        else:
            unattributed_active = in_progress_turn_id(
                response, expected_thread_id=thread_id
            ) or in_progress_turn_id(
                history, expected_thread_id=thread_id
            )
            if unattributed_active:
                raise ProtocolError(
                    "thread has an in-progress turn not owned by the durable round intent"
                )
            if intent["state"] in {"started", "delivery_unknown"}:
                raise ProtocolError(
                    "durable paid-turn intent cannot be reconciled from thread history"
                )
            if intent["state"] == "dispatching":
                mark_attempt(
                    phase="turn_dispatch_reconciliation",
                    dispatch_state="unknown",
                )
                store.record_round_intent(
                    round_client_id,
                    "delivery_unknown",
                    expected_states={"dispatching"},
                )
                raise ProtocolError(
                    "paid turn dispatch was interrupted before acknowledgement; "
                    "refusing an automatic duplicate"
                )
            if _force_stop_requested(wl):
                mark_attempt(
                    phase="owner_stop",
                    dispatch_state="none",
                    failure_code="owner_stop_requested",
                    failure="cooperative owner stop requested before paid dispatch",
                )
                return WORKER_STOP_REQUESTED_RC
            store.record_round_intent(
                round_client_id,
                "dispatching",
                expected_states={intent["state"]},
            )
            mark_attempt(phase="turn_dispatch", dispatch_state="unknown")
            try:
                response = client.rpc(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                        "effort": requested_effort,
                        "clientUserMessageId": round_client_id,
                        "sandboxPolicy": {
                            "type": "workspaceWrite",
                            "writableRoots": [str(wl.local_memory)],
                            "networkAccess": False,
                            "excludeTmpdirEnvVar": True,
                            "excludeSlashTmp": True,
                        },
                    },
                )
            except RpcError:
                store.record_round_intent(
                    round_client_id,
                    "failed",
                    expected_states={"dispatching"},
                )
                mark_attempt(dispatch_state="none")
                raise
            except (HotJoinError, TimeoutError):
                store.record_round_intent(
                    round_client_id,
                    "delivery_unknown",
                    expected_states={"dispatching"},
                )
                raise
            turn = response.get("turn") if isinstance(response, dict) else None
            if (
                not isinstance(turn, dict)
                or not isinstance(turn.get("id"), str)
                or not turn.get("id")
            ):
                store.record_round_intent(
                    round_client_id,
                    "delivery_unknown",
                    expected_states={"dispatching"},
                )
                raise ProtocolError("turn/start returned no turn id")
            turn_id = turn["id"]
            _bounded_protocol_identity(turn_id, "turn id")
            store.record_round_intent(
                round_client_id,
                "started",
                turn_id=turn_id,
                expected_states={"dispatching"},
            )
            mark_attempt(phase="paid_turn_active", dispatch_state="sent")
            terminal = client.terminal_turn(thread_id, turn_id)
            if terminal is None and turn.get("status") in {"inProgress", "in_progress"}:
                client.adopt_active_turn(thread_id, turn_id)

        if recovered_prior_turn:
            mark_attempt(phase="recovered_turn_terminalization")
            recovery_failure = "recovered after prior adapter interruption"
            if terminal is None:
                recovery_failure = (
                    "recovered in-progress paid turn after prior adapter "
                    "interruption; interrupted"
                )
                try:
                    client.rpc(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": turn_id},
                        timeout=10,
                    )
                    terminal = client.wait_turn(thread_id, turn_id, 10)
                except (HotJoinError, TimeoutError):
                    terminal = client.terminal_turn(thread_id, turn_id)
            finalize_turn(
                terminal,
                failure=recovery_failure,
                reroute_observation_unknown=True,
            )
            # Historical model/rerouted notifications are not replayed by
            # thread/read. Preserve the recovered terminal, never duplicate the
            # paid turn, and fail-stop because exact model provenance is unknown.
            return APP_SERVER_MODEL_REROUTED_RC

        if terminal is not None:
            terminal_status = str(terminal.get("status", "unknown"))
            mark_attempt(
                phase="terminal_audit",
                failure_code=(
                    None
                    if terminal_status == "completed"
                    else "paid_turn_terminal_failure"
                ),
                failure=(
                    None
                    if terminal_status == "completed"
                    else f"paid turn terminal status={terminal_status}"
                ),
            )
            rerouted = finalize_turn(
                terminal,
            )
            if rerouted:
                return APP_SERVER_MODEL_REROUTED_RC
            return (
                0
                if terminal.get("status") == "completed"
                else APP_SERVER_PROTOCOL_FAILURE_RC
            )

        broker = HotJoinBroker(store, client, target=wl.name, thread_id=thread_id)
        broker.start()

        write_status(
            wl,
            transport="app-server",
            app_server_thread_id=thread_id,
            active_turn_id=turn_id,
        )

        deadline = (
            time.monotonic() + hard_timeout if hard_timeout > 0 else None
        )
        timed_out = False
        while terminal is None:
            try:
                client.ensure_owned_host_alive()
            except AppServerClosed:
                mark_attempt(
                    phase="app_server_host_lost",
                    failure_code="app_server_host_lost",
                    failure="app-server owned-child host exited unexpectedly",
                )
                raise
            if _force_stop_requested(wl):
                stop_failure = "cooperative owner stop requested"
                mark_attempt(
                    phase="terminal_audit",
                    failure_code="owner_stop_requested",
                    failure=stop_failure,
                )
                interrupted_terminal: Optional[dict] = None
                try:
                    client.rpc(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": turn_id},
                        timeout=10,
                    )
                    interrupted_terminal = client.wait_turn(
                        thread_id, turn_id, 10
                    )
                except (HotJoinError, TimeoutError):
                    pass
                if interrupted_terminal is None:
                    interrupted_terminal = client.terminal_turn(thread_id, turn_id)
                finalize_turn(interrupted_terminal, failure=stop_failure)
                return WORKER_STOP_REQUESTED_RC
            if client.model_reroutes(thread_id, turn_id).get("observed") is True:
                rerouted_terminal = client.terminal_turn(thread_id, turn_id)
                if rerouted_terminal is None:
                    try:
                        client.rpc(
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": turn_id},
                            timeout=10,
                        )
                        rerouted_terminal = client.wait_turn(thread_id, turn_id, 10)
                    except (HotJoinError, TimeoutError):
                        pass
                if rerouted_terminal is None:
                    rerouted_terminal = client.terminal_turn(thread_id, turn_id)
                finalize_turn(rerouted_terminal)
                return APP_SERVER_MODEL_REROUTED_RC
            if broker.error is not None:
                broker_failure = (
                    "human hot-join broker failed: "
                    f"{redact_external_error(broker.error)}"
                )
                mark_attempt(
                    phase="broker_failure",
                    failure_code="hotjoin_broker_failure",
                    failure=broker_failure,
                )
                broker_terminal: Optional[dict] = None
                try:
                    client.rpc(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": turn_id},
                        timeout=10,
                    )
                    broker_terminal = client.wait_turn(thread_id, turn_id, 10)
                except (HotJoinError, TimeoutError):
                    pass
                if broker_terminal is None:
                    broker_terminal = client.terminal_turn(thread_id, turn_id)
                rerouted = finalize_turn(
                    broker_terminal,
                    failure=broker_failure,
                )
                return (
                    APP_SERVER_MODEL_REROUTED_RC
                    if rerouted
                    else APP_SERVER_PROTOCOL_FAILURE_RC
                )
            remaining = (
                1.0 if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if deadline is not None and remaining <= 0:
                timed_out = True
                break
            try:
                terminal = client.wait_turn(
                    thread_id, turn_id, min(1.0, remaining)
                )
            except TimeoutError:
                continue
        if timed_out:
            timeout_failure = f"round hard-timeout after {hard_timeout}s"
            mark_attempt(
                phase="terminal_audit",
                failure_code="hard_timeout",
                failure=timeout_failure,
            )
            interrupted_terminal: Optional[dict] = None
            try:
                client.rpc(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    timeout=10,
                )
                interrupted_terminal = client.wait_turn(thread_id, turn_id, 10)
            except (HotJoinError, TimeoutError):
                pass
            if interrupted_terminal is None:
                interrupted_terminal = client.terminal_turn(thread_id, turn_id)
            rerouted = finalize_turn(
                interrupted_terminal,
                failure=timeout_failure,
            )
            return APP_SERVER_MODEL_REROUTED_RC if rerouted else 124
        assert terminal is not None
        terminal_status = str(terminal.get("status", "unknown"))
        mark_attempt(
            phase="terminal_audit",
            failure_code=(
                None if terminal_status == "completed" else "paid_turn_terminal_failure"
            ),
            failure=(
                None
                if terminal_status == "completed"
                else f"paid turn terminal status={terminal_status}"
            ),
        )
        rerouted = finalize_turn(terminal)
        if rerouted:
            return APP_SERVER_MODEL_REROUTED_RC
        return (
            0
            if terminal.get("status") == "completed"
            else APP_SERVER_PROTOCOL_FAILURE_RC
        )
    except FileNotFoundError:
        mark_attempt(
            failure_code="codex_binary_missing",
            failure=f"codex binary not found: {codex_bin}",
        )
        _best_effort_worker_projection(
            log_path, f"[worker_loop] codex binary not found: {codex_bin}\n"
        )
        return 127
    except (HotJoinError, TimeoutError) as exc:
        failure_code = _app_server_failure_code(exc, phase=attempt_phase)
        mark_attempt(
            failure_code=failure_code,
            failure=redact_external_error(exc),
        )
        rerouted = False
        if thread_id and round_client_id:
            cached_terminal = (
                terminal
                if terminal is not None
                else client.terminal_turn(thread_id, turn_id)
                if turn_id
                else None
            )
            cached_loss = (
                "host_loss"
                if failure_code == "app_server_host_lost"
                else "adapter_interruption"
                if isinstance(exc, AppServerClosed)
                else None
            )
            rerouted = finalize_turn(
                cached_terminal,
                failure=f"app-server failure: {redact_external_error(exc)}",
                cached_after_transport_loss=cached_loss,
            )
        else:
            _best_effort_worker_projection(
                log_path,
                "[worker_loop] app-server failure: "
                f"{redact_external_error(exc)}\n",
            )
        return (
            APP_SERVER_MODEL_REROUTED_RC
            if rerouted
            else APP_SERVER_PROTOCOL_FAILURE_RC
        )
    finally:
        broker_stopped = True
        if broker is not None:
            broker_stopped = broker.stop()
        try:
            client.close()
        finally:
            proc = _Child.proc or client.process
            cleanup_complete = proc is None or proc.returncode is not None
            if cleanup_complete and paid_authority_fd >= 0:
                os.close(paid_authority_fd)
                paid_authority_fd = -1
        if broker is not None and not broker_stopped:
            if not broker.stop():
                write_status(
                    wl,
                    broker_stop_error=(
                        "hot-join broker stop timed out after app-server close"
                    ),
                )
        if _Child.proc is None or _Child.proc.returncode is not None:
            _Child.proc = None
        if thread_id:
            write_status(wl, active_turn_id=None, app_server_thread_id=thread_id)


# --- the loop -------------------------------------------------------------- #

def _cleanup_pid(wl: L.WorkerLayout) -> None:
    """Remove our own .pid if it still points at us (clean exit only)."""
    try:
        payload = _read_regular_text(wl.pid, max_bytes=4096)
        record = json.loads(payload)
        if (
            isinstance(record, dict)
            and record.get("schema_version") == 1
            and record.get("pid") == os.getpid()
        ):
            wl.pid.unlink(missing_ok=True)
    except (
        FileNotFoundError,
        HotJoinError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
    ):
        pass


def _prior_round_sequence(wl: L.WorkerLayout) -> int:
    """Return a monotonic host round number without trusting partial writes."""
    prior = 0

    def accept(value: object) -> None:
        nonlocal prior
        if (
            not isinstance(value, bool)
            and isinstance(value, int)
            and 0 <= value <= 999_999_999_999
        ):
            prior = max(prior, value)

    try:
        status = json.loads(_read_regular_text(wl.status, max_bytes=1_000_000))
        if isinstance(status, dict):
            accept(status.get("round"))
    except (
        FileNotFoundError,
        HotJoinError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
    ):
        pass

    # A crash may create the next log before its status write is durable.  The
    # real, supervisor-owned log directory is therefore a second lower bound.
    try:
        for entry in wl.logs.iterdir():
            match = re.fullmatch(r"round_([1-9][0-9]{0,11})\.log", entry.name)
            if match is None:
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                accept(int(match.group(1)))
    except OSError:
        pass
    return prior


def main(worker_dir: str) -> int:
    wdir = Path(worker_dir).resolve()
    if not wdir.is_dir():
        print(f"worker dir not found: {wdir}", file=sys.stderr)
        return 2
    wl = L.WorkerLayout(wdir)
    try:
        require_gateway_runtime()
    except GatewayRuntimeUnavailable as exc:
        print(f"gateway runtime unavailable: {exc}", file=sys.stderr)
        return 126
    project_dir = wl.project_dir
    project = wl.project
    worker = wl.name
    transport = os.environ.get("DANUS_WORKER_TRANSPORT", "exec").strip().lower()
    try:
        role = _read_role(wl, protected=transport == "app-server")
    except HotJoinError as exc:
        print(f"worker role unavailable: {exc}", file=sys.stderr)
        return 126

    # Pin the worker's gateway to THIS interpreter (sys.executable = the venv
    # python danus runs on), rewritten every start: a moved/rebuilt venv is
    # picked up, and a bare `python3` on codex's PATH can never resolve the
    # gateway to a different install.
    scaffold.write_codex_config(wl)

    beat = float(os.environ.get("DANUS_ROUND_BEAT", "5"))
    hard_timeout = int(os.environ.get("DANUS_ROUND_HARD_TIMEOUT", "14400"))
    max_rounds = int(os.environ.get("DANUS_MAX_ROUNDS", "0"))
    max_fail = int(os.environ.get("DANUS_MAX_CONSEC_FAILURES", "5"))
    try:
        _ensure_real_dir(wl.logs)
    except (HotJoinError, OSError) as exc:
        print(f"unsafe worker logs directory: {exc}", file=sys.stderr)
        return 126
    prompt = kickoff(project, worker)

    def _on_term(signum, _frame):
        if _Child.proc is not None:
            _terminate_owned_child(_Child.proc)
        write_status(wl, state="terminated")
        _cleanup_pid(wl)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    round_seq = _prior_round_sequence(wl)
    write_status(wl, state="running", round=round_seq, started_at=time.time())
    attempts = 0
    consec_fail = 0
    try:
        while True:
            if wl.stop.exists():
                wl.stop.unlink(missing_ok=True)
                write_status(wl, state="stopped")
                break
            if _deadline_passed(project_dir):
                write_status(wl, state="deadline")
                break
            if max_rounds and attempts >= max_rounds:
                write_status(wl, state="max_rounds")
                break

            attempts += 1
            round_seq += 1
            log_path = wl.logs / f"round_{round_seq}.log"
            write_status(
                wl,
                state="running",
                round=round_seq,
                round_started_at=time.time(),
                error=None,
                recovery_required=None,
                attempt_phase="pre_dispatch",
                attempt_dispatch_state="none",
                attempt_failure_code=None,
                attempt_failure=None,
                attempt_client_id=None,
                attempt_thread_id=None,
                attempt_turn_id=None,
            )
            if transport == "app-server":
                rc = run_round_app_server(wl, role, prompt, log_path, hard_timeout)
            elif transport == "exec":
                rc = run_round(wl, role, prompt, log_path, hard_timeout)
            else:
                _best_effort_worker_projection(
                    log_path,
                    f"[worker_loop] unsupported DANUS_WORKER_TRANSPORT={transport!r}\n",
                )
                rc = 126
            last_fact_id = (
                _canonical_app_server_fact_id(project_dir, worker)
                if transport == "app-server"
                else _parse_last_fact_id(log_path, worker=wl)
            )
            finished_at = time.time()
            attempt_snapshot = _read_status_snapshot(wl)
            attempt = {
                "round": round_seq,
                "rc": rc,
                "phase": attempt_snapshot.get("attempt_phase", "unknown"),
                "dispatch_state": attempt_snapshot.get(
                    "attempt_dispatch_state", "unknown"
                ),
                "failure_code": attempt_snapshot.get("attempt_failure_code"),
                "failure": attempt_snapshot.get("attempt_failure"),
                "client_id": attempt_snapshot.get("attempt_client_id"),
                "thread_id": attempt_snapshot.get("attempt_thread_id"),
                "turn_id": attempt_snapshot.get("attempt_turn_id"),
                "finished_at": finished_at,
            }
            status_fields: dict[str, object] = {
                "state": "idle",
                "round": round_seq,
                "last_round_at": finished_at,
                "last_rc": rc,
                "last_fact_id": last_fact_id,
                "last_attempt": attempt,
            }
            if transport == "app-server" and attempt["dispatch_state"] in {
                "sent",
                "recovered",
            }:
                status_fields["last_paid_turn"] = {
                    **attempt,
                    "terminal_status": attempt_snapshot.get("last_turn_status"),
                    "token_usage": attempt_snapshot.get("last_turn_token_usage"),
                    "token_usage_observed": attempt_snapshot.get(
                        "last_turn_token_usage_observed"
                    ),
                    "token_usage_finality": attempt_snapshot.get(
                        "last_turn_token_usage_finality"
                    ),
                    "model": attempt_snapshot.get("last_turn_model"),
                    "effort": attempt_snapshot.get("last_turn_effort"),
                    "model_rerouted": attempt_snapshot.get(
                        "last_turn_model_rerouted"
                    ),
                }
            write_status(
                wl,
                **status_fields,
            )

            if rc == WORKER_STOP_REQUESTED_RC:
                wl.stop.unlink(missing_ok=True)
                write_status(
                    wl,
                    state="stopped",
                    error=None,
                    recovery_required=None,
                )
                break

            if transport == "app-server" and rc == APP_SERVER_MODEL_REROUTED_RC:
                write_status(
                    wl,
                    state="error",
                    error=(
                        "app-server model provenance was rerouted or could not be "
                        "recovered; automatic retry disabled"
                    ),
                    recovery_required=None,
                )
                return rc
            if transport == "app-server" and rc == APP_SERVER_PROTOCOL_FAILURE_RC:
                no_new_paid_turn = attempt.get("dispatch_state") == "none"
                oversized_history = (
                    attempt.get("failure_code") == THREAD_HISTORY_OVERSIZE_CODE
                )
                if no_new_paid_turn and oversized_history:
                    # A pre-existing started/delivery-unknown intent makes the
                    # oversized history an ambiguous paid-turn incident, not a
                    # terminal-context rotation opportunity.  Re-read the
                    # canonical ledger after the failed adapter attempt; only a
                    # proven empty unfinished-intent set may publish rotate argv.
                    # Any query failure is deliberately equivalent to ambiguity.
                    try:
                        unfinished_intent = HotJoinStore(
                            project_dir
                        ).unfinished_round_intent(worker)
                        intent_state_known = True
                    except Exception:
                        unfinished_intent = None
                        intent_state_known = False

                    if not intent_state_known:
                        recovery_required = None
                        error = (
                            "ambiguous paid intent preserved; owner must reconcile/"
                            "abandon explicitly. The unfinished-intent ledger could "
                            "not be read safely after oversized thread/resume; no new "
                            "turn/start was sent and automatic rotation/retry is disabled"
                        )
                    elif unfinished_intent is not None:
                        recovery_required = None
                        error = (
                            "ambiguous paid intent preserved; owner must reconcile/"
                            "abandon explicitly. Oversized thread/resume sent no new "
                            "turn/start; automatic rotation/retry is disabled"
                        )
                    else:
                        recovery_required = {
                            "action": "rotate_thread",
                            "reason": (
                                "persisted terminal thread history exceeds the bounded "
                                "app-server transport"
                            ),
                            "drops_conversation_context": True,
                            "preserves_research_memory": True,
                            "argv": [
                                "bin/danus",
                                "rotate-thread",
                                f"{project}/{worker}",
                                "--expected-thread-id",
                                str(attempt.get("thread_id") or ""),
                                "--reason",
                                "terminal history exceeds the app-server JSONL limit",
                            ],
                        }
                        error = (
                            "persisted terminal thread could not be resumed within the "
                            "bounded app-server transport; no new turn/start was sent. "
                            "The prior paid outcome is preserved in last_paid_turn; "
                            "automatic retry is disabled pending explicit owner rotation"
                        )
                else:
                    recovery_required = None
                    error = (
                        "app-server protocol, configuration, authentication, or "
                        "delivery failure; automatic retry disabled"
                    )
                write_status(
                    wl,
                    state="error",
                    error=error,
                    recovery_required=recovery_required,
                )
                return rc
            if rc in (126, 127):             # launch prerequisite missing — do not spin
                error = (
                    str(attempt.get("failure"))
                    if rc == 126 and attempt.get("failure")
                    else "gateway runtime unavailable"
                    if rc == 126
                    else "codex binary not found"
                )
                write_status(wl, state="error", error=error)
                return rc
            consec_fail = consec_fail + 1 if rc not in (0, 124) else 0
            if max_fail and consec_fail >= max_fail:
                write_status(wl, state="error", error=f"{consec_fail} consecutive failed rounds")
                return 1

            if beat > 0:
                time.sleep(beat)
    finally:
        _cleanup_pid(wl)
    return 0
