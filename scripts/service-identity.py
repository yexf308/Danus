#!/usr/bin/env python3
"""Durable, fail-closed guardian for Danus resident services.

The shell scripts in this repository never signal a numeric PID or PGID.  One
detached guardian owns each service lifecycle lock, an authenticated 0600 Unix
control socket, and an unreaped service-host ``Popen``.  The service host owns
the actual service ``Popen``.  Bidirectional liveness pipes make either a
guardian crash or a service-host crash tear down the service process group.

The file is deliberately standalone: bootstrap/recovery can run it with the
configured Python before the ``danus`` package is importable.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit


RECORD_VERSION = 2
CONTROL_VERSION = 1
MANIFEST_VERSION = 1
MAX_RECORD_BYTES = 4096
MAX_CONTROL_BYTES = 4096
MAX_HTTP_BYTES = 4096
MAX_LOG_READ_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX_128_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_FAULT_FIRED = False


class GuardianError(RuntimeError):
    """An unsafe or unavailable guardian operation."""


class ManagedFileUnlinked(GuardianError):
    """A regular managed file was unlinked after it was opened."""


class HealthUnavailable(GuardianError):
    """No process accepted the loopback health connection."""


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _reject_json_constant(value: str) -> "None":
    raise ValueError(f"non-standard JSON constant is forbidden: {value}")


def _strict_json_loads(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_json_constant)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])


def _read_line_fd(fd: int, *, limit: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GuardianError("bounded protocol timed out")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            raise GuardianError("bounded protocol timed out")
        chunk = os.read(fd, min(1024, limit + 1 - len(data)))
        if not chunk:
            raise GuardianError("bounded protocol closed before newline")
        data.extend(chunk)
        if len(data) > limit:
            raise GuardianError("bounded protocol exceeded its byte limit")
        if b"\n" in chunk:
            line, remainder = bytes(data).split(b"\n", 1)
            if remainder:
                raise GuardianError("bounded protocol contained trailing bytes")
            return line


def _parse_exact_json(raw: bytes, keys: set[str]) -> dict[str, Any]:
    try:
        value = _strict_json_loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GuardianError("bounded protocol is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != keys:
        raise GuardianError("bounded protocol has an unsupported schema")
    return value


def _set_socket_deadline(sock: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GuardianError("bounded socket protocol timed out")
    sock.settimeout(remaining)


def _validate_service(name: Any) -> None:
    if (
        not isinstance(name, str)
        or SERVICE_RE.fullmatch(name) is None
        or name in {".", ".."}
    ):
        raise GuardianError("invalid service name")
    if name != "verify" and not name.startswith("dashboard-"):
        raise GuardianError("unsupported service name")


def argv_marker(argv: list[str]) -> str:
    if not argv or any(not value or "\x00" in value for value in argv):
        raise GuardianError("service argv is empty or malformed")
    return hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _open_parent(path: Path) -> tuple[int, str]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise GuardianError("managed path must be an absolute file path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(str(path.parent), flags)
    except OSError as exc:
        raise GuardianError(f"unsafe managed parent directory: {exc}") from exc
    info = os.fstat(parent_fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(parent_fd)
        raise GuardianError("managed parent must be a real directory owned by this user")
    if info.st_mode & stat.S_IWOTH:
        os.close(parent_fd)
        raise GuardianError("managed parent may not be world-writable")
    return parent_fd, path.name


def _prepare_dir(path: Path) -> None:
    if not path.is_absolute():
        raise GuardianError("runtime directories must be absolute")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & stat.S_IWOTH
    ):
        raise GuardianError("runtime directory is unsafe")


def prepare_dirs(run_dir: Path, log_dir: Path) -> None:
    _prepare_dir(run_dir)
    _prepare_dir(log_dir)


def _open_regular_read(path: Path, max_bytes: int) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 0
            and info.st_uid == os.geteuid()
            and info.st_size <= max_bytes
        ):
            # A concurrent reader may open an authenticated guardian record
            # just before its owner unlinks it.  Keep that vanished inode
            # distinct from an unsafe extant file (including nlink > 1).
            raise ManagedFileUnlinked("managed file was unlinked during read")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_size > max_bytes
        ):
            raise GuardianError("managed file is not a safe bounded regular file")
        raw = os.read(fd, max_bytes + 1)
        if len(raw) > max_bytes:
            raise GuardianError("managed file exceeded its byte limit")
        return raw, info
    finally:
        os.close(fd)


def _fsync_unlink(path: Path) -> None:
    parent_fd, name = _open_parent(path)
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _rename_noreplace(
    source: str, destination: str, *, source_dir_fd: int, destination_dir_fd: int
) -> None:
    """Atomically rename without clobbering and without a hard-link crash state."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_raw = os.fsencode(source)
    destination_raw = os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_dir_fd,
            source_raw,
            destination_dir_fd,
            destination_raw,
            0x00000004,  # RENAME_EXCL
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_dir_fd,
            source_raw,
            destination_dir_fd,
            destination_raw,
            1,  # RENAME_NOREPLACE
        )
    else:
        raise GuardianError("atomic no-clobber rename is unsupported on this host")
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise GuardianError("managed record appeared during registration")
        raise GuardianError(f"atomic no-clobber rename failed: {os.strerror(error)}")


def _atomic_replace(path: Path, raw: bytes, *, no_clobber: bool = False) -> None:
    parent_fd, name = _open_parent(path)
    temp = f".{name}.tmp-{os.getpid()}-{os.urandom(8).hex()}"
    fd: int | None = None
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        _write_all(fd, raw)
        os.fsync(fd)
        _fault("after-file-fsync")
        os.close(fd)
        fd = None
        if no_clobber:
            _test_barrier("atomic-before-no-clobber-rename")
            _rename_noreplace(
                temp,
                name,
                source_dir_fd=parent_fd,
                destination_dir_fd=parent_fd,
            )
        else:
            os.rename(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        _fault("after-rename")
        os.fsync(parent_fd)
        _fault("after-dir-fsync")
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temp, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _fault(point: str) -> None:
    """Deterministic crash cuts used only by subprocess fault tests."""
    if os.environ.get("DANUS_SERVICE_FAULT_POINT") == point:
        os._exit(97)


def _test_barrier(point: str) -> None:
    """Bounded deterministic interleaving hook for subprocess fault tests."""
    if os.environ.get("DANUS_SERVICE_TEST_BARRIER_POINT") != point:
        return
    base = os.environ.get("DANUS_SERVICE_TEST_BARRIER_PATH")
    if not base:
        raise GuardianError("test barrier point requires a barrier path")
    marker = Path(base + ".reached")
    release = Path(base + ".release")
    marker.write_text(f"{os.getpid()}\n", encoding="ascii")
    deadline = time.monotonic() + 10.0
    while not release.exists():
        if time.monotonic() >= deadline:
            raise GuardianError(f"test barrier timed out: {point}")
        time.sleep(0.01)


def _manifest_lock_timeout() -> float:
    raw = os.environ.get("DANUS_MANIFEST_LOCK_TIMEOUT_SECONDS", "10")
    try:
        value = float(raw)
    except ValueError as exc:
        raise GuardianError("manifest lock timeout must be numeric") from exc
    if not 0.05 <= value <= 30.0:
        raise GuardianError("manifest lock timeout must be between .05 and 30 seconds")
    return value


def _open_lock(path: Path) -> int:
    parent_fd, name = _open_parent(path)
    try:
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
    ):
        os.close(fd)
        raise GuardianError("lifecycle lock is not a safe regular file")
    return fd


