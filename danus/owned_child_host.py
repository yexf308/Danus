"""Crash guardian for one paid subprocess (stdlib-only executable module)."""

from __future__ import annotations

import argparse
import os
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Sequence


TERM_GRACE_SECONDS = 3.0


def _parse(argv: Sequence[str]) -> tuple[int, int, list[int], list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--liveness-fd", required=True, type=int)
    parser.add_argument("--status-fd", required=True, type=int)
    parser.add_argument("--hold-fd", action="append", type=int, default=[])
    options, command = parser.parse_known_args(list(argv))
    if command and command[0] == "--":
        command = command[1:]
    if (
        options.liveness_fd < 3
        or options.status_fd < 3
        or options.liveness_fd == options.status_fd
        or not command
        or not os.path.isabs(command[0])
    ):
        raise ValueError("invalid owned-child host invocation")
    for fd in (options.liveness_fd, options.status_fd):
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise ValueError("owned-child host descriptor is not a pipe")
    hold_fds: list[int] = []
    for fd in options.hold_fd:
        if fd < 3 or fd in {options.liveness_fd, options.status_fd} or fd in hold_fds:
            raise ValueError("invalid owned-child retained descriptor")
        os.fstat(fd)
        hold_fds.append(fd)
    return options.liveness_fd, options.status_fd, hold_fds, command


def _exited_no_reap(proc: subprocess.Popen[bytes]) -> bool:
    try:
        return os.waitid(
            os.P_PID,
            proc.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        ) is not None
    except InterruptedError:
        return False
    except ChildProcessError as exc:
        raise RuntimeError("owned paid child was reaped before group cleanup") from exc


def _write_returncode(status_fd: int, value: int) -> None:
    payload = f"RC {value}\n".encode("ascii")
    while payload:
        try:
            written = os.write(status_fd, payload)
        except InterruptedError:
            continue
        except OSError:
            # Worker death closes the receipt reader at exactly the moment the
            # group must be killed.  Observability is best effort; cleanup is
            # never conditional on a surviving reader.
            return
        payload = payload[written:]


def _stop_own_group(
    proc: subprocess.Popen[bytes], *, leader_exited: bool, status_fd: int
) -> "None":
    """TERM then KILL the host's fenced group, including the host itself."""
    group = os.getpgrp()
    # The host is the session/group leader.  Its caught TERM handler survives;
    # the exec'd child has the default disposition and receives the signal.
    os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + (0.0 if leader_exited else TERM_GRACE_SECONDS)
    while not leader_exited and time.monotonic() < deadline:
        if _exited_no_reap(proc):
            leader_exited = True
            break
        time.sleep(0.02)
    if leader_exited:
        child_rc = int(proc.wait())
    else:
        child_rc = -int(signal.SIGKILL)
    _write_returncode(status_fd, child_rc)
    # KILL is intentionally addressed to the complete group.  It catches an
    # uncooperative child/grandchild and the host in one atomic kernel action.
    # If the worker is alive it retains the host zombie as a PGID reuse fence;
    # after worker death the group has already been eliminated before orphaning.
    os.killpg(group, signal.SIGKILL)
    raise AssertionError("SIGKILL unexpectedly returned")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        liveness_fd, status_fd, hold_fds, command = _parse(
            sys.argv[1:] if argv is None else argv
        )
    except (OSError, ValueError) as exc:
        print(f"owned-child host refused invocation: {exc}", file=sys.stderr)
        return 126

    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    # A caller may have inherited SIGCHLD=SIG_IGN/SA_NOCLDWAIT.  Restore a
    # retained direct-child fence before spawning the paid process.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, request_stop)
    try:
        # The real child deliberately inherits the host's session and process
        # group.  Therefore a host exception, worker-death EOF, or final parent
        # sweep cannot leave a detached paid process behind.
        proc = subprocess.Popen(command, close_fds=True)
    except (OSError, ValueError) as exc:
        print(f"owned paid child failed to exec: {exc}", file=sys.stderr)
        _write_returncode(status_fd, 127)
        os.close(liveness_fd)
        os.close(status_fd)
        return 127

    try:
        while True:
            if _exited_no_reap(proc):
                _stop_own_group(proc, leader_exited=True, status_fd=status_fd)
            if stop_requested:
                _stop_own_group(proc, leader_exited=False, status_fd=status_fd)
            ready, _, _ = select.select([liveness_fd], [], [], 0.05)
            if ready:
                # The protocol carries no data: EOF (or unexpected input) is
                # an unconditional revocation of the worker-liveness lease.
                os.read(liveness_fd, 1)
                _stop_own_group(proc, leader_exited=False, status_fd=status_fd)
    except BaseException:
        # Fail stopped on every monitor exception.  This group KILL includes
        # the host itself, so Python cannot unwind past it and orphan Codex.
        _stop_own_group(
            proc,
            leader_exited=_exited_no_reap(proc),
            status_fd=status_fd,
        )
    finally:
        # Reached only before a real child existed or if the kernel refused a
        # group signal; close all private descriptors without exposing data.
        for fd in (liveness_fd, status_fd, *hold_fds):
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
