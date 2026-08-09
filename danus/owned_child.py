"""Parent-side host for paid subprocesses owned by a Danus worker.

The worker never launches Codex directly.  A tiny retained host process owns
the real Codex process and watches a close-on-exec pipe held by the worker.  If
the worker crashes or is SIGKILLed, EOF on that pipe makes the host terminate
and reap the complete Codex process group before it exits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


_LIVENESS_FD_ATTR = "_danus_worker_liveness_fd"
_STATUS_FD_ATTR = "_danus_child_status_fd"
_ACTUAL_RC_ATTR = "_danus_actual_returncode"


def _host_script() -> Path:
    path = Path(__file__).with_name("owned_child_host.py")
    if not path.is_file():
        raise FileNotFoundError(f"owned-child host is unavailable: {path}")
    return path


def spawn_owned_child(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    env: Optional[Mapping[str, str]] = None,
    popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    hold_fds: Sequence[int] = (),
    **kwargs: Any,
) -> subprocess.Popen[Any]:
    """Spawn a retained host whose child is ``argv``.

    ``stdin``/``stdout``/``stderr`` belong to the host and are inherited
    unchanged by the real child, so JSONL app-server pipes remain directly
    usable by the worker.  The host does not parse or buffer their contents.
    """
    command = [str(part) for part in argv]
    if not command or not os.path.isabs(command[0]):
        raise ValueError("owned child argv[0] must be an absolute executable path")
    if "start_new_session" in kwargs or "pass_fds" in kwargs:
        raise ValueError("owned-child session and liveness descriptors are internal")
    read_fd, write_fd = os.pipe()
    status_read_fd, status_write_fd = os.pipe()
    try:
        retained_fds: list[int] = []
        for fd in hold_fds:
            if isinstance(fd, bool) or not isinstance(fd, int) or fd < 3:
                raise ValueError("owned-child retained descriptor is invalid")
            os.fstat(fd)
            if fd in retained_fds or fd in {
                read_fd,
                write_fd,
                status_read_fd,
                status_write_fd,
            }:
                raise ValueError("owned-child retained descriptors must be unique")
            retained_fds.append(fd)
        host_argv = [
            sys.executable,
            "-I",
            "-B",
            str(_host_script()),
            "--liveness-fd",
            str(read_fd),
            "--status-fd",
            str(status_write_fd),
            *[
                token
                for fd in retained_fds
                for token in ("--hold-fd", str(fd))
            ],
            "--",
            *command,
        ]
        proc = popen(
            host_argv,
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            start_new_session=True,
            pass_fds=(read_fd, status_write_fd, *retained_fds),
            **kwargs,
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        os.close(status_read_fd)
        os.close(status_write_fd)
        raise
    os.close(read_fd)
    os.close(status_write_fd)
    os.set_blocking(status_read_fd, False)
    setattr(proc, _LIVENESS_FD_ATTR, write_fd)
    setattr(proc, _STATUS_FD_ATTR, status_read_fd)
    setattr(proc, _ACTUAL_RC_ATTR, None)
    return proc


def request_owned_child_stop(proc: subprocess.Popen[Any]) -> None:
    """Durably revoke the worker-liveness lease held by ``proc``'s host."""
    fd = getattr(proc, _LIVENESS_FD_ATTR, None)
    if isinstance(fd, int) and fd >= 0:
        setattr(proc, _LIVENESS_FD_ATTR, -1)
        try:
            os.close(fd)
        except OSError:
            pass


def owned_child_exited_no_reap(proc: subprocess.Popen[Any]) -> bool:
    """Observe the retained host's terminal state without releasing its PID."""
    if proc.returncode is not None:
        return True
    pid = proc.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise RuntimeError("owned-child host has an unsafe process id")
    try:
        return os.waitid(
            os.P_PID,
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        ) is not None
    except InterruptedError:
        return False
    except ChildProcessError as exc:
        raise RuntimeError(
            "owned-child host was reaped before its child cleanup was attested"
        ) from exc


def _capture_actual_returncode(proc: subprocess.Popen[Any]) -> None:
    fd = getattr(proc, _STATUS_FD_ATTR, None)
    if not isinstance(fd, int) or fd < 0:
        return
    chunks: list[bytes] = []
    try:
        while True:
            try:
                chunk = os.read(fd, 64)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(part) for part in chunks) > 64:
                break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        setattr(proc, _STATUS_FD_ATTR, -1)
    payload = b"".join(chunks)
    if len(payload) <= 64 and payload.startswith(b"RC "):
        try:
            value = int(payload[3:].strip().decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return
        if -255 <= value <= 255:
            setattr(proc, _ACTUAL_RC_ATTR, value)


def owned_child_returncode(proc: subprocess.Popen[Any]) -> Optional[int]:
    """Return the real child's recorded code after the host was reaped."""
    value = getattr(proc, _ACTUAL_RC_ATTR, None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if proc.returncode is not None:
        return int(proc.returncode)
    return None


def _sweep_terminal_host_group(proc: subprocess.Popen[Any]) -> None:
    """Sweep descendants while the unreaped host still fences its PGID."""
    pid = proc.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise RuntimeError("owned-child host has an unsafe process-group id")
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin reports EPERM for a group containing only the exact zombie
        # leader.  The caller has already observed that leader with WNOWAIT.
        pass


def stop_owned_child(
    proc: subprocess.Popen[Any], *, grace: float = 10.0
) -> int:
    """Ask the host to clean its child group, then reap the retained host.

    No numeric signal is sent by the worker.  If the host does not honor its
    liveness lease within the deadline, its handle remains retained and the
    caller receives an explicit failure instead of risking an orphaned paid
    subprocess with a blind SIGKILL.
    """
    request_owned_child_stop(proc)
    if proc.returncode is not None:
        _capture_actual_returncode(proc)
        return int(owned_child_returncode(proc) or 0)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if owned_child_exited_no_reap(proc):
            _sweep_terminal_host_group(proc)
            proc.wait()
            _capture_actual_returncode(proc)
            value = owned_child_returncode(proc)
            if value is None:
                raise RuntimeError("owned-child host omitted its terminal receipt")
            return value
        time.sleep(0.02)
    raise TimeoutError("owned-child host did not finish process-group cleanup")