def _flock(fd: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _valid_manifest_entry(entry: str) -> bool:
    if entry == "verify":
        return True
    if not entry.startswith("dashboard "):
        return False
    project = entry[len("dashboard ") :]
    return SERVICE_RE.fullmatch(project) is not None and project not in {".", ".."}


def _read_manifest(path: Path) -> dict[str, str]:
    try:
        raw, _ = _open_regular_read(path, MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        return {}
    # Safely accept the pre-guardian newline format and migrate on mutation.
    try:
        value = _strict_json_loads(raw.decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, ValueError):
        try:
            lines = [line for line in raw.decode("utf-8", errors="strict").splitlines() if line]
        except UnicodeDecodeError as exc:
            raise GuardianError("autostart manifest is not UTF-8") from exc
        if any(not _valid_manifest_entry(line) for line in lines) or len(lines) != len(set(lines)):
            raise GuardianError("autostart manifest contains unsafe entries")
        return {
            line: hashlib.sha256(("legacy\0" + line).encode()).hexdigest()[:32]
            for line in lines
        }
    except UnicodeDecodeError as exc:
        raise GuardianError("autostart manifest is not UTF-8") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "entries"}:
        raise GuardianError("unsupported autostart manifest schema")
    entries = value["entries"]
    if value["schema_version"] != MANIFEST_VERSION or not isinstance(entries, dict):
        raise GuardianError("unsupported autostart manifest schema")
    if any(
        not isinstance(entry, str)
        or not _valid_manifest_entry(entry)
        or not isinstance(generation, str)
        or HEX_128_RE.fullmatch(generation) is None
        for entry, generation in entries.items()
    ):
        raise GuardianError("autostart manifest contains unsafe entries")
    return dict(entries)


def _write_manifest(path: Path, entries: dict[str, str]) -> None:
    raw = _json_bytes(
        {"schema_version": MANIFEST_VERSION, "entries": dict(sorted(entries.items()))}
    )
    if len(raw) > MAX_MANIFEST_BYTES:
        raise GuardianError("autostart manifest exceeds its byte limit")
    _atomic_replace(path, raw)


def _with_manifest_lock(lock_path: Path, timeout: float, operation: Any) -> Any:
    fd = _open_lock(lock_path)
    try:
        if not _flock(fd, timeout=timeout):
            raise GuardianError("autostart manifest lock timed out")
        return operation()
    finally:
        os.close(fd)


def manifest_mutate(
    path: Path, lock_path: Path, action: str, entry: str, generation: str | None
) -> dict[str, Any]:
    if not _valid_manifest_entry(entry):
        raise GuardianError("invalid autostart entry")

    def operation() -> dict[str, Any]:
        entries = _read_manifest(path)
        current = entries.get(entry)
        if action == "add":
            if current is not None:
                return {"state": "present", "generation": current}
            token = os.urandom(16).hex()
            entries[entry] = token
            _write_manifest(path, entries)
            return {"state": "added", "generation": token}
        if action == "del":
            if current is None:
                return {"state": "absent", "generation": None}
            del entries[entry]
            _write_manifest(path, entries)
            return {"state": "deleted", "generation": current}
        if action == "rollback":
            if generation is None or HEX_128_RE.fullmatch(generation) is None:
                raise GuardianError("rollback requires a 128-bit generation")
            if current != generation:
                return {"state": "unchanged", "generation": current}
            del entries[entry]
            _write_manifest(path, entries)
            return {"state": "rolled_back", "generation": generation}
        raise GuardianError("unsupported autostart mutation")

    return _with_manifest_lock(lock_path, _manifest_lock_timeout(), operation)


def manifest_snapshot(path: Path, lock_path: Path) -> list[dict[str, str]]:
    return _with_manifest_lock(
        lock_path,
        _manifest_lock_timeout(),
        lambda: [
            {"entry": entry, "generation": generation}
            for entry, generation in sorted(_read_manifest(path).items())
        ],
    )


def manifest_has(
    path: Path, lock_path: Path, entry: str, generation: str | None
) -> dict[str, Any]:
    if not _valid_manifest_entry(entry):
        raise GuardianError("invalid autostart entry")

    def operation() -> dict[str, Any]:
        current = _read_manifest(path).get(entry)
        if current is None:
            return {"state": "absent", "generation": None}
        if generation is not None and current != generation:
            return {"state": "replaced", "generation": current}
        return {"state": "present", "generation": current}

    return _with_manifest_lock(lock_path, _manifest_lock_timeout(), operation)


_RECORD_KEYS = {
    "schema_version",
    "authority",
    "guardian_pid",
    "service",
    "argv_sha256",
    "socket_path",
    "control_nonce",
    "instance_nonce",
    "health_kind",
    "health_url",
    "expected_protocol",
    "expected_digest",
    "intent_entry",
    "intent_generation",
    "created_ns",
}


def _validate_record(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise GuardianError("unsupported guardian record schema")
    _validate_service(value.get("service"))
    socket_path = value.get("socket_path")
    pid = value.get("guardian_pid")
    protocol = value.get("expected_protocol")
    intent_entry = value.get("intent_entry")
    intent_generation = value.get("intent_generation")
    if (
        value.get("schema_version") != RECORD_VERSION
        or value.get("authority") != "unix_guardian"
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or not isinstance(value.get("argv_sha256"), str)
        or SHA256_RE.fullmatch(value["argv_sha256"]) is None
        or not isinstance(socket_path, str)
        or not Path(socket_path).is_absolute()
        or Path(socket_path).parent != path.parent
        or path.name != f"{value['service']}.pid"
        or Path(socket_path).name != f"{value['service']}.sock"
        or not isinstance(value.get("control_nonce"), str)
        or HEX_128_RE.fullmatch(value["control_nonce"]) is None
        or not isinstance(value.get("instance_nonce"), str)
        or HEX_128_RE.fullmatch(value["instance_nonce"]) is None
        or value.get("health_kind") not in {"verify", "dashboard"}
        or not isinstance(value.get("health_url"), str)
        or len(value["health_url"]) > 512
        or (protocol is not None and (isinstance(protocol, bool) or not isinstance(protocol, int)))
        or (
            value.get("expected_digest") is not None
            and (
                not isinstance(value["expected_digest"], str)
                or SHA256_RE.fullmatch(value["expected_digest"]) is None
            )
        )
        or (
            (intent_entry is None) != (intent_generation is None)
            or (
                intent_entry is not None
                and (
                    not isinstance(intent_entry, str)
                    or not _valid_manifest_entry(intent_entry)
                    or not isinstance(intent_generation, str)
                    or HEX_128_RE.fullmatch(intent_generation) is None
                )
            )
        )
        or isinstance(value.get("created_ns"), bool)
        or not isinstance(value["created_ns"], int)
        or value["created_ns"] <= 0
    ):
        raise GuardianError("guardian record failed validation")
    if value["health_kind"] == "verify" and (
        protocol is None or value["expected_digest"] is None
    ):
        raise GuardianError("verify guardian record lacks its pinned contract")
    return value


def read_record(path: Path) -> dict[str, Any]:
    try:
        raw, info = _open_regular_read(path, MAX_RECORD_BYTES)
    except ManagedFileUnlinked as exc:
        # Record absence is authenticated by the lifecycle lock at every
        # caller that acts on it.  Other managed files retain fail-closed
        # handling for the same condition.
        raise FileNotFoundError(
            errno.ENOENT, "guardian record was unlinked", path
        ) from exc
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise GuardianError("guardian record permissions are not 0600")
    try:
        value = _strict_json_loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GuardianError("guardian record is malformed") from exc
    return _validate_record(value, path)


def _write_record(path: Path, value: dict[str, Any]) -> None:
    _validate_record(value, path)
    raw = _json_bytes(value)
    if len(raw) > MAX_RECORD_BYTES:
        raise GuardianError("guardian record exceeds its byte limit")
    _atomic_replace(path, raw, no_clobber=True)


def _remove_record_if_nonce(path: Path, nonce: str) -> bool:
    try:
        record = read_record(path)
    except FileNotFoundError:
        return True
    except GuardianError:
        return False
    if record["control_nonce"] != nonce:
        return False
    _fsync_unlink(path)
    return True


def _safe_socket(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise GuardianError("guardian control path is not a safe 0600 Unix socket")
    return info


def _peer_uid(conn: socket.socket) -> int:
    if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    if sys.platform == "darwin" and hasattr(socket, "LOCAL_PEERCRED"):
        # macOS xucred starts with (version:uint32, uid:uid_t).
        raw = conn.getsockopt(0, socket.LOCAL_PEERCRED, 128)
        if len(raw) < 8:
            raise GuardianError("Unix peer credentials were truncated")
        _version, uid = struct.unpack_from("@II", raw)
        return uid
    raise GuardianError("Unix peer credentials are unsupported on this host")


def _recv_socket_line(conn: socket.socket) -> bytes:
    deadline = time.monotonic() + 1.0
    data = bytearray()
    while True:
        _set_socket_deadline(conn, deadline)
        chunk = conn.recv(min(1024, MAX_CONTROL_BYTES + 1 - len(data)))
        if not chunk:
            raise GuardianError("control request closed before newline")
        data.extend(chunk)
        if len(data) > MAX_CONTROL_BYTES:
            raise GuardianError("control request exceeded 4096 bytes")
        if b"\n" in chunk:
            line, extra = bytes(data).split(b"\n", 1)
            if extra:
                raise GuardianError("control request contains trailing bytes")
            return line


def _control_request(record: dict[str, Any], command: str, timeout: float) -> dict[str, Any]:
    if command not in {"status", "stop"}:
        raise GuardianError("unsupported guardian command")
    path = Path(record["socket_path"])
    _safe_socket(path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    try:
        _set_socket_deadline(client, deadline)
        client.connect(str(path))
        _set_socket_deadline(client, deadline)
        client.sendall(
            _json_bytes(
                {
                    "version": CONTROL_VERSION,
                    "nonce": record["control_nonce"],
                    "command": command,
                }
            )
        )
        raw = bytearray()
        while True:
            _set_socket_deadline(client, deadline)
            chunk = client.recv(min(1024, MAX_CONTROL_BYTES + 1 - len(raw)))
            if not chunk:
                raise GuardianError("guardian response closed before newline")
            raw.extend(chunk)
            if len(raw) > MAX_CONTROL_BYTES:
                raise GuardianError("guardian response exceeded 4096 bytes")
            if b"\n" in chunk:
                line, extra = bytes(raw).split(b"\n", 1)
                if extra:
                    raise GuardianError("guardian response contains trailing bytes")
                break
        value = _parse_exact_json(
            line,
            {
                "version",
                "ok",
                "state",
                "service",
                "child_pid",
                "instance_nonce",
                "expected_protocol",
                "expected_digest",
                "intent_entry",
                "intent_generation",
            },
        )
        if (
            isinstance(value["version"], bool)
            or not isinstance(value["version"], int)
            or value["version"] != CONTROL_VERSION
            or value["ok"] is not True
            or value["service"] != record["service"]
            or value["instance_nonce"] != record["instance_nonce"]
            or value["expected_protocol"] != record["expected_protocol"]
            or value["expected_digest"] != record["expected_digest"]
            or value["intent_entry"] != record["intent_entry"]
            or value["intent_generation"] != record["intent_generation"]
            or value["state"] not in {"starting", "ready", "stopping"}
            or (
                value["child_pid"] is not None
                and (
                    isinstance(value["child_pid"], bool)
                    or not isinstance(value["child_pid"], int)
                    or value["child_pid"] <= 1
                )
            )
        ):
            raise GuardianError("guardian response failed authentication")
        return value
    except (OSError, socket.timeout) as exc:
        raise GuardianError(f"guardian control failed: {exc}") from exc
    finally:
        client.close()


def _http_json(url: str, timeout: float) -> tuple[int, Any]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port is None
    ):
        raise GuardianError("health URL must be bounded loopback HTTP")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in path):
        raise GuardianError("health URL path contains unsafe characters")
    family = socket.AF_INET6 if parsed.hostname == "::1" else socket.AF_INET
    connection = socket.socket(family, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    try:
        try:
            _set_socket_deadline(connection, deadline)
            connection.connect((parsed.hostname, parsed.port))
        except OSError as exc:
            raise HealthUnavailable(f"health endpoint unavailable: {exc}") from exc
        _set_socket_deadline(connection, deadline)
        connection.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
                "Connection: close\r\nAccept: application/json\r\n\r\n"
            ).encode("ascii")
        )
        wire = bytearray()
        while b"\r\n\r\n" not in wire:
            _set_socket_deadline(connection, deadline)
            chunk = connection.recv(min(1024, MAX_HTTP_BYTES + 5 - len(wire)))
            if not chunk:
                raise GuardianError("health response closed before its headers")
            wire.extend(chunk)
            if len(wire) > MAX_HTTP_BYTES:
                raise GuardianError("health response headers exceeded 4096 bytes")
        header_raw, body_raw = bytes(wire).split(b"\r\n\r\n", 1)
        try:
            lines = header_raw.decode("iso-8859-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise GuardianError("health response headers are malformed") from exc
        status_fields = lines[0].split(" ", 2)
        if (
            len(status_fields) < 2
            or status_fields[0] not in {"HTTP/1.0", "HTTP/1.1"}
            or len(status_fields[1]) != 3
            or not status_fields[1].isdigit()
        ):
            raise GuardianError("health response status line is malformed")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or line[:1].isspace() or ":" not in line:
                raise GuardianError("health response headers are malformed")
            name, value = line.split(":", 1)
            name = name.strip().lower()
            value = value.strip()
            if name in headers:
                raise GuardianError("health response contains duplicate headers")
            headers[name] = value
        if "transfer-encoding" in headers:
            raise GuardianError("chunked health responses are unsupported")
        length_text = headers.get("content-length")
        if length_text is None or not length_text.isdigit():
            raise GuardianError("health response requires a bounded Content-Length")
        length = int(length_text)
        if length > MAX_HTTP_BYTES:
            raise GuardianError("health response body exceeded 4096 bytes")
        if len(body_raw) > length:
            raise GuardianError("health response contained trailing bytes")
        body = bytearray(body_raw)
        while len(body) < length:
            _set_socket_deadline(connection, deadline)
            chunk = connection.recv(length - len(body))
            if not chunk:
                raise GuardianError("health response body was truncated")
            body.extend(chunk)
        try:
            value = _strict_json_loads(bytes(body).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise GuardianError("health response is not bounded JSON") from exc
        return int(status_fields[1]), value
    except HealthUnavailable:
        raise
    except OSError as exc:
        raise GuardianError(f"health request failed: {exc}") from exc
    finally:
        connection.close()


def _verify_http_health(
    record: dict[str, Any], child_pid: int, *, timeout: float
) -> None:
    status, body = _http_json(record["health_url"], timeout)
    expected_keys = {
        "status",
        "pid",
        "instance_nonce",
        "output_protocol_version",
        "verifier_bundle_digest",
    }
    if (
        status != 200
        or not isinstance(body, dict)
        or set(body) != expected_keys
        or body.get("status") != "ok"
        or body.get("pid") != child_pid
        or body.get("instance_nonce") != record["instance_nonce"]
        or body.get("output_protocol_version") != record["expected_protocol"]
        or body.get("verifier_bundle_digest") != record["expected_digest"]
    ):
        raise GuardianError("verify health identity/contract mismatch")


def _dashboard_http_health(
    record: dict[str, Any], child_pid: int, *, timeout: float
) -> None:
    status, body = _http_json(record["health_url"], timeout)
    if (
        status != 200
        or not isinstance(body, dict)
        or set(body) != {"status", "pid", "instance_nonce"}
        or body.get("status") != "ok"
        or body.get("pid") != child_pid
        or body.get("instance_nonce") != record["instance_nonce"]
    ):
        raise GuardianError("dashboard health identity mismatch")


def _waitid_exited(pid: int) -> bool:
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        raise GuardianError("host lacks unreaped-child inspection")
    result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    return result is not None


def _cleanup_fault(point: str) -> None:
    """Inject one guardian-cleanup syscall failure for deterministic tests."""
    global _CLEANUP_FAULT_FIRED
    if (
        not _CLEANUP_FAULT_FIRED
        and os.environ.get("DANUS_SERVICE_TEST_CLEANUP_EXCEPTION") == point
    ):
        _CLEANUP_FAULT_FIRED = True
        raise GuardianError(f"injected {point} cleanup exception")


def _terminate_exact_group(proc: subprocess.Popen[Any], grace: float = 2.0) -> None:
    """Signal only a direct child while retaining its unreaped PID fence."""
    pid = proc.pid
    try:
        group_ready = os.getpgid(pid) == pid
    except ProcessLookupError:
        group_ready = False
    try:
        _cleanup_fault("kill")
        if group_ready:
            os.killpg(pid, signal.SIGTERM)
        else:
            # Pre-setsid startup window: exact direct-child authority is safe;
            # a PGID equal to pid does not exist yet.
            proc.terminate()
    except ProcessLookupError:
        pass
    exited = False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            _cleanup_fault("waitid")
            if _waitid_exited(pid):
                exited = True
                break
        except ChildProcessError:
            return
        if not group_ready:
            try:
                group_ready = os.getpgid(pid) == pid
            except ProcessLookupError:
                pass
        time.sleep(0.02)
    else:
        try:
            # Re-check immediately before KILL: the host may have completed
            # setsid after our initial exact-PID TERM.
            group_ready = os.getpgid(pid) == pid
            if group_ready:
                os.killpg(pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                if _waitid_exited(pid):
                    exited = True
                    break
            except ChildProcessError:
                return
            time.sleep(0.02)
    if os.environ.get("DANUS_SERVICE_TEST_FORCE_NONTERMINAL") == "1":
        exited = False
        _test_barrier("guardian-before-terminal-wait")
    if not exited:
        # Safety dominates availability here.  A child stuck in uninterruptible
        # kernel sleep after KILL keeps the guardian (and the service-host's
        # shared flock) alive.  Never erase authority or admit a replacement
        # until waitid observes the exact direct child terminal while unreaped.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        exited = True
    # Sweep leader-dead descendants while the direct child PID is still an
    # unreaped zombie and therefore cannot have been reused.
    if group_ready:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # Darwin may report EPERM for a process group containing only the
            # already-dead zombie leader.  The direct child is still unreaped;
            # no numeric authority is transferred or retried elsewhere.
            pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _open_service_log(path: Path) -> int:
    parent_fd, name = _open_parent(path)
    try:
        fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
            | os.O_NONBLOCK,
            0o600,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
    ):
        os.close(fd)
        raise GuardianError("service log is not a safe regular file")
    return fd


def _send_host_status(fd: int, state: str, child_pid: int | None, detail: str) -> None:
    raw = _json_bytes({"state": state, "child_pid": child_pid, "detail": detail[:512]})
    if len(raw) > MAX_CONTROL_BYTES:
        os._exit(70)
    _write_all(fd, raw)


def service_host(guardian_fd: int, status_fd: int, lifecycle_lock_fd: int, argv: list[str]) -> int:
    """Own the real service Popen and die with its guardian."""
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    _test_barrier("host-before-setsid")
    _fault("host-before-setsid")
    os.setsid()
    proc: subprocess.Popen[Any] | None = None
    guardian_lost = False
    try:
        # CPython Popen uses a close-on-exec error pipe and does not return until
        # exec succeeded or the child reported errno.  This is our exec proof.
        service_env = os.environ.copy()
        service_env["DANUS_SERVICE_AUTHORITY_FD"] = str(lifecycle_lock_fd)
        try:
            proc = subprocess.Popen(
                argv,
                close_fds=True,
                pass_fds=(lifecycle_lock_fd,),
                env=service_env,
            )
        except BaseException as exc:
            try:
                _send_host_status(status_fd, "exec_failed", None, str(exc))
            except BaseException:
                pass
            return 70

        # From the instant exec is proven, every exception path below is inside
        # this function's fail-stop cleanup.  In particular, guardian death
        # between Popen return and the status write cannot orphan the service.
        _test_barrier("host-after-service-exec-before-status")
        _fault("host-after-service-exec-before-status")
        try:
            _send_host_status(status_fd, "exec_ok", proc.pid, "")
            os.set_blocking(guardian_fd, False)
        except BaseException:
            guardian_lost = True

        while True:
            if guardian_lost:
                break
            readable, _, _ = select.select([guardian_fd], [], [], 0.05)
            if readable:
                if os.read(guardian_fd, 1) == b"":
                    guardian_lost = True
                    stopping = True
            try:
                exited = _waitid_exited(proc.pid)
            except ChildProcessError:
                exited = True
            if exited or stopping:
                break
    except BaseException:
        # Any unexpected host-side failure after service exec is equivalent to
        # losing the guardian: fail-stop the entire owned group rather than
        # returning normally and leaving a live service behind.
        guardian_lost = proc is not None
    finally:
        if proc is not None:
            # Signal the service and all of its descendants.  This host ignores
            # TERM long enough to reap its exact Popen; the outer guardian keeps
            # this host unreaped and performs a final descendant sweep.
            try:
                os.killpg(os.getpgrp(), signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                try:
                    if _waitid_exited(proc.pid):
                        break
                except ChildProcessError:
                    break
                time.sleep(0.02)
            else:
                # If the guardian vanished, killing our own group is the
                # portable death coupling: it atomically kills this host and
                # the service while inherited authority remains held until all
                # owned descendants actually close it.
                if guardian_lost:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                try:
                    os.kill(proc.pid, signal.SIGKILL)  # exact unreaped child
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, ChildProcessError):
                if guardian_lost:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
            if guardian_lost:
                # Sweep descendants even when the service leader exited
                # promptly.  This also kills this host, closing its authority
                # fd only after the group-wide stop has been issued.
                os.killpg(os.getpgrp(), signal.SIGKILL)
        try:
            os.close(guardian_fd)
        except OSError:
            pass
        try:
            os.close(status_fd)
        except OSError:
            pass
        try:
            # This is the same flocked open-file description as the guardian's
            # fd.  On guardian SIGKILL it prevents reconcile/new-start until the
            # host has torn down the old process group.
            os.close(lifecycle_lock_fd)
        except OSError:
            pass
    return 0


def _host_start_line(
    fd: int,
    listener: socket.socket,
    record: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    """Wait for exec proof while still servicing authenticated stop."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GuardianError("service exec proof timed out")
        readable, _, _ = select.select([fd, listener], [], [], remaining)
        if listener in readable and _handle_connection(listener, record, "starting", None):
            raise GuardianError("service start cancelled by stop")
        if fd in readable:
            chunk = os.read(fd, min(1024, MAX_CONTROL_BYTES + 1 - len(data)))
            if not chunk:
                raise GuardianError("service host closed before exec proof")
            data.extend(chunk)
            if len(data) > MAX_CONTROL_BYTES:
                raise GuardianError("service exec proof exceeded 4096 bytes")
    raw, extra = bytes(data).split(b"\n", 1)
    if extra:
        raise GuardianError("service exec proof had trailing bytes")
    value = _parse_exact_json(raw, {"state", "child_pid", "detail"})
    if value["state"] != "exec_ok":
        raise GuardianError(f"service exec failed: {value.get('detail', '')}")
    child_pid = value["child_pid"]
    if isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid <= 1:
        raise GuardianError("service host returned an unsafe child PID")
    return child_pid, str(value["detail"])


def _control_response(
    record: dict[str, Any], state: str, child_pid: int | None
) -> dict[str, Any]:
    return {
        "version": CONTROL_VERSION,
        "ok": True,
        "state": state,
        "service": record["service"],
        "child_pid": child_pid,
        "instance_nonce": record["instance_nonce"],
        "expected_protocol": record["expected_protocol"],
        "expected_digest": record["expected_digest"],
        "intent_entry": record["intent_entry"],
        "intent_generation": record["intent_generation"],
    }


def _handle_connection(
    listener: socket.socket,
    record: dict[str, Any],
    state: str,
    child_pid: int | None,
) -> bool:
    conn, _ = listener.accept()
    try:
        if _peer_uid(conn) != os.geteuid():
            raise GuardianError("control peer UID mismatch")
        value = _parse_exact_json(
            _recv_socket_line(conn), {"version", "nonce", "command"}
        )
        if (
            isinstance(value["version"], bool)
            or not isinstance(value["version"], int)
            or value["version"] != CONTROL_VERSION
            or value["nonce"] != record["control_nonce"]
            or value["command"] not in {"status", "stop"}
        ):
            raise GuardianError("control request authentication failed")
        requested_stop = value["command"] == "stop"
        conn.sendall(
            _json_bytes(
                _control_response(
                    record, "stopping" if requested_stop else state, child_pid
                )
            )
        )
        return requested_stop
    except (GuardianError, OSError, socket.timeout):
        try:
            conn.sendall(b'{"error":"rejected"}\n')
        except OSError:
            pass
        return False
    finally:
        conn.close()


def _guardian(
    ack_fd: int,
    *,
    record_path: Path,
    lock_path: Path,
    socket_path: Path,
    service: str,
    log_path: Path,
    timeout: float,
    health_kind: str,
    health_url: str,
    expected_protocol: int | None,
    expected_digest: str | None,
    manifest_path: Path | None,
    manifest_lock: Path | None,
    require_entry: str | None,
    require_generation: str | None,
    argv: list[str],
) -> None:
    lock_fd: int | None = None
    listener: socket.socket | None = None
    host: subprocess.Popen[Any] | None = None
    host_live_write: int | None = None
    host_status_read: int | None = None
    log_fd: int | None = None
    control_nonce = os.urandom(16).hex()
    record: dict[str, Any] | None = None
    socket_inode: tuple[int, int] | None = None
    ack_sent = False

    def ack(value: dict[str, Any]) -> None:
        nonlocal ack_sent
        if not ack_sent:
            try:
                _write_all(ack_fd, _json_bytes(value))
            except OSError:
                pass
            ack_sent = True

    def require_current_intent() -> None:
        if require_entry is None:
            return
        if manifest_path is None or manifest_lock is None:
            raise GuardianError("recovery start lacks manifest paths")
        current = manifest_has(
            manifest_path, manifest_lock, require_entry, require_generation
        )
        if current["state"] != "present":
            raise GuardianError(
                f"service desired-state intent is {current['state']}"
            )

    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            os.setsid()
        except PermissionError:
            pass
        lock_fd = _open_lock(lock_path)
        if not _flock(lock_fd, timeout=0.0):
            ack({"result": "busy", "detail": "service lifecycle lock is held"})
            return
        _test_barrier("guardian-after-lock")
        _fault("guardian-after-lock")

        if require_entry is not None:
            try:
                require_current_intent()
            except GuardianError as exc:
                if "desired-state intent is" not in str(exc):
                    raise
                ack({"result": "skipped", "detail": str(exc).rsplit(" ", 1)[-1]})
                return
        _test_barrier("guardian-after-intent-check")
        require_current_intent()

        # Acquiring the exclusive lifecycle lock proves no trusted guardian is
        # alive.  Remove only a structurally safe stale record/socket; unsafe
        # state is retained and launch fails closed.
        try:
            stale = read_record(record_path)
        except FileNotFoundError:
            stale = None
        try:
            _safe_socket(socket_path)
        except FileNotFoundError:
            stale_socket_present = False
        else:
            stale_socket_present = True
        if stale is not None:
            try:
                _control_request(stale, "status", 0.2)
            except GuardianError:
                if not _remove_record_if_nonce(record_path, stale["control_nonce"]):
                    raise GuardianError("stale guardian record changed during cleanup")
            else:
                raise GuardianError("live guardian exists without its lifecycle lock")
        if stale_socket_present:
            _fsync_unlink(socket_path)

        log_fd = _open_service_log(log_path)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        if log_fd > 2:
            os.close(log_fd)
            log_fd = None

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        info = _safe_socket(socket_path)
        socket_inode = (info.st_dev, info.st_ino)
        listener.listen(8)
        listener.setblocking(False)

        instance_nonce = os.urandom(16).hex()
        record = {
            "schema_version": RECORD_VERSION,
            "authority": "unix_guardian",
            "guardian_pid": os.getpid(),
            "service": service,
            "argv_sha256": argv_marker(argv),
            "socket_path": str(socket_path),
            "control_nonce": control_nonce,
            "instance_nonce": instance_nonce,
            "health_kind": health_kind,
            "health_url": health_url,
            "expected_protocol": expected_protocol,
            "expected_digest": expected_digest,
            "intent_entry": require_entry,
            "intent_generation": require_generation,
            "created_ns": time.time_ns(),
        }
        _write_record(record_path, record)
        _fault("guardian-after-record")
        require_current_intent()

        host_live_read, host_live_write = os.pipe()
        host_status_read, host_status_write = os.pipe()
        os.set_inheritable(host_live_read, True)
        os.set_inheritable(host_status_write, True)
        child_env = os.environ.copy()
        child_env.pop("PYTHONPATH", None)
        child_env.pop("PYTHONHOME", None)
        child_env["DANUS_VERIFY_INSTANCE_NONCE"] = instance_nonce
        child_env["DANUS_SERVICE_INSTANCE_NONCE"] = instance_nonce
        child_env["DANUS_SERVICE_AUTHORITY_PATH"] = str(lock_path)
        host = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                "service-host",
                str(host_live_read),
                str(host_status_write),
                str(lock_fd),
                "--",
                *argv,
            ],
            pass_fds=(host_live_read, host_status_write, lock_fd),
            close_fds=True,
            env=child_env,
        )
        os.close(host_live_read)
        os.close(host_status_write)
        child_pid, _ = _host_start_line(
            host_status_read, listener, record, min(timeout, 5.0)
        )
        _test_barrier("guardian-after-exec")
        _fault("guardian-after-exec")
        require_current_intent()

        deadline = time.monotonic() + timeout
        stop_requested = False
        while time.monotonic() < deadline:
            require_current_intent()
            if _waitid_exited(host.pid):
                raise GuardianError("service host exited before readiness")
            readable, _, _ = select.select([listener, host_status_read], [], [], 0)
            if host_status_read in readable and os.read(host_status_read, 1) == b"":
                raise GuardianError("service host closed before readiness")
            if listener in readable:
                stop_requested = _handle_connection(
                    listener, record, "starting", child_pid
                )
                if stop_requested:
                    raise GuardianError("service start cancelled by stop")
            try:
                if health_kind == "verify":
                    _verify_http_health(record, child_pid, timeout=0.4)
                else:
                    _dashboard_http_health(record, child_pid, timeout=0.4)
            except GuardianError:
                time.sleep(0.04)
                continue
            break
        else:
            raise GuardianError("service readiness timed out")

        # Readiness is not a commit point unless the exact desired-state
        # generation is still current under the global manifest lock.
        require_current_intent()

        ack(
            {
                "result": "started",
                "detail": "ready",
                "child_pid": child_pid,
                "guardian_pid": os.getpid(),
            }
        )
        try:
            os.close(ack_fd)
        except OSError:
            pass

        while True:
            # A deleted intent is a durable stop even if the `down` caller dies
            # before reaching the control socket.  A replaced generation makes
            # this old guardian yield so the new `up` can acquire the lock.
            require_current_intent()
            readable, _, _ = select.select([listener, host_status_read], [], [], 1.0)
            if host_status_read in readable:
                if os.read(host_status_read, 1) == b"":
                    raise GuardianError("service host exited")
            if listener in readable and _handle_connection(listener, record, "ready", child_pid):
                break
    except BaseException as exc:
        try:
            print(f"service guardian [{service}] failed: {exc}", file=sys.stderr, flush=True)
        except BaseException:
            pass
        ack({"result": "failed", "detail": str(exc)[:512]})
    finally:
        if host is not None:
            # Releasing the record/socket/lock is permitted only after the
            # exact unreaped direct child has been observed terminal, its
            # process group swept, and the child reaped.  Any syscall anomaly
            # therefore keeps this guardian alive as the sole authority and is
            # retried fail-closed; it can never admit an overlapping service.
            cleanup_failures = 0
            while True:
                try:
                    _terminate_exact_group(host)
                    break
                except BaseException as exc:
                    cleanup_failures += 1
                    if cleanup_failures == 1 or cleanup_failures % 50 == 0:
                        try:
                            print(
                                f"service guardian [{service}] retaining authority "
                                f"after host cleanup failure: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                        except BaseException:
                            pass
                    try:
                        _test_barrier("guardian-cleanup-retry")
                    except BaseException:
                        # A test hook must never weaken the production safety
                        # invariant it is intended to exercise.
                        pass
                    time.sleep(0.1)
        if host_live_write is not None:
            try:
                os.close(host_live_write)
            except OSError:
                pass
            host_live_write = None
        if host_status_read is not None:
            try:
                os.close(host_status_read)
            except OSError:
                pass
        if record is not None:
            _remove_record_if_nonce(record_path, control_nonce)
        if listener is not None:
            listener.close()
        if socket_inode is not None:
            try:
                info = os.lstat(socket_path)
                if (info.st_dev, info.st_ino) == socket_inode and stat.S_ISSOCK(info.st_mode):
                    _fsync_unlink(socket_path)
            except (FileNotFoundError, GuardianError, OSError):
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        if log_fd is not None:
            os.close(log_fd)
        try:
            os.close(ack_fd)
        except OSError:
            pass


def start_guardian(**kwargs: Any) -> dict[str, Any]:
    timeout_value = float(kwargs["timeout"])
    if not 1.0 <= timeout_value <= 60.0:
        raise GuardianError("service readiness timeout must be between 1 and 60 seconds")
    ack_read, ack_write = os.pipe()
    os.set_inheritable(ack_write, False)
    previous = signal.getsignal(signal.SIGCHLD)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        pid = os.fork()
    except BaseException:
        os.close(ack_read)
        os.close(ack_write)
        signal.signal(signal.SIGCHLD, previous)
        raise
    if pid == 0:
        os.close(ack_read)
        try:
            _guardian(ack_write, **kwargs)
        finally:
            os._exit(0)
    os.close(ack_write)
    _test_barrier("launcher-after-fork")
    _fault("launcher-after-fork")
    try:
        timeout = float(kwargs["timeout"]) + 3.0
        raw = _read_line_fd(ack_read, limit=MAX_CONTROL_BYTES, timeout=timeout)
        value = _strict_json_loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or value.get("result") not in {
            "started",
            "busy",
            "skipped",
            "failed",
        }:
            raise GuardianError("guardian returned an invalid startup result")
        return value
    finally:
        os.close(ack_read)
        signal.signal(signal.SIGCHLD, previous)


def _absent_service_state(record_path: Path) -> dict[str, Any]:
    if not record_path.name.endswith(".pid"):
        raise GuardianError("guardian record filename is malformed")
    service = record_path.name[:-4]
    _validate_service(service)
    fd = _open_lock(record_path.with_name(f"{service}.lock"))
    try:
        if not _flock(fd, timeout=0.0):
            return {"state": "cleanup_in_progress"}
        return {"state": "absent"}
    finally:
        os.close(fd)


def service_status(record_path: Path) -> dict[str, Any]:
    try:
        record = read_record(record_path)
    except FileNotFoundError:
        return _absent_service_state(record_path)
    response = _control_request(record, "status", 1.0)
    return {"state": response["state"], "record": record, "control": response}


def public_service_status(record_path: Path) -> dict[str, Any]:
    """Return diagnostics without exposing either authentication nonce."""
    status = service_status(record_path)
    if status["state"] in {"absent", "cleanup_in_progress"}:
        return status
    record = status["record"]
    control = status["control"]
    return {
        "state": status["state"],
        "service": record["service"],
        "child_pid": control["child_pid"],
        "expected_protocol": record["expected_protocol"],
        "expected_digest": record["expected_digest"],
        "intent_entry": record["intent_entry"],
        "intent_generation": record["intent_generation"],
    }


def stop_service(
    record_path: Path,
    lock_path: Path,
    timeout: float,
    expected_generation: str | None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            record = read_record(record_path)
        except FileNotFoundError:
            fd = _open_lock(lock_path)
            try:
                if _flock(fd, timeout=0.0):
                    return {"state": "not_running"}
            finally:
                os.close(fd)
            if time.monotonic() >= deadline:
                raise GuardianError("service transition did not publish a guardian record")
            time.sleep(0.03)
            continue
        break
    if record["intent_generation"] != expected_generation:
        return {
            "state": "superseded",
            "running_generation": record["intent_generation"],
        }
    _control_request(record, "stop", 1.0)
    while time.monotonic() < deadline:
        try:
            current = read_record(record_path)
        except FileNotFoundError:
            fd = _open_lock(lock_path)
            try:
                if _flock(fd, timeout=0.0):
                    return {"state": "stopped"}
            finally:
                os.close(fd)
        else:
            if current["control_nonce"] != record["control_nonce"]:
                raise GuardianError("guardian record changed during stop")
        time.sleep(0.03)
    raise GuardianError("guardian stop timed out; no external signal was sent")


def reconcile_service(record_path: Path, lock_path: Path) -> dict[str, Any]:
    fd = _open_lock(lock_path)
    try:
        if not _flock(fd, timeout=0.0):
            try:
                status = service_status(record_path)
            except GuardianError as exc:
                raise GuardianError("held lifecycle lock has no authenticated guardian") from exc
            return {"state": "live", "status": status["state"]}
        try:
            record = read_record(record_path)
        except FileNotFoundError:
            return {"state": "absent"}
        socket_path = Path(record["socket_path"])
        try:
            _safe_socket(socket_path)
        except FileNotFoundError:
            stale_socket_present = False
        else:
            stale_socket_present = True
        # No guardian holds the lock.  Never signal anything; remove only this
        # structurally safe stale control record.
        if not _remove_record_if_nonce(record_path, record["control_nonce"]):
            raise GuardianError("stale guardian record changed during reconciliation")
        if stale_socket_present:
            _fsync_unlink(socket_path)
        return {"state": "stale_cleared"}
    finally:
        os.close(fd)


def verify_health(record_path: Path, url: str) -> dict[str, Any]:
    try:
        status = service_status(record_path)
    except GuardianError as exc:
        return {"state": "unsafe", "detail": str(exc)}
    if status["state"] == "cleanup_in_progress":
        return {
            "state": "cleanup_in_progress",
            "detail": "service lifecycle cleanup still holds its authority lock",
        }
    if status["state"] == "absent":
        try:
            _http_json(url, 1.0)
        except HealthUnavailable:
            return {"state": "down"}
        except GuardianError as exc:
            return {"state": "foreign", "detail": str(exc)}
        return {"state": "foreign", "detail": "port answered without our guardian"}
    record = status["record"]
    control = status["control"]
    if record["health_kind"] != "verify" or record["health_url"] != url:
        return {"state": "foreign", "detail": "guardian URL/type mismatch"}
    try:
        _verify_http_health(record, control["child_pid"], timeout=1.0)
    except GuardianError as exc:
        return {"state": "foreign", "detail": str(exc)}
    return {
        "state": "ours",
        "pid": control["child_pid"],
        "protocol": record["expected_protocol"],
        "digest": record["expected_digest"],
    }


def stream_log(path: Path, *, follow: bool) -> None:
    if follow:
        raise GuardianError(
            "authenticated log follow is unsupported; request another bounded snapshot"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise GuardianError("service log is not a safe regular file")
        offset = max(0, info.st_size - MAX_LOG_READ_BYTES)
        os.lseek(fd, offset, os.SEEK_SET)
        raw = os.read(fd, MAX_LOG_READ_BYTES)
        if offset and b"\n" in raw:
            raw = raw.split(b"\n", 1)[1]
        lines = raw.splitlines(keepends=True)[-50:]
        for line in lines:
            _write_all(1, line)
    finally:
        os.close(fd)


def project_dir(root: Path, name: str) -> Path:
    if SERVICE_RE.fullmatch(name) is None or name in {".", ".."}:
        raise GuardianError("project must be one safe path segment")
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise GuardianError("agents root must be a real directory")
    candidate = root / name
    info = os.lstat(candidate)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise GuardianError("project must be a real directory, not a symlink")
    resolved_root = root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if os.path.commonpath([str(resolved_root), str(resolved)]) != str(resolved_root):
        raise GuardianError("project escapes agents root")
    return resolved


def verifier_contract(root: Path) -> dict[str, Any]:
    if not root.is_absolute():
        raise GuardianError("repository root must be absolute")
    try:
        import danus
        from danus.core import VERIFICATION_OUTPUT_PROTOCOL_VERSION
        from danus.verify.launcher import VERIFIER_BUNDLE_DIGEST
    except Exception as exc:
        raise GuardianError(f"cannot load verifier contract: {exc}") from exc
    try:
        loaded_root = Path(danus.__file__).resolve(strict=True).parents[1]
        expected_root = root.resolve(strict=True)
    except (OSError, RuntimeError, IndexError) as exc:
        raise GuardianError("cannot bind verifier contract to this release") from exc
    if loaded_root != expected_root:
        raise GuardianError(
            "installed danus package does not match the requested release root"
        )
    return {
        "protocol": VERIFICATION_OUTPUT_PROTOCOL_VERSION,
        "digest": VERIFIER_BUNDLE_DIGEST,
    }


def _path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service-identity.py")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("run_dir", type=_path)
    prepare.add_argument("log_dir", type=_path)

    marker = sub.add_parser("marker")
    marker.add_argument("argv", nargs=argparse.REMAINDER)

    contract = sub.add_parser("verifier-contract")
    contract.add_argument("root", type=_path)

    manifest = sub.add_parser("manifest")
    manifest.add_argument(
        "action", choices=("add", "del", "rollback", "has", "snapshot", "lines")
    )
    manifest.add_argument("path", type=_path)
    manifest.add_argument("lock", type=_path)
    manifest.add_argument("entry", nargs="?")
    manifest.add_argument("generation", nargs="?")

    start = sub.add_parser("start")
    start.add_argument("record", type=_path)
    start.add_argument("lock", type=_path)
    start.add_argument("socket", type=_path)
    start.add_argument("service")
    start.add_argument("log", type=_path)
    start.add_argument("timeout", type=float)
    start.add_argument("health_kind", choices=("verify", "dashboard"))
    start.add_argument("health_url")
    start.add_argument("expected_protocol")
    start.add_argument("expected_digest")
    start.add_argument("manifest", type=_path)
    start.add_argument("manifest_lock", type=_path)
    start.add_argument("require_entry")
    start.add_argument("require_generation")
    start.add_argument("argv", nargs=argparse.REMAINDER)

    host = sub.add_parser("service-host")
    host.add_argument("guardian_fd", type=int)
    host.add_argument("status_fd", type=int)
    host.add_argument("lifecycle_lock_fd", type=int)
    host.add_argument("argv", nargs=argparse.REMAINDER)

    status = sub.add_parser("status")
    status.add_argument("record", type=_path)

    stop = sub.add_parser("stop")
    stop.add_argument("record", type=_path)
    stop.add_argument("lock", type=_path)
    stop.add_argument("timeout", type=float)
    stop.add_argument("expected_generation")

    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("record", type=_path)
    reconcile.add_argument("lock", type=_path)

    health = sub.add_parser("verify-health")
    health.add_argument("record", type=_path)
    health.add_argument("url")

    read_log = sub.add_parser("read-log")
    read_log.add_argument("path", type=_path)
    read_log.add_argument("--follow", action="store_true")

    project = sub.add_parser("project-dir")
    project.add_argument("root", type=_path)
    project.add_argument("name")

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_dirs(args.run_dir, args.log_dir)
            return 0
        if args.command == "marker":
            values = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            print(argv_marker(values))
            return 0
        if args.command == "verifier-contract":
            print(json.dumps(verifier_contract(args.root), sort_keys=True))
            return 0
        if args.command == "manifest":
            if args.action == "snapshot":
                print(json.dumps(manifest_snapshot(args.path, args.lock), sort_keys=True))
            elif args.action == "lines":
                for item in manifest_snapshot(args.path, args.lock):
                    print(f"{item['entry']}|{item['generation']}")
            elif args.action == "has":
                if args.entry is None:
                    raise GuardianError("manifest has requires ENTRY")
                print(
                    json.dumps(
                        manifest_has(args.path, args.lock, args.entry, args.generation),
                        sort_keys=True,
                    )
                )
            else:
                if args.entry is None:
                    raise GuardianError("manifest mutation requires ENTRY")
                print(
                    json.dumps(
                        manifest_mutate(
                            args.path,
                            args.lock,
                            args.action,
                            args.entry,
                            args.generation,
                        ),
                        sort_keys=True,
                    )
                )
            return 0
        if args.command == "start":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            _validate_service(args.service)
            if not command:
                raise GuardianError("start requires service argv")
            protocol = None if args.expected_protocol == "-" else int(args.expected_protocol)
            digest = None if args.expected_digest == "-" else args.expected_digest
            require_entry = None if args.require_entry == "-" else args.require_entry
            require_generation = (
                None if args.require_generation == "-" else args.require_generation
            )
            result = start_guardian(
                record_path=args.record,
                lock_path=args.lock,
                socket_path=args.socket,
                service=args.service,
                log_path=args.log,
                timeout=args.timeout,
                health_kind=args.health_kind,
                health_url=args.health_url,
                expected_protocol=protocol,
                expected_digest=digest,
                manifest_path=args.manifest,
                manifest_lock=args.manifest_lock,
                require_entry=require_entry,
                require_generation=require_generation,
                argv=command,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result["result"] in {"started", "skipped"} else 75 if result["result"] == "busy" else 70
        if args.command == "service-host":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if not command:
                raise GuardianError("service-host requires argv")
            return service_host(
                args.guardian_fd, args.status_fd, args.lifecycle_lock_fd, command
            )
        if args.command == "status":
            print(json.dumps(public_service_status(args.record), sort_keys=True))
            return 0
        if args.command == "stop":
            if not 0.1 <= args.timeout <= 60.0:
                raise GuardianError("service stop timeout must be between .1 and 60 seconds")
            expected_generation = (
                None if args.expected_generation == "-" else args.expected_generation
            )
            if (
                expected_generation is not None
                and HEX_128_RE.fullmatch(expected_generation) is None
            ):
                raise GuardianError("stop expected generation must be 128-bit hex or -")
            print(
                json.dumps(
                    stop_service(
                        args.record, args.lock, args.timeout, expected_generation
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "reconcile":
            print(json.dumps(reconcile_service(args.record, args.lock), sort_keys=True))
            return 0
        if args.command == "verify-health":
            result = verify_health(args.record, args.url)
            print(json.dumps(result, sort_keys=True))
            return {
                "ours": 0,
                "foreign": 3,
                "unsafe": 4,
                "cleanup_in_progress": 4,
                "down": 5,
            }.get(result["state"], 4)
        if args.command == "read-log":
            stream_log(args.path, follow=args.follow)
            return 0
        if args.command == "project-dir":
            print(project_dir(args.root, args.name))
            return 0
        raise GuardianError("unknown command")
    except (GuardianError, OSError, ValueError) as exc:
        print(f"service-identity: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
