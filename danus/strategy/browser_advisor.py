"""Durable owner-mediated ChatGPT Pro browser-advisor broker.

This module deliberately does *not* automate a browser and never imports a
browser, model, API, gateway, verifier, or FactGraph client.  It is the durable
handoff seam between Danus and an owner-controlled ``query-chatgpt-pro`` skill:

* Danus prepares and hashes the exact prompt.
* The owner explicitly authorizes transmission to ChatGPT.
* The UI driver records dispatch before clicking, then attests the observed
  submitted prompt and stable final response.
* Danus imports the response only as untrusted strategy with no correctness or
  control-plane authority.

An interrupted dispatch is ambiguous by construction.  The broker records
``delivery_unknown`` and never authorizes an automatic resend.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse


CHATGPT_BROWSER_TRANSPORT = "chatgpt_pro_browser"
CHATGPT_DESTINATION = "https://chatgpt.com/"
ADVISOR_TRUST = "untrusted_strategy"
ADVISOR_AUTHORITIES: tuple[str, ...] = ()
MAX_PROMPT_BYTES = 256 * 1024
MAX_REPLY_BYTES = 512 * 1024

_STATES = {
    "prepared",
    "authorized",
    "dispatching",
    "submitted",
    "completed",
    "needs_user_input",
    "delivery_unknown",
    "imported",
    "adopted",
    "failed_not_submitted",
    "abandoned",
    "owner_abandoned_outcome_unknown",
}
_AMBIGUOUS_STATES = {"dispatching", "submitted", "delivery_unknown"}
_CONTINUATION_PREDECESSOR_STATES = {
    "completed",
    "imported",
    "adopted",
    "needs_user_input",
}
_ACTIVE_CONVERSATION_STATES = {
    "prepared",
    "authorized",
    "dispatching",
    "submitted",
    "delivery_unknown",
    "owner_abandoned_outcome_unknown",
}
_LINEAGE_KINDS = {"new_chat", "local_predecessor"}
_TERMINAL_STATES = {
    "imported",
    "adopted",
    "failed_not_submitted",
    "abandoned",
    "owner_abandoned_outcome_unknown",
    "needs_user_input",
}
_RECOMMENDATION_RELEASE_SAFE_STATES = {
    "imported",
    "adopted",
    "failed_not_submitted",
    "abandoned",
    "owner_abandoned_outcome_unknown",
    "needs_user_input",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GLOBAL_MEMORY_ENTRY_ID_RE = re.compile(r"[0-9a-f]{16}")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=:%-]{8,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|authorization|password)\b\s*[:=]\s*"
        r"[\"']?[^\s,;\"']{8,}"
    ),
)
_CONTROL_SIGNALS = {
    "fact_submit": re.compile(r"(?i)\bfact_submit\b"),
    "fact_revoke": re.compile(r"(?i)\bfact_revoke\b"),
    "verifier_control": re.compile(
        r"(?i)\b(?:start|stop|restart|bypass|disable)\s+(?:the\s+)?verifier\b"
    ),
    "finalize_or_publish": re.compile(
        r"(?i)\b(?:danus\s+finalize|paper_deliver|publish\s+(?:the\s+)?paper)\b"
    ),
    "process_control": re.compile(r"(?i)\b(?:turn/interrupt|danus\s+stop)\b"),
    "shell_or_script": re.compile(r"(?i)(?:```\s*(?:bash|sh|zsh)|<script\b)"),
}

_LOCK_REGISTRY_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_FENCE_SUFFIX = ".browser-output.lock"
_MAX_DURABLE_SCAN_NODES = 100_000
_MAX_DURABLE_SCAN_DEPTH = 64
_MAX_GLOBAL_MEMORY_LINE_BYTES = 16 * 1024 * 1024
_FENCE_LOCAL = threading.local()


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.realpath(os.fspath(path))
    with _LOCK_REGISTRY_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _durable_string_digests(
    fields: dict[str, object],
) -> list[tuple[str, str]]:
    """Hash every persisted scalar string without label-collision loss."""

    output: list[tuple[str, str]] = []
    active_containers: set[int] = set()
    nodes = 0

    def collect(label: str, value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_DURABLE_SCAN_NODES:
            raise ValueError("durable global-memory payload exceeds scan node limit")
        if depth > _MAX_DURABLE_SCAN_DEPTH:
            raise ValueError("durable global-memory payload exceeds nesting limit")
        if isinstance(value, str):
            try:
                output.append((label, _sha256_text(value)))
            except UnicodeEncodeError as exc:
                raise ValueError(f"global-memory {label} must be valid UTF-8") from exc
            return
        if isinstance(value, (dict, list, tuple)):
            identity = id(value)
            if identity in active_containers:
                raise ValueError("durable global-memory payload contains a cycle")
            active_containers.add(identity)
            try:
                if isinstance(value, dict):
                    for index, (key, item) in enumerate(value.items()):
                        if isinstance(key, (str, int, float, bool)) or key is None:
                            collect(f"{label}.key[{index}]", str(key), depth + 1)
                        collect(f"{label}.value[{index}]", item, depth + 1)
                else:
                    for index, item in enumerate(value):
                        collect(f"{label}[{index}]", item, depth + 1)
            finally:
                active_containers.remove(identity)

    for field, value in fields.items():
        collect(field, value, 0)
    return output


def _canonical_project_identity(
    project_dir: Path | str,
) -> tuple[Path, tuple[str, int, int]]:
    try:
        project = Path(project_dir).resolve(strict=True)
        project_info = os.lstat(project)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise BrowserAdvisorError("advisor project must exist") from exc
    if stat.S_ISLNK(project_info.st_mode) or not stat.S_ISDIR(project_info.st_mode):
        raise BrowserAdvisorError("advisor project must be a real directory")
    return project, (
        os.fspath(project),
        int(project_info.st_dev),
        int(project_info.st_ino),
    )


def _canonical_control_root() -> Path:
    """Return the one installation-bound supervisor control root.

    The authority path is derived from the already loaded trusted Danus module,
    never from process environment or an MCP/CLI argument.  A same-UID worker
    can therefore start another Python process or replace environment variables,
    but cannot select a second flock domain without modifying trusted code.
    """

    try:
        release_root = Path(__file__).resolve(strict=True).parents[2]
    except (OSError, RuntimeError, IndexError) as exc:
        raise BrowserAdvisorError(
            "could not resolve the trusted Danus release root"
        ) from exc
    return release_root / "runtime" / "advisor-control"


def _project_fence_name(project_identity: tuple[str, int, int]) -> str:
    canonical_project = json.dumps(
        project_identity, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    project_key = hashlib.sha256(canonical_project).hexdigest()
    return f"{project_key}{_PROJECT_FENCE_SUFFIX}"


def _project_fence_path(project_dir: Path | str) -> Path:
    """Return the canonical fence path for diagnostics and offline tests."""

    _, project_identity = _canonical_project_identity(project_dir)
    return _canonical_control_root() / _project_fence_name(project_identity)


@contextmanager
def _project_memory_fence(project_dir: Path | str) -> Iterator[None]:
    """Serialize browser digests with every sanctioned durable GM append.

    The lock is deliberately outside the project and every worker writable
    root.  A project-local lock could be unlinked and replaced by a same-UID
    sandboxed worker, splitting the ``flock`` domain.  Both the owner browser
    CLI and the gateway derive the same supervisor control root from the loaded
    trusted Danus release and key one lock by canonical project identity.
    """

    project, project_identity = _canonical_project_identity(project_dir)
    active_fences = getattr(_FENCE_LOCAL, "project_identities", set())
    if project_identity in active_fences:
        raise BrowserAdvisorError("browser-output fence cannot be acquired recursively")

    control_root = _canonical_control_root()
    if not control_root.is_absolute() or ".." in control_root.parts:
        raise BrowserAdvisorError("advisor control root must be an absolute safe path")

    parent = control_root.parent
    try:
        parent_info = os.lstat(parent)
    except (FileNotFoundError, OSError) as exc:
        raise BrowserAdvisorError("advisor control-root parent must exist") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise BrowserAdvisorError(
            "advisor control-root parent must be a real directory"
        )
    if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o022:
        raise BrowserAdvisorError(
            "advisor control-root parent must be owner-controlled and not writable "
            "by group or other"
        )
    try:
        canonical_control_root = parent.resolve(strict=True) / control_root.name
        canonical_control_root.relative_to(project)
    except ValueError:
        pass
    else:
        raise BrowserAdvisorError(
            "advisor control root must be outside the project writable tree"
        )
    try:
        os.mkdir(control_root, mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise BrowserAdvisorError("could not create advisor control root") from exc

    try:
        root_visible = os.lstat(control_root)
    except OSError as exc:
        raise BrowserAdvisorError("advisor control root is unavailable") from exc
    if (
        stat.S_ISLNK(root_visible.st_mode)
        or not stat.S_ISDIR(root_visible.st_mode)
        or root_visible.st_uid != os.geteuid()
        or stat.S_IMODE(root_visible.st_mode) != 0o700
    ):
        raise BrowserAdvisorError(
            "advisor control root must be a private owner-controlled real directory"
        )

    lock_name = _project_fence_name(project_identity)
    path = control_root / lock_name
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    try:
        root_fd = os.open(control_root, directory_flags)
    except OSError as exc:
        raise BrowserAdvisorError("could not open advisor control root") from exc
    try:
        root_opened = os.fstat(root_fd)
        root_after_open = os.lstat(control_root)
        if (
            not stat.S_ISDIR(root_opened.st_mode)
            or root_opened.st_uid != os.geteuid()
            or stat.S_IMODE(root_opened.st_mode) != 0o700
            or (root_opened.st_dev, root_opened.st_ino)
            != (root_after_open.st_dev, root_after_open.st_ino)
        ):
            raise BrowserAdvisorError("advisor control root changed during open")
    except BaseException:
        os.close(root_fd)
        raise

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    with _process_lock(path):
        try:
            fd = os.open(lock_name, flags, 0o600, dir_fd=root_fd)
        except OSError as exc:
            os.close(root_fd)
            raise BrowserAdvisorError("could not open browser-output fence") from exc
        locked = False
        try:
            opened = os.fstat(fd)
            visible = os.stat(lock_name, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or not stat.S_ISREG(visible.st_mode)
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise BrowserAdvisorError(
                    "browser-output fence must be a private owner-controlled "
                    "unaliased regular file"
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            visible = os.stat(lock_name, dir_fd=root_fd, follow_symlinks=False)
            root_after_lock = os.lstat(control_root)
            if (
                opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
                or (root_opened.st_dev, root_opened.st_ino)
                != (root_after_lock.st_dev, root_after_lock.st_ino)
            ):
                raise BrowserAdvisorError("browser-output fence changed during lock")
            active_fences.add(project_identity)
            _FENCE_LOCAL.project_identities = active_fences
            try:
                yield
            finally:
                active_fences.remove(project_identity)
                try:
                    visible = os.stat(lock_name, dir_fd=root_fd, follow_symlinks=False)
                    root_after_use = os.lstat(control_root)
                    current = os.fstat(fd)
                except OSError as exc:
                    raise BrowserAdvisorError(
                        "browser-output fence changed while held"
                    ) from exc
                if (
                    current.st_nlink != 1
                    or (current.st_dev, current.st_ino)
                    != (visible.st_dev, visible.st_ino)
                    or (root_opened.st_dev, root_opened.st_ino)
                    != (root_after_use.st_dev, root_after_use.st_ino)
                ):
                    raise BrowserAdvisorError("browser-output fence changed while held")
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            os.close(root_fd)


class BrowserAdvisorError(RuntimeError):
    """Base class for browser-advisor broker failures."""


class BrowserAdvisorStateError(BrowserAdvisorError):
    """A requested state transition is not safe from the current state."""


class BrowserAdvisorConflict(BrowserAdvisorError):
    """An idempotency key or terminal receipt conflicts with durable content."""


def _assert_digest_absent_from_global_memory(
    project_dir: Path | str, *, digest: str
) -> None:
    """Fail if ``digest`` already names any durable GM string.

    The caller holds the supervisor project fence.  Gateway appends use the
    same fence, so this scan and the subsequent broker digest commit form one
    linearizable decision.  JSONL file locks additionally give a complete
    snapshot of each append-only channel.
    """

    digest = _validate_hash(digest, label="browser output hash")
    project = Path(project_dir).resolve(strict=True)
    memory_root = project / "global_memory"
    try:
        visible_root = os.lstat(memory_root)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BrowserAdvisorError("could not inspect global memory") from exc
    if stat.S_ISLNK(visible_root.st_mode) or not stat.S_ISDIR(visible_root.st_mode):
        raise BrowserAdvisorError("global memory must be a real directory")
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    try:
        root_fd = os.open(memory_root, directory_flags)
    except OSError as exc:
        raise BrowserAdvisorError("could not open global memory") from exc
    try:
        opened_root = os.fstat(root_fd)
        after_root = os.lstat(memory_root)
        if not stat.S_ISDIR(opened_root.st_mode) or (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != (after_root.st_dev, after_root.st_ino):
            raise BrowserAdvisorError("global memory changed during scan")
        try:
            names = sorted(os.listdir(root_fd))
        except OSError as exc:
            raise BrowserAdvisorError("could not enumerate global memory") from exc
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            try:
                fd = os.open(name, flags, dir_fd=root_fd)
            except OSError as exc:
                raise BrowserAdvisorError(
                    "could not open global-memory channel"
                ) from exc
            locked = False
            try:
                opened = os.fstat(fd)
                visible = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or not stat.S_ISREG(visible.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (visible.st_dev, visible.st_ino)
                ):
                    raise BrowserAdvisorError(
                        "global-memory channel must be an unaliased regular file"
                    )
                fcntl.flock(fd, fcntl.LOCK_SH)
                locked = True
                with os.fdopen(os.dup(fd), "rb") as stream:
                    while True:
                        raw_line = stream.readline(_MAX_GLOBAL_MEMORY_LINE_BYTES + 1)
                        if not raw_line:
                            break
                        if len(raw_line) > _MAX_GLOBAL_MEMORY_LINE_BYTES:
                            raise BrowserAdvisorError(
                                "global-memory record exceeds browser-output scan limit"
                            )
                        try:
                            text = raw_line.decode("utf-8").strip()
                        except UnicodeDecodeError as exc:
                            raise BrowserAdvisorError(
                                "global-memory channel is not valid UTF-8"
                            ) from exc
                        if not text:
                            continue
                        try:
                            record = json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise BrowserAdvisorError(
                                "global-memory channel contains invalid JSON"
                            ) from exc
                        if not isinstance(record, dict):
                            raise BrowserAdvisorError(
                                "global-memory channel contains a non-object record"
                            )
                        try:
                            record_digests = {
                                item_digest
                                for _, item_digest in _durable_string_digests(
                                    {"record": record}
                                )
                            }
                        except ValueError as exc:
                            raise BrowserAdvisorError(str(exc)) from exc
                        if digest in record_digests:
                            raise BrowserAdvisorConflict(
                                "browser output already exists in durable global memory"
                            )
                current = os.fstat(fd)
                visible = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if current.st_nlink != 1 or (current.st_dev, current.st_ino) != (
                    visible.st_dev,
                    visible.st_ino,
                ):
                    raise BrowserAdvisorError(
                        "global-memory channel changed during scan"
                    )
            finally:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        final_root = os.lstat(memory_root)
        if (opened_root.st_dev, opened_root.st_ino) != (
            final_root.st_dev,
            final_root.st_ino,
        ):
            raise BrowserAdvisorError("global memory changed during scan")
    finally:
        os.close(root_fd)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_utf8_text(
    value: object, *, label: str, max_bytes: int, allow_empty: bool = False
) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} must not be empty")
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes")
    return value, len(encoded)


def secret_markers(value: str) -> list[str]:
    """Return stable names for credential-shaped material in outbound text."""

    names = (
        "private_key",
        "openai_style_key",
        "authorization_header",
        "credential_assignment",
    )
    return [
        name for name, pattern in zip(names, _SECRET_PATTERNS) if pattern.search(value)
    ]


def _assert_no_secret_material(value: str, *, label: str) -> None:
    markers = secret_markers(value)
    if markers:
        raise ValueError(
            f"{label} contains credential-shaped material ({','.join(markers)}); "
            "refusing browser transmission or durable import"
        )


def control_signals(value: str) -> list[str]:
    """Classify privileged-looking text without granting it any authority."""

    return [name for name, pattern in _CONTROL_SIGNALS.items() if pattern.search(value)]


def _validate_hash(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_conversation_url(value: str) -> str:
    value, _ = _validate_utf8_text(value, label="conversation URL", max_bytes=4096)
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {
        "chatgpt.com",
        "www.chatgpt.com",
    }:
        raise ValueError("conversation URL must be an HTTPS chatgpt.com URL")
    if parsed.username or parsed.password:
        raise ValueError("conversation URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("conversation URL contains an invalid port") from exc
    if port not in {None, 443}:
        raise ValueError("conversation URL must use the default HTTPS port")
    return value


def _safe_optional_id(value: Optional[str], *, label: str) -> Optional[str]:
    if value is None:
        return None
    value, _ = _validate_utf8_text(value, label=label, max_bytes=512)
    if "\x00" in value:
        raise ValueError(f"{label} contains NUL")
    return value


def _validate_prepare_recommendation(
    project_dir: Path, recommendation_id: Optional[str]
) -> Optional[str]:
    """Bind reasoning-first prepares to one exact open recommendation.

    Legacy projects have no coordinator recommendation state and therefore keep
    the pre-existing unbound browser workflow.  Imports are deliberately local
    so the receipt broker remains usable for legacy projects without eagerly
    loading the coordination subsystem.
    """

    from danus.coordination import (
        CoordinationConfigError,
        CoordinationError,
        CoordinationStore,
        coordination_config,
    )
    from danus.coordination.store import load_project_metadata

    try:
        metadata = load_project_metadata(project_dir)
        config = coordination_config(metadata)
        if not config.reasoning_first:
            if recommendation_id is not None:
                raise BrowserAdvisorConflict(
                    "legacy browser-advisor requests cannot claim a coordinator "
                    "recommendation"
                )
            return None
        if recommendation_id is None:
            raise BrowserAdvisorStateError(
                "reasoning-first browser prepare requires an exact current "
                "recommendation id"
            )
        store = CoordinationStore.open_existing(project_dir, metadata)
        if store is None:
            raise BrowserAdvisorStateError(
                "reasoning-first browser prepare has no coordination store"
            )
        projection = store.validate_open_recommendation(recommendation_id)
    except (CoordinationConfigError, CoordinationError, OSError, sqlite3.Error) as exc:
        raise BrowserAdvisorStateError(
            "browser prepare recommendation is not the exact current open "
            "recommendation"
        ) from exc
    if (
        projection.get("recommendation_id") != recommendation_id
        or projection.get("state") != "owner_action_required"
        or projection.get("ready") is not True
        or projection.get("browser_dispatch_authorized") is not False
        or projection.get("advisor_request_id") is not None
    ):
        raise BrowserAdvisorConflict(
            "coordination returned an inexact open recommendation projection"
        )
    return recommendation_id


def _validate_prepare_checkpoint(
    project_dir: Path,
    *,
    prompt: str,
    recommendation_id: Optional[str],
    checkpoint_id: Optional[str],
    checkpoint_sha256: Optional[str],
    checkpoint_bytes: Optional[int],
) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Bind one reasoning-first prompt to its exact immutable GM checkpoint."""

    supplied = (checkpoint_id, checkpoint_sha256, checkpoint_bytes)
    if any(value is None for value in supplied):
        raise BrowserAdvisorStateError(
            "new browser prepare requires exact checkpoint id, digest, and byte count"
        )
    if (
        not isinstance(checkpoint_id, str)
        or _GLOBAL_MEMORY_ENTRY_ID_RE.fullmatch(checkpoint_id) is None
    ):
        raise ValueError("checkpoint id must be 16 lowercase hex characters")
    checkpoint_sha256 = _validate_hash(
        checkpoint_sha256,
        label="checkpoint hash",
    )
    if (
        isinstance(checkpoint_bytes, bool)
        or not isinstance(checkpoint_bytes, int)
        or checkpoint_bytes <= 0
        or checkpoint_bytes > 32 * 1024
    ):
        raise ValueError("checkpoint byte count is outside its bounded range")

    from danus.core import GlobalMemory, canonical_global_memory_record
    from danus.core.schema import validate_advisor_checkpoint

    memory = GlobalMemory(project_dir)
    try:
        checkpoint = memory.get_immutable_in_kind("advisor_checkpoint", checkpoint_id)
        canonical = canonical_global_memory_record(checkpoint)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BrowserAdvisorStateError(
            "browser prepare checkpoint is not one exact immutable project record"
        ) from exc
    links = checkpoint.get("links")
    author = checkpoint.get("author")
    if (
        checkpoint.get("id") != checkpoint_id
        or checkpoint.get("kind") != "advisor_checkpoint"
        or not isinstance(author, str)
        or not author.strip()
        or checkpoint.get("verifiable") is not False
        or checkpoint.get("status") != "open"
        or checkpoint.get("fact_id") is not None
        or not isinstance(links, dict)
    ):
        raise BrowserAdvisorConflict("advisor checkpoint immutable record is invalid")
    if recommendation_id is None:
        if "recommendation_id" in links or set(links) != {"fact_ids"}:
            raise BrowserAdvisorConflict(
                "legacy advisor checkpoint must omit the recommendation binding"
            )
    elif links.get("recommendation_id") != recommendation_id or set(links) != {
        "fact_ids",
        "recommendation_id",
    }:
        raise BrowserAdvisorConflict(
            "advisor checkpoint does not bind the exact current recommendation"
        )
    try:
        validate_advisor_checkpoint(
            checkpoint.get("claim"),
            checkpoint.get("evidence"),
            links,
        )
    except ValueError as exc:
        raise BrowserAdvisorConflict(
            "advisor checkpoint record violates its canonical schema"
        ) from exc
    if hashlib.sha256(canonical).hexdigest() != checkpoint_sha256:
        raise BrowserAdvisorConflict("advisor checkpoint digest changed")
    if len(canonical) != checkpoint_bytes:
        raise BrowserAdvisorConflict("advisor checkpoint byte count changed")
    if checkpoint.get("evidence") != prompt:
        raise BrowserAdvisorConflict(
            "advisor prompt must exactly equal the durable checkpoint evidence"
        )

    if recommendation_id is not None:
        matches: list[str] = []
        for projected in memory.iter_immutable("advisor_checkpoint"):
            projected_links = projected.get("links")
            if (
                isinstance(projected_links, dict)
                and projected_links.get("recommendation_id") == recommendation_id
            ):
                observed_id = projected.get("id")
                if (
                    not isinstance(observed_id, str)
                    or _GLOBAL_MEMORY_ENTRY_ID_RE.fullmatch(observed_id) is None
                ):
                    raise BrowserAdvisorConflict(
                        "advisor checkpoint set contains an invalid entry id"
                    )
                matches.append(observed_id)
        if matches != [checkpoint_id]:
            raise BrowserAdvisorConflict(
                "recommendation must have exactly one matching advisor checkpoint"
            )
    return checkpoint_id, checkpoint_sha256, checkpoint_bytes


def _secure_advisor_root(project_dir: Path) -> tuple[Path, Path]:
    try:
        project = Path(project_dir).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BrowserAdvisorError("project directory must exist") from exc
    if not project.is_dir():
        raise BrowserAdvisorError("project directory must be a directory")
    root = project / ".advisor"
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        try:
            os.mkdir(root, mode=0o700)
        except FileExistsError:
            pass
        root_stat = os.lstat(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise BrowserAdvisorError("advisor root must be a real directory")
    os.chmod(root, 0o700)
    return project, root


@contextmanager
def _read_existing_database(
    project_dir: Path | str, *, required: bool
) -> Iterator[Optional[sqlite3.Connection]]:
    """Open an existing broker database read-only without creating any path."""

    try:
        project = Path(project_dir).resolve(strict=True)
        project_info = os.lstat(project)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise BrowserAdvisorError("advisor project must exist") from exc
    if not stat.S_ISDIR(project_info.st_mode) or stat.S_ISLNK(project_info.st_mode):
        raise BrowserAdvisorError("advisor project must be a real directory")
    root = project / ".advisor"
    path = root / "browser-advisor.sqlite3"
    try:
        root_info = os.lstat(root)
        before = os.lstat(path)
    except (FileNotFoundError, OSError) as exc:
        if not required:
            yield None
            return
        raise BrowserAdvisorError(
            "browser-backed master guidance requires an existing same-project "
            "advisor ledger"
        ) from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise BrowserAdvisorError("advisor root must be a real directory")
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
    ):
        raise BrowserAdvisorError(
            "browser-advisor database must be an unaliased regular file"
        )
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        after = os.lstat(path)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise BrowserAdvisorError("browser-advisor database changed during open")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


class BrowserAdvisorBroker:
    """SQLite-backed, exact-CAS browser-advisor request ledger."""

    def __init__(self, project_dir: Path | str) -> None:
        self.project_dir, self.root = _secure_advisor_root(Path(project_dir))
        self.path = self.root / "browser-advisor.sqlite3"
        self._lock = _process_lock(self.path)
        self._validate_database_path(allow_missing=True)
        self._initialize()

    def _validate_database_path(
        self, *, allow_missing: bool
    ) -> Optional[tuple[int, int]]:
        try:
            item = os.lstat(self.path)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise BrowserAdvisorError("browser-advisor database disappeared")
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
        ):
            raise BrowserAdvisorError(
                "browser-advisor database must be an unaliased regular file"
            )
        return item.st_dev, item.st_ino

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            before = self._validate_database_path(allow_missing=True)
            db = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            try:
                after = self._validate_database_path(allow_missing=False)
                if before is not None and after != before:
                    raise BrowserAdvisorError(
                        "browser-advisor database changed during open"
                    )
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
            finally:
                db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS advisor_requests (
                    request_id TEXT PRIMARY KEY,
                    client_id TEXT UNIQUE,
                    context_id TEXT NOT NULL,
                    recommendation_id TEXT,
                    checkpoint_id TEXT,
                    checkpoint_sha256 TEXT,
                    checkpoint_bytes INTEGER,
                    binding_sha256 TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    prompt_bytes INTEGER NOT NULL,
                    elaboration_id TEXT,
                    lineage_kind TEXT NOT NULL DEFAULT 'new_chat'
                        CHECK(lineage_kind IN ('new_chat','local_predecessor')),
                    predecessor_request_id TEXT REFERENCES advisor_requests(request_id),
                    predecessor_conversation_url_sha256 TEXT,
                    predecessor_receipt_sha256 TEXT,
                    predecessor_state TEXT,
                    lineage_root_request_id TEXT,
                    lineage_depth INTEGER NOT NULL DEFAULT 0,
                    receipt_schema_version INTEGER NOT NULL DEFAULT 5,
                    state TEXT NOT NULL CHECK(state IN
                        ('prepared','authorized','dispatching','submitted',
                         'completed','needs_user_input','delivery_unknown','imported',
                         'adopted','failed_not_submitted','abandoned',
                         'owner_abandoned_outcome_unknown')),
                    authorization_scope TEXT,
                    authorization_scope_sha256 TEXT,
                    authorized_ns INTEGER,
                    pre_click_token_sha256 TEXT,
                    ui_mode TEXT,
                    conversation_url_sha256 TEXT,
                    reply_sha256 TEXT,
                    reply_bytes INTEGER,
                    stable_snapshots INTEGER,
                    completion_actions_observed INTEGER,
                    composer_available INTEGER,
                    working_indicator_absent INTEGER,
                    control_signals_json TEXT,
                    terminal_reason_sha256 TEXT,
                    terminal_evidence_sha256 TEXT,
                    terminal_acknowledgement INTEGER,
                    terminal_prior_state TEXT,
                    adopted_strategy TEXT,
                    adopted_strategy_sha256 TEXT,
                    adopted_ns INTEGER,
                    created_ns INTEGER NOT NULL,
                    updated_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS advisor_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL REFERENCES advisor_requests(request_id),
                    state TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_ns INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_advisor_prompt_state
                    ON advisor_requests(prompt_sha256,state,created_ns);
                CREATE INDEX IF NOT EXISTS idx_advisor_events_request
                    ON advisor_events(request_id,seq);
                COMMIT;
                """
            )
            # Reopened development builds briefly used a plaintext ``reply``
            # column.  Scrub it before any read and keep the compatibility
            # column unused if such a pre-release database is encountered.
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(advisor_requests)")
            }
            for name, declaration in (
                ("terminal_reason_sha256", "TEXT"),
                ("terminal_evidence_sha256", "TEXT"),
                ("terminal_acknowledgement", "INTEGER"),
                ("terminal_prior_state", "TEXT"),
                ("recommendation_id", "TEXT"),
                ("checkpoint_id", "TEXT"),
                ("checkpoint_sha256", "TEXT"),
                ("checkpoint_bytes", "INTEGER"),
                (
                    "lineage_kind",
                    "TEXT NOT NULL DEFAULT 'new_chat' "
                    "CHECK(lineage_kind IN ('new_chat','local_predecessor'))",
                ),
                ("predecessor_request_id", "TEXT"),
                ("predecessor_conversation_url_sha256", "TEXT"),
                ("predecessor_receipt_sha256", "TEXT"),
                ("predecessor_state", "TEXT"),
                ("lineage_root_request_id", "TEXT"),
                ("lineage_depth", "INTEGER NOT NULL DEFAULT 0"),
                # Existing receipts keep their exact v2/v3 hash. Every request
                # created by this build explicitly stores version 5 below.
                ("receipt_schema_version", "INTEGER NOT NULL DEFAULT 2"),
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE advisor_requests ADD COLUMN {name} {declaration}"
                    )
            db.execute(
                "UPDATE advisor_requests SET lineage_root_request_id=request_id "
                "WHERE lineage_kind='new_chat' AND lineage_root_request_id IS NULL"
            )
            recommendation_index = next(
                (
                    row
                    for row in db.execute("PRAGMA index_list(advisor_requests)")
                    if str(row[1]) == "idx_advisor_recommendation"
                ),
                None,
            )
            if recommendation_index is not None and int(recommendation_index[2]) != 1:
                # Replace the briefly shipped development-only non-unique index.
                # Creation below deliberately fails closed if that database has
                # already accumulated duplicate non-null recommendation ids.
                db.execute("DROP INDEX idx_advisor_recommendation")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_advisor_recommendation "
                "ON advisor_requests(recommendation_id) "
                "WHERE recommendation_id IS NOT NULL"
            )
            checkpoint_index = next(
                (
                    row
                    for row in db.execute("PRAGMA index_list(advisor_requests)")
                    if str(row[1]) == "idx_advisor_checkpoint"
                ),
                None,
            )
            if checkpoint_index is not None and int(checkpoint_index[2]) != 1:
                db.execute("DROP INDEX idx_advisor_checkpoint")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_advisor_checkpoint "
                "ON advisor_requests(checkpoint_id) "
                "WHERE checkpoint_id IS NOT NULL"
            )
            if "reply" in columns:
                has_plaintext = db.execute(
                    "SELECT 1 FROM advisor_requests WHERE reply IS NOT NULL LIMIT 1"
                ).fetchone()
                if has_plaintext is not None:
                    db.execute("PRAGMA secure_delete=ON")
                    db.execute(
                        "UPDATE advisor_requests SET reply=NULL WHERE reply IS NOT NULL"
                    )
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    db.execute("VACUUM")
            db.execute("PRAGMA user_version=5")
        os.chmod(self.path, 0o600)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _receipt_sha256(row: dict[str, Any]) -> str:
        """Commit the canonical owner/authorization/delivery receipt fields."""

        receipt_schema_version = int(row.get("receipt_schema_version") or 2)
        if receipt_schema_version not in {2, 3, 4, 5}:
            raise BrowserAdvisorConflict("unsupported browser-advisor receipt schema")
        payload = {
            "schema_version": receipt_schema_version,
            "transport": CHATGPT_BROWSER_TRANSPORT,
            "request_id": row["request_id"],
            "state": row["state"],
            "context_id": row["context_id"],
            "binding_sha256": row["binding_sha256"],
            "prompt_sha256": row["prompt_sha256"],
            "authorization_scope_sha256": row["authorization_scope_sha256"],
            "authorized_ns": row["authorized_ns"],
            "pre_click_token_sha256": row["pre_click_token_sha256"],
            "ui_mode": row["ui_mode"],
            "conversation_url_sha256": row["conversation_url_sha256"],
            "reply_sha256": row["reply_sha256"],
            "reply_bytes": row["reply_bytes"],
            "stable_snapshots": row["stable_snapshots"],
            "completion_actions_observed": row["completion_actions_observed"],
            "composer_available": row["composer_available"],
            "working_indicator_absent": row["working_indicator_absent"],
            "terminal_reason_sha256": row["terminal_reason_sha256"],
            "terminal_evidence_sha256": row["terminal_evidence_sha256"],
            "terminal_acknowledgement": row["terminal_acknowledgement"],
            "terminal_prior_state": row["terminal_prior_state"],
            "adopted_strategy_sha256": row["adopted_strategy_sha256"],
        }
        if receipt_schema_version >= 3:
            payload.update(
                {
                    "lineage_kind": row["lineage_kind"],
                    "predecessor_request_id": row["predecessor_request_id"],
                    "predecessor_conversation_url_sha256": row[
                        "predecessor_conversation_url_sha256"
                    ],
                    "predecessor_receipt_sha256": row["predecessor_receipt_sha256"],
                    "predecessor_state": row["predecessor_state"],
                    "lineage_root_request_id": row["lineage_root_request_id"],
                    "lineage_depth": row["lineage_depth"],
                }
            )
        if receipt_schema_version >= 4:
            payload["recommendation_id"] = row["recommendation_id"]
        if receipt_schema_version >= 5:
            payload.update(
                {
                    "checkpoint_id": row["checkpoint_id"],
                    "checkpoint_sha256": row["checkpoint_sha256"],
                    "checkpoint_bytes": row["checkpoint_bytes"],
                    "prompt_bytes": row["prompt_bytes"],
                }
            )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        request_id: str,
        state: str,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        if state not in _STATES:
            raise AssertionError(state)
        encoded = json.dumps(
            detail or {},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("advisor event detail exceeds hard limit")
        db.execute(
            "INSERT INTO advisor_events(request_id,state,detail_json,created_ns) "
            "VALUES(?,?,?,?)",
            (request_id, state, encoded, time.time_ns()),
        )

    def _get_in_tx(self, db: sqlite3.Connection, request_id: str) -> dict[str, Any]:
        request_id = _safe_optional_id(request_id, label="request id") or ""
        row = db.execute(
            "SELECT * FROM advisor_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise BrowserAdvisorError("unknown browser-advisor request")
        result = self._row(row)
        self._assert_v5_request_integrity(result)
        return result

    def _assert_v5_request_integrity(self, row: dict[str, Any]) -> None:
        """Fail closed if a checkpoint-bound broker row was locally altered."""

        if int(row.get("receipt_schema_version") or 2) < 5:
            return
        prompt = row.get("prompt")
        if not isinstance(prompt, str):
            raise BrowserAdvisorConflict("checkpoint-bound request lost its prompt")
        try:
            prompt_bytes = prompt.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BrowserAdvisorConflict(
                "checkpoint-bound request prompt is not valid UTF-8"
            ) from exc
        if row.get("prompt_sha256") != hashlib.sha256(
            prompt_bytes
        ).hexdigest() or row.get("prompt_bytes") != len(prompt_bytes):
            raise BrowserAdvisorConflict(
                "checkpoint-bound request prompt integrity check failed"
            )
        checkpoint_id = row.get("checkpoint_id")
        checkpoint_sha256 = row.get("checkpoint_sha256")
        checkpoint_bytes = row.get("checkpoint_bytes")
        if (
            not isinstance(checkpoint_id, str)
            or _GLOBAL_MEMORY_ENTRY_ID_RE.fullmatch(checkpoint_id) is None
            or not isinstance(checkpoint_sha256, str)
            or _SHA256_RE.fullmatch(checkpoint_sha256) is None
            or isinstance(checkpoint_bytes, bool)
            or not isinstance(checkpoint_bytes, int)
            or checkpoint_bytes <= 0
            or checkpoint_bytes > 32 * 1024
        ):
            raise BrowserAdvisorConflict(
                "checkpoint-bound request identity is malformed"
            )
        expected_binding = self._binding_sha256(
            project_dir=self.project_dir,
            elaboration_id=row.get("elaboration_id"),
            context_id=str(row.get("context_id") or ""),
            recommendation_id=row.get("recommendation_id"),
            checkpoint_id=checkpoint_id,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_bytes=checkpoint_bytes,
            prompt_sha256=str(row["prompt_sha256"]),
            prompt_bytes=len(prompt_bytes),
            lineage_kind=str(row.get("lineage_kind")),
            predecessor_request_id=row.get("predecessor_request_id"),
            predecessor_conversation_url_sha256=row.get(
                "predecessor_conversation_url_sha256"
            ),
        )
        if row.get("binding_sha256") != expected_binding:
            raise BrowserAdvisorConflict(
                "checkpoint-bound request binding integrity check failed"
            )

    @contextmanager
    def _checkpoint_fact_snapshot(self, checkpoint_id: str) -> Iterator[None]:
        """Hold the checkpoint's exact active FactGraph projection."""

        from danus.core import FactGraph, GlobalMemory

        checkpoint = GlobalMemory(self.project_dir).get_immutable_in_kind(
            "advisor_checkpoint", checkpoint_id
        )
        links = checkpoint.get("links")
        fact_ids = links.get("fact_ids") if isinstance(links, dict) else None
        if not isinstance(fact_ids, list):
            raise BrowserAdvisorConflict(
                "advisor checkpoint lost its exact verified fact set"
            )
        graph = FactGraph(self.project_dir)
        with graph.locked_context(
            fact_ids,
            predecessor_depth=0,
            proof_mode="none",
            include_project_glossary=False,
        ) as context:
            active_ids = {str(item["fact_id"]) for item in context["facts"]}
            if not context["complete"] or active_ids != set(fact_ids):
                raise BrowserAdvisorStateError(
                    "advisor checkpoint facts changed before browser dispatch"
                )
            yield

    @contextmanager
    def _v5_dispatch_checkpoint_snapshot(self, row: dict[str, Any]) -> Iterator[None]:
        """Hold exact current checkpoint and FactGraph authority through Send CAS."""

        if int(row.get("receipt_schema_version") or 2) < 5:
            raise BrowserAdvisorStateError(
                "pre-v5 unbound advisor request cannot authorize a new Send"
            )
        recommendation_id = _validate_prepare_recommendation(
            self.project_dir,
            row.get("recommendation_id"),
        )
        checkpoint_id, _, _ = _validate_prepare_checkpoint(
            self.project_dir,
            prompt=str(row["prompt"]),
            recommendation_id=recommendation_id,
            checkpoint_id=row.get("checkpoint_id"),
            checkpoint_sha256=row.get("checkpoint_sha256"),
            checkpoint_bytes=row.get("checkpoint_bytes"),
        )
        assert checkpoint_id is not None
        with self._checkpoint_fact_snapshot(checkpoint_id):
            yield

    @staticmethod
    def _binding_sha256(
        *,
        project_dir: Path,
        elaboration_id: Optional[str],
        context_id: str,
        recommendation_id: Optional[str],
        checkpoint_id: Optional[str],
        checkpoint_sha256: Optional[str],
        checkpoint_bytes: Optional[int],
        prompt_sha256: str,
        prompt_bytes: int,
        lineage_kind: str,
        predecessor_request_id: Optional[str],
        predecessor_conversation_url_sha256: Optional[str],
    ) -> str:
        if checkpoint_id is None and recommendation_id is None:
            # Preserve the exact pre-recommendation binding domain so a v3
            # receipt reopened by this build remains client-id replayable.
            # Adding a null field would silently change the digest even though
            # the durable request predates coordinator recommendation binding.
            material: list[Optional[str]] = [
                "browser-advisor-binding-v2",
                _sha256_text(os.fspath(project_dir)),
                elaboration_id,
                context_id,
                prompt_sha256,
                lineage_kind,
                predecessor_request_id,
                predecessor_conversation_url_sha256,
            ]
        elif checkpoint_id is None:
            material = [
                "browser-advisor-binding-v3",
                _sha256_text(os.fspath(project_dir)),
                elaboration_id,
                context_id,
                recommendation_id,
                prompt_sha256,
                lineage_kind,
                predecessor_request_id,
                predecessor_conversation_url_sha256,
            ]
        else:
            material = [
                "browser-advisor-binding-v4",
                _sha256_text(os.fspath(project_dir)),
                elaboration_id,
                context_id,
                recommendation_id,
                checkpoint_id,
                checkpoint_sha256,
                str(checkpoint_bytes),
                prompt_sha256,
                str(prompt_bytes),
                lineage_kind,
                predecessor_request_id,
                predecessor_conversation_url_sha256,
            ]
        return hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _assert_lineage_in_tx(
        self,
        db: sqlite3.Connection,
        row: dict[str, Any],
        *,
        require_conversation_head: bool = True,
    ) -> None:
        """Fail closed unless a stored local lineage is still coherent.

        A predecessor snapshot may advance from ``completed`` through import and
        adoption, but it may never become ambiguous or change conversation.
        Direct follow-ups are serialized so an older node cannot be reused after
        a later response has already extended the physical conversation.
        """

        lineage_kind = row.get("lineage_kind")
        if lineage_kind not in _LINEAGE_KINDS:
            raise BrowserAdvisorConflict("browser-advisor lineage kind is invalid")
        predecessor_fields = (
            row.get("predecessor_request_id"),
            row.get("predecessor_conversation_url_sha256"),
            row.get("predecessor_receipt_sha256"),
            row.get("predecessor_state"),
        )
        if lineage_kind == "new_chat":
            if any(value is not None for value in predecessor_fields):
                raise BrowserAdvisorConflict(
                    "new-chat receipt contains predecessor-only lineage fields"
                )
            if row.get("lineage_root_request_id") != row["request_id"]:
                raise BrowserAdvisorConflict("new-chat lineage root is invalid")
            if row.get("lineage_depth") != 0:
                raise BrowserAdvisorConflict("new-chat lineage depth is invalid")
            return

        predecessor_request_id = row.get("predecessor_request_id")
        if not isinstance(predecessor_request_id, str) or not predecessor_request_id:
            raise BrowserAdvisorConflict("local continuation has no predecessor")
        if predecessor_request_id == row["request_id"]:
            raise BrowserAdvisorConflict(
                "browser-advisor lineage cannot reference itself"
            )
        predecessor_url_sha256 = _validate_hash(
            row.get("predecessor_conversation_url_sha256"),
            label="predecessor conversation URL hash",
        )
        _validate_hash(
            row.get("predecessor_receipt_sha256"),
            label="predecessor receipt hash",
        )
        if row.get("predecessor_state") not in _CONTINUATION_PREDECESSOR_STATES:
            raise BrowserAdvisorConflict(
                "predecessor state snapshot is not continuable"
            )

        predecessor = self._get_in_tx(db, predecessor_request_id)
        if predecessor["context_id"] != row["context_id"]:
            raise BrowserAdvisorConflict(
                "predecessor belongs to a different advisor context"
            )
        if predecessor["state"] not in _CONTINUATION_PREDECESSOR_STATES:
            raise BrowserAdvisorStateError(
                f"predecessor is not a known terminal response ({predecessor['state']})"
            )
        if predecessor["conversation_url_sha256"] != predecessor_url_sha256:
            raise BrowserAdvisorConflict("predecessor conversation URL binding changed")
        if predecessor["prompt_sha256"] == row["prompt_sha256"]:
            raise BrowserAdvisorStateError(
                "a continuation must use a new evidence-specific prompt, not resend "
                "its predecessor prompt"
            )
        predecessor_root = (
            predecessor.get("lineage_root_request_id") or predecessor["request_id"]
        )
        predecessor_depth = int(predecessor.get("lineage_depth") or 0)
        if row.get("lineage_root_request_id") != predecessor_root:
            raise BrowserAdvisorConflict("continuation lineage root changed")
        if row.get("lineage_depth") != predecessor_depth + 1:
            raise BrowserAdvisorConflict("continuation lineage depth changed")

        if not require_conversation_head:
            return

        superseding = db.execute(
            "SELECT request_id,state FROM advisor_requests "
            "WHERE predecessor_request_id=? AND request_id<>? AND state NOT IN "
            "('failed_not_submitted','abandoned') ORDER BY created_ns LIMIT 1",
            (predecessor_request_id, row["request_id"]),
        ).fetchone()
        if superseding is not None:
            raise BrowserAdvisorStateError(
                "predecessor already has another active, delivered, or "
                "outcome-unknown follow-up"
            )

        placeholders = ",".join("?" for _ in _ACTIVE_CONVERSATION_STATES)
        active = db.execute(
            "SELECT request_id,state FROM advisor_requests WHERE request_id<>? AND "
            "(conversation_url_sha256=? OR "
            "predecessor_conversation_url_sha256=?) AND state IN ("
            + placeholders
            + ") ORDER BY created_ns LIMIT 1",
            (
                row["request_id"],
                predecessor_url_sha256,
                predecessor_url_sha256,
                *sorted(_ACTIVE_CONVERSATION_STATES),
            ),
        ).fetchone()
        if active is not None:
            raise BrowserAdvisorStateError(
                "the predecessor conversation already has an active or "
                "outcome-unknown intervention"
            )

    def _assert_delivery_conversation_in_tx(
        self,
        db: sqlite3.Connection,
        row: dict[str, Any],
        *,
        conversation_url_sha256: str,
        require_conversation_head: bool = True,
    ) -> None:
        self._assert_lineage_in_tx(
            db,
            row,
            require_conversation_head=require_conversation_head,
        )
        if row["lineage_kind"] == "local_predecessor":
            if row["predecessor_conversation_url_sha256"] != conversation_url_sha256:
                raise BrowserAdvisorConflict(
                    "continuation did not use its predecessor conversation URL"
                )
            return
        if not require_conversation_head:
            # Exact terminal replay is a current-row integrity operation.  A
            # later local descendant necessarily shares this new-chat root URL
            # and must not make the root's canonical receipt unavailable.
            # First terminal transitions call this helper again with the full
            # cross-row uniqueness/head gate below.
            return
        existing = db.execute(
            "SELECT request_id FROM advisor_requests WHERE request_id<>? AND "
            "conversation_url_sha256=? LIMIT 1",
            (row["request_id"], conversation_url_sha256),
        ).fetchone()
        if existing is not None:
            raise BrowserAdvisorConflict(
                "an existing ChatGPT conversation requires an explicit local "
                "predecessor; new-chat receipts cannot reuse it"
            )

    def prepare(
        self,
        prompt: str,
        *,
        elaboration_id: Optional[str] = None,
        client_id: Optional[str] = None,
        context_id: str,
        recommendation_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        checkpoint_sha256: Optional[str] = None,
        checkpoint_bytes: Optional[int] = None,
        predecessor_request_id: Optional[str] = None,
        predecessor_conversation_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate and persist one request under the owner-resolution fence."""

        with self.project_memory_fence(self.project_dir):
            return self._prepare_locked(
                prompt,
                elaboration_id=elaboration_id,
                client_id=client_id,
                context_id=context_id,
                recommendation_id=recommendation_id,
                checkpoint_id=checkpoint_id,
                checkpoint_sha256=checkpoint_sha256,
                checkpoint_bytes=checkpoint_bytes,
                predecessor_request_id=predecessor_request_id,
                predecessor_conversation_url=predecessor_conversation_url,
            )

    def _prepare_locked(
        self,
        prompt: str,
        *,
        elaboration_id: Optional[str] = None,
        client_id: Optional[str] = None,
        context_id: str,
        recommendation_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        checkpoint_sha256: Optional[str] = None,
        checkpoint_bytes: Optional[int] = None,
        predecessor_request_id: Optional[str] = None,
        predecessor_conversation_url: Optional[str] = None,
    ) -> dict[str, Any]:
        prompt, prompt_bytes = _validate_utf8_text(
            prompt, label="advisor prompt", max_bytes=MAX_PROMPT_BYTES
        )
        _assert_no_secret_material(prompt, label="advisor prompt")
        elaboration_id = _safe_optional_id(elaboration_id, label="elaboration id")
        client_id = _safe_optional_id(client_id, label="client id")
        context_id = _safe_optional_id(context_id, label="context id") or ""
        recommendation_id = _safe_optional_id(
            recommendation_id, label="recommendation id"
        )
        predecessor_request_id = _safe_optional_id(
            predecessor_request_id, label="predecessor request id"
        )
        if (predecessor_request_id is None) != (predecessor_conversation_url is None):
            raise ValueError(
                "local continuation requires both predecessor request id and "
                "predecessor conversation URL; a new chat requires neither"
            )
        lineage_kind = (
            "local_predecessor" if predecessor_request_id is not None else "new_chat"
        )
        predecessor_conversation_url_sha256: Optional[str] = None
        if predecessor_conversation_url is not None:
            predecessor_conversation_url = _validate_conversation_url(
                predecessor_conversation_url
            )
            predecessor_conversation_url_sha256 = _sha256_text(
                predecessor_conversation_url
            )
        prompt_sha256 = _sha256_text(prompt)
        checkpoint_values = (checkpoint_id, checkpoint_sha256, checkpoint_bytes)
        checkpoint_shape_complete = all(
            value is not None for value in checkpoint_values
        )
        checkpoint_shape_empty = all(value is None for value in checkpoint_values)
        prospective_binding: Optional[str] = None
        if checkpoint_shape_complete or checkpoint_shape_empty:
            prospective_binding = self._binding_sha256(
                project_dir=self.project_dir,
                elaboration_id=elaboration_id,
                context_id=context_id,
                recommendation_id=recommendation_id,
                checkpoint_id=checkpoint_id,
                checkpoint_sha256=checkpoint_sha256,
                checkpoint_bytes=checkpoint_bytes,
                prompt_sha256=prompt_sha256,
                prompt_bytes=prompt_bytes,
                lineage_kind=lineage_kind,
                predecessor_request_id=predecessor_request_id,
                predecessor_conversation_url_sha256=(
                    predecessor_conversation_url_sha256
                ),
            )
        with self._connect() as replay_db:
            replay_row = None
            if client_id is not None:
                replay_row = replay_db.execute(
                    "SELECT * FROM advisor_requests WHERE client_id=?",
                    (client_id,),
                ).fetchone()
            elif recommendation_id is not None:
                replay_row = replay_db.execute(
                    "SELECT * FROM advisor_requests WHERE recommendation_id=?",
                    (recommendation_id,),
                ).fetchone()
            elif prospective_binding is not None:
                replay_rows = replay_db.execute(
                    "SELECT * FROM advisor_requests WHERE binding_sha256=? LIMIT 2",
                    (prospective_binding,),
                ).fetchall()
                if len(replay_rows) > 1:
                    raise BrowserAdvisorConflict(
                        "advisor binding has multiple durable requests"
                    )
                replay_row = replay_rows[0] if replay_rows else None
            if replay_row is not None:
                existing = self._row(replay_row)
                receipt_version = int(existing.get("receipt_schema_version") or 2)
                exact = (
                    prospective_binding is not None
                    and existing["prompt"] == prompt
                    and existing["prompt_sha256"] == prompt_sha256
                    and existing["prompt_bytes"] == prompt_bytes
                    and existing["elaboration_id"] == elaboration_id
                    and existing["context_id"] == context_id
                    and existing["recommendation_id"] == recommendation_id
                    and existing["binding_sha256"] == prospective_binding
                    and existing["lineage_kind"] == lineage_kind
                    and existing["predecessor_request_id"] == predecessor_request_id
                    and existing["predecessor_conversation_url_sha256"]
                    == predecessor_conversation_url_sha256
                )
                if receipt_version >= 5:
                    exact = exact and (
                        existing["checkpoint_id"] == checkpoint_id
                        and existing["checkpoint_sha256"] == checkpoint_sha256
                        and existing["checkpoint_bytes"] == checkpoint_bytes
                    )
                else:
                    exact = exact and checkpoint_shape_empty
                if not exact:
                    raise BrowserAdvisorConflict(
                        "existing advisor request belongs to different content"
                    )
                self._assert_v5_request_integrity(existing)
                if receipt_version >= 5:
                    _validate_prepare_checkpoint(
                        self.project_dir,
                        prompt=prompt,
                        recommendation_id=recommendation_id,
                        checkpoint_id=checkpoint_id,
                        checkpoint_sha256=checkpoint_sha256,
                        checkpoint_bytes=checkpoint_bytes,
                    )
                self._assert_lineage_in_tx(
                    replay_db,
                    existing,
                    require_conversation_head=False,
                )
                return self._public(existing, include_prompt=True)

        recommendation_id = _validate_prepare_recommendation(
            self.project_dir, recommendation_id
        )
        checkpoint_id, checkpoint_sha256, checkpoint_bytes = (
            _validate_prepare_checkpoint(
                self.project_dir,
                prompt=prompt,
                recommendation_id=recommendation_id,
                checkpoint_id=checkpoint_id,
                checkpoint_sha256=checkpoint_sha256,
                checkpoint_bytes=checkpoint_bytes,
            )
        )
        binding_sha256 = self._binding_sha256(
            project_dir=self.project_dir,
            elaboration_id=elaboration_id,
            context_id=context_id,
            recommendation_id=recommendation_id,
            checkpoint_id=checkpoint_id,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_bytes=checkpoint_bytes,
            prompt_sha256=prompt_sha256,
            prompt_bytes=prompt_bytes,
            lineage_kind=lineage_kind,
            predecessor_request_id=predecessor_request_id,
            predecessor_conversation_url_sha256=(predecessor_conversation_url_sha256),
        )
        now = time.time_ns()
        assert checkpoint_id is not None
        with ExitStack() as stack:
            stack.enter_context(self._checkpoint_fact_snapshot(checkpoint_id))
            db = stack.enter_context(self._connect())
            db.execute("BEGIN IMMEDIATE")
            if client_id is not None:
                prior = db.execute(
                    "SELECT * FROM advisor_requests WHERE client_id=?", (client_id,)
                ).fetchone()
                if prior is not None:
                    existing = self._row(prior)
                    if (
                        existing["prompt_sha256"] != prompt_sha256
                        or existing["elaboration_id"] != elaboration_id
                        or existing["context_id"] != context_id
                        or existing["recommendation_id"] != recommendation_id
                        or existing["checkpoint_id"] != checkpoint_id
                        or existing["checkpoint_sha256"] != checkpoint_sha256
                        or existing["checkpoint_bytes"] != checkpoint_bytes
                        or existing["binding_sha256"] != binding_sha256
                        or existing["lineage_kind"] != lineage_kind
                        or existing["predecessor_request_id"] != predecessor_request_id
                        or existing["predecessor_conversation_url_sha256"]
                        != predecessor_conversation_url_sha256
                    ):
                        raise BrowserAdvisorConflict(
                            "client id already belongs to different advisor content"
                        )
                    self._assert_lineage_in_tx(
                        db, existing, require_conversation_head=False
                    )
                    return self._public(existing, include_prompt=True)
            if recommendation_id is not None:
                prior = db.execute(
                    "SELECT * FROM advisor_requests WHERE recommendation_id=?",
                    (recommendation_id,),
                ).fetchone()
                if prior is not None:
                    existing = self._row(prior)
                    if (
                        existing["prompt_sha256"] == prompt_sha256
                        and existing["elaboration_id"] == elaboration_id
                        and existing["context_id"] == context_id
                        and existing["checkpoint_id"] == checkpoint_id
                        and existing["checkpoint_sha256"] == checkpoint_sha256
                        and existing["checkpoint_bytes"] == checkpoint_bytes
                        and existing["binding_sha256"] == binding_sha256
                        and existing["lineage_kind"] == lineage_kind
                        and existing["predecessor_request_id"] == predecessor_request_id
                        and existing["predecessor_conversation_url_sha256"]
                        == predecessor_conversation_url_sha256
                    ):
                        self._assert_lineage_in_tx(
                            db, existing, require_conversation_head=False
                        )
                        return self._public(existing, include_prompt=True)
                    raise BrowserAdvisorConflict(
                        "coordinator recommendation already has a different "
                        "browser-advisor request"
                    )
            active = db.execute(
                "SELECT * FROM advisor_requests WHERE binding_sha256=? AND state NOT IN "
                "('imported','adopted','failed_not_submitted','abandoned',"
                "'owner_abandoned_outcome_unknown','needs_user_input') "
                "ORDER BY created_ns DESC LIMIT 1",
                (binding_sha256,),
            ).fetchone()
            if active is not None:
                existing = self._row(active)
                self._assert_lineage_in_tx(db, existing)
                return self._public(existing, include_prompt=True)
            active_prompt = db.execute(
                "SELECT * FROM advisor_requests WHERE prompt_sha256=? AND state NOT IN "
                "('imported','adopted','failed_not_submitted','abandoned',"
                "'owner_abandoned_outcome_unknown','needs_user_input') "
                "ORDER BY created_ns DESC LIMIT 1",
                (prompt_sha256,),
            ).fetchone()
            if active_prompt is not None:
                existing = self._row(active_prompt)
                raise BrowserAdvisorStateError(
                    "the same prompt already has an active request in state "
                    f"{existing['state']} under a different context binding"
                )
            unresolved = db.execute(
                "SELECT request_id FROM advisor_requests WHERE prompt_sha256=? AND "
                "state='owner_abandoned_outcome_unknown' "
                "ORDER BY created_ns DESC LIMIT 1",
                (prompt_sha256,),
            ).fetchone()
            if unresolved is not None:
                raise BrowserAdvisorStateError(
                    "this exact prompt has an outcome-unknown prior submission and "
                    "may never be fresh-sent again"
                )
            predecessor_receipt_sha256: Optional[str] = None
            predecessor_state: Optional[str] = None
            lineage_root_request_id: Optional[str]
            lineage_depth: int
            if lineage_kind == "local_predecessor":
                assert predecessor_request_id is not None
                assert predecessor_conversation_url_sha256 is not None
                predecessor = self._get_in_tx(db, predecessor_request_id)
                if predecessor["context_id"] != context_id:
                    raise BrowserAdvisorConflict(
                        "predecessor belongs to a different advisor context"
                    )
                if predecessor["state"] not in _CONTINUATION_PREDECESSOR_STATES:
                    raise BrowserAdvisorStateError(
                        "predecessor must have a known terminal browser response; "
                        f"state {predecessor['state']} cannot be continued"
                    )
                if (
                    predecessor["conversation_url_sha256"]
                    != predecessor_conversation_url_sha256
                ):
                    raise BrowserAdvisorConflict(
                        "supplied conversation URL does not match predecessor"
                    )
                if predecessor["prompt_sha256"] == prompt_sha256:
                    raise BrowserAdvisorStateError(
                        "a continuation must use a new evidence-specific prompt, "
                        "not resend its predecessor prompt"
                    )
                predecessor_receipt_sha256 = self._receipt_sha256(predecessor)
                predecessor_state = str(predecessor["state"])
                lineage_root_request_id = (
                    predecessor.get("lineage_root_request_id")
                    or predecessor["request_id"]
                )
                lineage_depth = int(predecessor.get("lineage_depth") or 0) + 1
                prospective = {
                    "request_id": "<prospective-request>",
                    "prompt_sha256": prompt_sha256,
                    "context_id": context_id,
                    "lineage_kind": lineage_kind,
                    "predecessor_request_id": predecessor_request_id,
                    "predecessor_conversation_url_sha256": (
                        predecessor_conversation_url_sha256
                    ),
                    "predecessor_receipt_sha256": predecessor_receipt_sha256,
                    "predecessor_state": predecessor_state,
                    "lineage_root_request_id": lineage_root_request_id,
                    "lineage_depth": lineage_depth,
                }
                self._assert_lineage_in_tx(db, prospective)
            else:
                lineage_root_request_id = None
                lineage_depth = 0
            request_id = str(uuid.uuid4())
            if lineage_root_request_id is None:
                lineage_root_request_id = request_id
            db.execute(
                "INSERT INTO advisor_requests("
                "request_id,client_id,context_id,recommendation_id,"
                "checkpoint_id,checkpoint_sha256,checkpoint_bytes,binding_sha256,"
                "prompt,prompt_sha256,prompt_bytes,"
                "elaboration_id,lineage_kind,predecessor_request_id,"
                "predecessor_conversation_url_sha256,"
                "predecessor_receipt_sha256,predecessor_state,"
                "lineage_root_request_id,lineage_depth,receipt_schema_version,"
                "state,created_ns,updated_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    client_id,
                    context_id,
                    recommendation_id,
                    checkpoint_id,
                    checkpoint_sha256,
                    checkpoint_bytes,
                    binding_sha256,
                    prompt,
                    prompt_sha256,
                    prompt_bytes,
                    elaboration_id,
                    lineage_kind,
                    predecessor_request_id,
                    predecessor_conversation_url_sha256,
                    predecessor_receipt_sha256,
                    predecessor_state,
                    lineage_root_request_id,
                    lineage_depth,
                    5,
                    "prepared",
                    now,
                    now,
                ),
            )
            self._event(
                db,
                request_id,
                "prepared",
                {
                    "prompt_sha256": prompt_sha256,
                    "prompt_bytes": prompt_bytes,
                    "elaboration_id": elaboration_id,
                    "context_id": context_id,
                    "recommendation_id": recommendation_id,
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_sha256": checkpoint_sha256,
                    "checkpoint_bytes": checkpoint_bytes,
                    "binding_sha256": binding_sha256,
                    "lineage_kind": lineage_kind,
                    "predecessor_request_id": predecessor_request_id,
                    "predecessor_conversation_url_sha256": (
                        predecessor_conversation_url_sha256
                    ),
                    "predecessor_receipt_sha256": predecessor_receipt_sha256,
                    "predecessor_state": predecessor_state,
                    "lineage_root_request_id": lineage_root_request_id,
                    "lineage_depth": lineage_depth,
                },
            )
            row = self._get_in_tx(db, request_id)
        return self._public(row, include_prompt=True)

    def authorize(
        self,
        request_id: str,
        *,
        prompt_sha256: str,
        authorization_scope: str,
        acknowledge_external_transmission: bool,
    ) -> dict[str, Any]:
        prompt_sha256 = _validate_hash(prompt_sha256, label="prompt hash")
        scope, _ = _validate_utf8_text(
            authorization_scope, label="authorization scope", max_bytes=4096
        )
        _assert_no_secret_material(scope, label="authorization scope")
        if not acknowledge_external_transmission:
            raise ValueError(
                "explicit acknowledgement of transmission to ChatGPT is required"
            )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_in_tx(db, request_id)
            self._assert_lineage_in_tx(db, row)
            if row["prompt_sha256"] != prompt_sha256:
                raise BrowserAdvisorConflict("authorized prompt hash does not match")
            if row["state"] == "authorized":
                if row["authorization_scope_sha256"] != _sha256_text(scope):
                    raise BrowserAdvisorConflict("authorization scope changed")
                return self._public(row)
            if row["state"] != "prepared":
                raise BrowserAdvisorStateError(
                    f"cannot authorize request in state {row['state']}"
                )
            now = time.time_ns()
            db.execute(
                "UPDATE advisor_requests SET state='authorized',authorization_scope=?,"
                "authorization_scope_sha256=?,authorized_ns=?,updated_ns=? "
                "WHERE request_id=? AND state='prepared'",
                (scope, _sha256_text(scope), now, now, request_id),
            )
            self._event(
                db,
                request_id,
                "authorized",
                {
                    "destination": CHATGPT_DESTINATION,
                    "prompt_sha256": prompt_sha256,
                    "authorization_scope_sha256": _sha256_text(scope),
                },
            )
            row = self._get_in_tx(db, request_id)
        return self._public(row)

    def dispatch_started(
        self,
        request_id: str,
        *,
        predecessor_conversation_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persist intent immediately before the browser action that may submit."""

        with self.project_memory_fence(self.project_dir):
            return self._dispatch_started_locked(
                request_id,
                predecessor_conversation_url=predecessor_conversation_url,
            )

    def _dispatch_started_locked(
        self,
        request_id: str,
        *,
        predecessor_conversation_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """Dispatch CAS while the supervisor project fence is held."""

        predecessor_conversation_url_sha256: Optional[str] = None
        if predecessor_conversation_url is not None:
            predecessor_conversation_url = _validate_conversation_url(
                predecessor_conversation_url
            )
            predecessor_conversation_url_sha256 = _sha256_text(
                predecessor_conversation_url
            )

        def validate_dispatch_row(db: sqlite3.Connection, row: dict[str, Any]) -> None:
            self._assert_v5_request_integrity(row)
            self._assert_lineage_in_tx(db, row)
            if row["lineage_kind"] == "local_predecessor":
                if predecessor_conversation_url_sha256 is None:
                    raise ValueError(
                        "local continuation dispatch requires the transient exact "
                        "predecessor conversation URL"
                    )
                if (
                    predecessor_conversation_url_sha256
                    != row["predecessor_conversation_url_sha256"]
                ):
                    raise BrowserAdvisorConflict(
                        "dispatch conversation URL does not match predecessor"
                    )
            elif predecessor_conversation_url_sha256 is not None:
                raise ValueError(
                    "new-chat dispatch must not supply a predecessor conversation URL"
                )

        with self._connect() as read_db:
            row = self._get_in_tx(read_db, request_id)
            validate_dispatch_row(read_db, row)
            if row["state"] == "dispatching":
                result = self._public(row)
                result.update(
                    {
                        "transitioned": False,
                        "already_dispatching": True,
                        "click_authorized": False,
                    }
                )
                return result
            if row["state"] != "authorized":
                raise BrowserAdvisorStateError(
                    f"cannot begin dispatch from state {row['state']}; "
                    "ambiguous delivery is never retried automatically"
                )
        with self._v5_dispatch_checkpoint_snapshot(row):
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = self._get_in_tx(db, request_id)
                validate_dispatch_row(db, row)
                if row["state"] != "authorized":
                    raise BrowserAdvisorStateError(
                        "advisor request changed before dispatch CAS"
                    )
                now = time.time_ns()
                pre_click_token = str(uuid.uuid4())
                db.execute(
                    "UPDATE advisor_requests SET state='dispatching',"
                    "pre_click_token_sha256=?,updated_ns=? "
                    "WHERE request_id=? AND state='authorized'",
                    (_sha256_text(pre_click_token), now, request_id),
                )
                self._event(
                    db,
                    request_id,
                    "dispatching",
                    {"browser_action_next": True},
                )
                row = self._get_in_tx(db, request_id)
        result = self._public(row)
        result.update(
            {
                "transitioned": True,
                "already_dispatching": False,
                "click_authorized": True,
                "pre_click_token": pre_click_token,
            }
        )
        return result

    def submitted(
        self,
        request_id: str,
        *,
        observed_prompt_sha256: str,
        ui_mode: str,
        full_prompt_observed: bool,
        conversation_url: str,
    ) -> dict[str, Any]:
        observed_prompt_sha256 = _validate_hash(
            observed_prompt_sha256, label="observed prompt hash"
        )
        if ui_mode != "Pro":
            raise ValueError(
                "browser advisor requires an observed UI mode of exactly 'Pro'"
            )
        if not full_prompt_observed:
            raise ValueError(
                "the full submitted prompt must be observed in the conversation"
            )
        conversation_url = _validate_conversation_url(conversation_url)
        conversation_url_sha256 = _sha256_text(conversation_url)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_in_tx(db, request_id)
            self._assert_delivery_conversation_in_tx(
                db,
                row,
                conversation_url_sha256=conversation_url_sha256,
            )
            if row["prompt_sha256"] != observed_prompt_sha256:
                raise BrowserAdvisorConflict("observed prompt hash does not match")
            if row["state"] == "submitted":
                if (
                    row["ui_mode"] != ui_mode
                    or row["conversation_url_sha256"] != conversation_url_sha256
                ):
                    raise BrowserAdvisorConflict("submitted receipt changed")
                return self._public(row)
            if row["state"] not in {"dispatching", "delivery_unknown"}:
                raise BrowserAdvisorStateError(
                    f"cannot attest submission from state {row['state']}"
                )
            now = time.time_ns()
            db.execute(
                "UPDATE advisor_requests SET state='submitted',ui_mode=?,"
                "conversation_url_sha256=?,updated_ns=? "
                "WHERE request_id=? AND state IN ('dispatching','delivery_unknown')",
                (ui_mode, conversation_url_sha256, now, request_id),
            )
            self._event(
                db,
                request_id,
                "submitted",
                {
                    "prompt_sha256": observed_prompt_sha256,
                    "ui_mode": ui_mode,
                    "conversation_url_sha256": conversation_url_sha256,
                },
            )
            row = self._get_in_tx(db, request_id)
        return self._public(row)

    @staticmethod
    def _validate_completion_attestation(
        *,
        ui_mode: str,
        stable_snapshots: int,
        completion_actions_observed: bool,
        composer_available: bool,
        working_indicator_absent: bool,
    ) -> None:
        if ui_mode != "Pro":
            raise ValueError("final response was not observed in UI mode 'Pro'")
        if isinstance(stable_snapshots, bool) or stable_snapshots < 2:
            raise ValueError("at least two stable full response snapshots are required")
        if not completion_actions_observed:
            raise ValueError("final response actions were not observed")
        if not composer_available:
            raise ValueError("composer was not available after the response")
        if not working_indicator_absent:
            raise ValueError("a working/streaming indicator was still present")

    def _finish(
        self,
        request_id: str,
        *,
        target_state: str,
        response: str,
        observed_prompt_sha256: str,
        ui_mode: str,
        conversation_url: str,
        stable_snapshots: int,
        completion_actions_observed: bool,
        composer_available: bool,
        working_indicator_absent: bool,
    ) -> dict[str, Any]:
        if target_state not in {"completed", "needs_user_input"}:
            raise AssertionError(target_state)
        response, response_bytes = _validate_utf8_text(
            response, label="advisor response", max_bytes=MAX_REPLY_BYTES
        )
        _assert_no_secret_material(response, label="advisor response")
        observed_prompt_sha256 = _validate_hash(
            observed_prompt_sha256, label="observed prompt hash"
        )
        conversation_url = _validate_conversation_url(conversation_url)
        self._validate_completion_attestation(
            ui_mode=ui_mode,
            stable_snapshots=stable_snapshots,
            completion_actions_observed=completion_actions_observed,
            composer_available=composer_available,
            working_indicator_absent=working_indicator_absent,
        )
        reply_sha256 = _sha256_text(response)
        signals = control_signals(response)
        signals_json = json.dumps(signals, separators=(",", ":"))
        conversation_url_sha256 = _sha256_text(conversation_url)
        with _project_memory_fence(self.project_dir):
            _assert_digest_absent_from_global_memory(
                self.project_dir, digest=reply_sha256
            )
            return self._record_finish_locked(
                request_id=request_id,
                target_state=target_state,
                response=response,
                response_bytes=response_bytes,
                observed_prompt_sha256=observed_prompt_sha256,
                ui_mode=ui_mode,
                conversation_url_sha256=conversation_url_sha256,
                stable_snapshots=stable_snapshots,
                signals=signals,
                signals_json=signals_json,
            )

    def _record_finish_locked(
        self,
        *,
        request_id: str,
        target_state: str,
        response: str,
        response_bytes: int,
        observed_prompt_sha256: str,
        ui_mode: str,
        conversation_url_sha256: str,
        stable_snapshots: int,
        signals: list[str],
        signals_json: str,
    ) -> dict[str, Any]:
        """Commit a completion while the supervisor project fence is held."""

        reply_sha256 = _sha256_text(response)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_in_tx(db, request_id)
            self._assert_delivery_conversation_in_tx(
                db,
                row,
                conversation_url_sha256=conversation_url_sha256,
                require_conversation_head=False,
            )
            adopted_collision = db.execute(
                "SELECT 1 FROM advisor_requests WHERE adopted_strategy_sha256=? "
                "LIMIT 1",
                (reply_sha256,),
            ).fetchone()
            if adopted_collision is not None:
                raise BrowserAdvisorConflict(
                    "browser output already exists as a durable adopted strategy"
                )
            if row["prompt_sha256"] != observed_prompt_sha256:
                raise BrowserAdvisorConflict("observed prompt hash does not match")
            if row["state"] == target_state:
                if (
                    row["reply_sha256"] != reply_sha256
                    or row["reply_bytes"] != response_bytes
                    or row["ui_mode"] != ui_mode
                    or row["conversation_url_sha256"] != conversation_url_sha256
                    or row["stable_snapshots"] != stable_snapshots
                    or row["completion_actions_observed"] != 1
                    or row["composer_available"] != 1
                    or row["working_indicator_absent"] != 1
                    or row["control_signals_json"] != signals_json
                ):
                    raise BrowserAdvisorConflict(
                        "terminal advisor completion receipt changed"
                    )
                result = self._public(row)
                if target_state == "needs_user_input":
                    # Current owner invocation only; never stored or returned by
                    # status/events after this call.
                    result["clarifying_question"] = response
                return result
            if row["state"] not in {"submitted", "delivery_unknown"}:
                raise BrowserAdvisorStateError(
                    f"cannot record {target_state} response from state {row['state']}"
                )
            # A first terminal response must still extend the current physical
            # conversation head.  Exact terminal replays above deliberately do
            # not require head status: a later prepared follow-up cannot make an
            # already durable receipt unavailable to owner/lease recovery.
            self._assert_delivery_conversation_in_tx(
                db,
                row,
                conversation_url_sha256=conversation_url_sha256,
                require_conversation_head=True,
            )
            if row["conversation_url_sha256"] not in {
                None,
                conversation_url_sha256,
            }:
                raise BrowserAdvisorConflict("conversation URL changed")
            now = time.time_ns()
            db.execute(
                "UPDATE advisor_requests SET state=?,ui_mode=?,"
                "conversation_url_sha256=?,reply_sha256=?,reply_bytes=?,"
                "stable_snapshots=?,completion_actions_observed=1,"
                "composer_available=1,working_indicator_absent=1,"
                "control_signals_json=?,updated_ns=? WHERE request_id=? AND "
                "state IN ('submitted','delivery_unknown')",
                (
                    target_state,
                    ui_mode,
                    conversation_url_sha256,
                    reply_sha256,
                    response_bytes,
                    stable_snapshots,
                    signals_json,
                    now,
                    request_id,
                ),
            )
            self._event(
                db,
                request_id,
                target_state,
                {
                    "reply_sha256": reply_sha256,
                    "reply_bytes": response_bytes,
                    "stable_snapshots": stable_snapshots,
                    "ui_mode": ui_mode,
                    "control_signals": signals,
                },
            )
            row = self._get_in_tx(db, request_id)
        result = self._public(row)
        if target_state == "needs_user_input":
            # A clarification is transient owner output, just like the raw
            # completed response supplied later to ``import_result``.
            result["clarifying_question"] = response
        return result

    def complete(self, request_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._finish(request_id, target_state="completed", **kwargs)

    def needs_input(self, request_id: str, **kwargs: Any) -> dict[str, Any]:
        """Record a stable clarifying question; it is never imported as guidance."""

        return self._finish(request_id, target_state="needs_user_input", **kwargs)

    def recover(
        self,
        request_id: str,
        *,
        observation: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if observation != "unknown":
            raise ValueError(
                "recovery only records an unknown outcome; the same request can "
                "later be reconciled as submitted/completed/needs_user_input, "
                "never resent"
            )
        if reason:
            reason, _ = _validate_utf8_text(
                reason, label="recovery reason", max_bytes=4096
            )
            _assert_no_secret_material(reason, label="recovery reason")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_in_tx(db, request_id)
            self._assert_lineage_in_tx(db, row)
            if row["state"] == "delivery_unknown":
                return self._public(row)
            if row["state"] not in {"dispatching", "submitted"}:
                raise BrowserAdvisorStateError(
                    f"cannot mark delivery unknown from state {row['state']}"
                )
            state = "delivery_unknown"
            detail = {"reason_sha256": _sha256_text(reason) if reason else None}
            now = time.time_ns()
            db.execute(
                "UPDATE advisor_requests SET state=?,updated_ns=? WHERE request_id=?",
                (state, now, request_id),
            )
            self._event(db, request_id, state, detail)
            row = self._get_in_tx(db, request_id)
        return self._public(row)

    def fail_not_submitted(
        self,
        request_id: str,
        *,
        reason: str,
        before_click_evidence: str,
        acknowledge_no_submit_action: bool,
        pre_click_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Terminally close a dispatch that authoritatively failed before click.

        This is intentionally available only from ``authorized`` (dispatch CAS
        failed before state change) or a newly transitioned ``dispatching``
        receipt while the UI owner still knows that no click/key action capable
        of submission has occurred.  It is never a recovery escape from a
        replayed ``dispatching`` receipt or ``delivery_unknown``.
        """

        reason, _ = _validate_utf8_text(
            reason, label="pre-submit failure reason", max_bytes=4096
        )
        evidence, _ = _validate_utf8_text(
            before_click_evidence,
            label="before-click evidence",
            max_bytes=4096,
        )
        _assert_no_secret_material(reason, label="pre-submit failure reason")
        _assert_no_secret_material(evidence, label="before-click evidence")
        if not acknowledge_no_submit_action:
            raise ValueError(
                "failed-not-submitted requires explicit acknowledgement that no "
                "browser action capable of submission occurred"
            )
        reason_sha256 = _sha256_text(reason)
        evidence_sha256 = _sha256_text(evidence)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_in_tx(db, request_id)
            self._assert_lineage_in_tx(db, row)
            if row["state"] == "failed_not_submitted":
                if (
                    row["terminal_reason_sha256"] != reason_sha256
                    or row["terminal_evidence_sha256"] != evidence_sha256
                    or row["terminal_acknowledgement"] != 1
                ):
                    raise BrowserAdvisorConflict(
                        "failed-not-submitted terminal receipt changed"
                    )
                if row["terminal_prior_state"] == "dispatching":
                    token = _safe_optional_id(pre_click_token, label="pre-click token")
                    if (
                        token is None
                        or _sha256_text(token) != row["pre_click_token_sha256"]
                    ):
                        raise BrowserAdvisorConflict(
                            "failed-not-submitted replay changed its pre-click binding"
                        )
                elif pre_click_token is not None:
                    raise BrowserAdvisorConflict(
                        "failed-not-submitted replay added a pre-click binding"
                    )
                return self._public(row)
            if row["state"] not in {"authorized", "dispatching"}:
                raise BrowserAdvisorStateError(
                    "failed-not-submitted is valid only from authorized or a fresh "
                    "dispatching transition before "
                    "any submit-capable browser action; unknown/submitted outcomes "
                    "must be reconciled, never downgraded"
                )
            if row["state"] == "authorized" and pre_click_token is not None:
                raise ValueError(
                    "authorized pre-submit failure must not supply a pre-click token"
                )
            if row["state"] == "dispatching":
                token = _safe_optional_id(pre_click_token, label="pre-click token")
                if (
                    token is None
                    or _sha256_text(token) != row["pre_click_token_sha256"]
                ):
                    raise BrowserAdvisorStateError(
                        "dispatching failure requires the one-time pre-click token "
                        "returned only by the fresh dispatch transition; a replay "
                        "must be treated as unknown/recover-only"
                    )
            prior = row["state"]
            now = time.time_ns()
            db.execute(
                "UPDATE advisor_requests SET state='failed_not_submitted',"
                "terminal_reason_sha256=?,terminal_evidence_sha256=?,"
                "terminal_acknowledgement=1,terminal_prior_state=?,updated_ns=? "
                "WHERE request_id=? AND state IN ('authorized','dispatching')",
                (reason_sha256, evidence_sha256, prior, now, request_id),
            )
            self._event(
                db,
                request_id,
                "failed_not_submitted",
                {
                    "reason_sha256": reason_sha256,
                    "before_click_evidence_sha256": evidence_sha256,
                    "no_submit_action_acknowledged": True,
                },
            )
            row = self._get_in_tx(db, request_id)
        return self._public(row)

    def abandon(
        self,
        request_id: str,
        *,
        reason: str,
        acknowledge_delivery_unknown: bool = False,
    ) -> dict[str, Any]:
        reason, _ = _validate_utf8_text(reason, label="abandon reason", max_bytes=4096)
        _assert_no_secret_material(reason, label="abandon reason")
        reason_sha256 = _sha256_text(reason)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_in_tx(db, request_id)
            self._assert_lineage_in_tx(db, row, require_conversation_head=False)
            if row["state"] in {
                "abandoned",
                "owner_abandoned_outcome_unknown",
            }:
                if row["terminal_reason_sha256"] != reason_sha256 or row[
                    "terminal_acknowledgement"
                ] != int(acknowledge_delivery_unknown):
                    raise BrowserAdvisorConflict("abandon terminal receipt changed")
                return self._public(row)
            if row["state"] == "completed" or row["state"] in _TERMINAL_STATES:
                raise BrowserAdvisorStateError(
                    f"terminal advisor receipt {row['state']} must be preserved "
                    "and cannot be abandoned"
                )
            if row["state"] in _AMBIGUOUS_STATES and not acknowledge_delivery_unknown:
                raise ValueError(
                    "abandoning a possibly submitted request requires explicit "
                    "acknowledgement that delivery/outcome may be unknown"
                )
            prior = row["state"]
            terminal_state = (
                "owner_abandoned_outcome_unknown"
                if prior in _AMBIGUOUS_STATES
                else "abandoned"
            )
            now = time.time_ns()
            db.execute(
                "UPDATE advisor_requests SET state=?,terminal_reason_sha256=?,"
                "terminal_acknowledgement=?,terminal_prior_state=?,updated_ns=? "
                "WHERE request_id=?",
                (
                    terminal_state,
                    reason_sha256,
                    int(acknowledge_delivery_unknown),
                    prior,
                    now,
                    request_id,
                ),
            )
            self._event(
                db,
                request_id,
                terminal_state,
                {
                    "prior_state": prior,
                    "reason_sha256": reason_sha256,
                    "acknowledged_delivery_unknown": acknowledge_delivery_unknown,
                },
            )
            row = self._get_in_tx(db, request_id)
        return self._public(row)

    def import_result(self, request_id: str, *, response: str) -> dict[str, Any]:
        """Return a standard envelope and idempotently ledger it.

        Import is only a strategy receipt.  This method intentionally has no
        dependency on the MCP gateway or any truth/publication path.  The owner
        must resupply the exact response bytes from the ChatGPT conversation;
        those bytes are returned transiently and are never persisted here.
        """

        response, response_bytes = _validate_utf8_text(
            response, label="advisor response", max_bytes=MAX_REPLY_BYTES
        )
        _assert_no_secret_material(response, label="advisor response")
        response_sha256 = _sha256_text(response)
        with self._connect() as db:
            row = self._get_in_tx(db, request_id)
            self._assert_lineage_in_tx(db, row, require_conversation_head=False)
        if row["state"] not in {"completed", "imported", "adopted"}:
            raise BrowserAdvisorStateError(
                f"only a completed response can be imported, not {row['state']}"
            )
        if (
            row["reply_sha256"] != response_sha256
            or row["reply_bytes"] != response_bytes
            or row["control_signals_json"]
            != json.dumps(control_signals(response), separators=(",", ":"))
        ):
            raise BrowserAdvisorConflict(
                "resupplied advisor response does not match the completed receipt"
            )
        ledger_envelope = self._envelope(row, response=response)
        from .ledger import log_spend_summary

        summary = log_spend_summary(self.project_dir, ledger_envelope)
        if row["state"] == "completed":
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self._get_in_tx(db, request_id)
                self._assert_lineage_in_tx(db, current, require_conversation_head=False)
                if current["state"] == "completed":
                    now = time.time_ns()
                    db.execute(
                        "UPDATE advisor_requests SET state='imported',updated_ns=? "
                        "WHERE request_id=? AND state='completed'",
                        (now, request_id),
                    )
                    self._event(
                        db,
                        request_id,
                        "imported",
                        {"reply_sha256": current["reply_sha256"]},
                    )
                elif current["state"] != "imported":
                    raise BrowserAdvisorStateError(
                        f"request changed to {current['state']} during import"
                    )
        with self._connect() as db:
            row = self._get_in_tx(db, request_id)
        envelope = self._envelope(row, response=response)
        envelope.update(summary)
        return envelope

    def adopt(
        self,
        request_id: str,
        *,
        strategy: str,
        acknowledge_untrusted_review: bool,
    ) -> dict[str, Any]:
        """Record explicit review/synthesis before master-guidance use.

        Import only exposes an untrusted browser report.  The main agent or
        owner must review it and provide strategy-only text here.  Privileged
        tool/control instructions are rejected at this seam.
        """

        strategy, strategy_bytes = _validate_utf8_text(
            strategy, label="adopted strategy", max_bytes=MAX_REPLY_BYTES
        )
        _assert_no_secret_material(strategy, label="adopted strategy")
        signals = control_signals(strategy)
        if signals:
            raise ValueError(
                "adopted strategy still contains privileged control signals "
                f"({','.join(signals)}); synthesize mathematical strategy only"
            )
        if not acknowledge_untrusted_review:
            raise ValueError(
                "adoption requires explicit acknowledgement that the browser "
                "report was reviewed as untrusted content"
            )
        strategy_sha256 = _sha256_text(strategy)
        with _project_memory_fence(self.project_dir):
            return self._adopt_locked(
                request_id=request_id,
                strategy=strategy,
                strategy_bytes=strategy_bytes,
                strategy_sha256=strategy_sha256,
            )

    def _adopt_locked(
        self,
        *,
        request_id: str,
        strategy: str,
        strategy_bytes: int,
        strategy_sha256: str,
    ) -> dict[str, Any]:
        """Persist reviewed synthesis while raw-output registration is fenced."""

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_in_tx(db, request_id)
            self._assert_lineage_in_tx(db, row, require_conversation_head=False)
            raw_collision = db.execute(
                "SELECT 1 FROM advisor_requests WHERE reply_sha256=? LIMIT 1",
                (strategy_sha256,),
            ).fetchone()
            if raw_collision is not None:
                raise ValueError(
                    "adopted strategy must be an explicit reviewed synthesis, "
                    "not the raw browser response from any advisor receipt"
                )
            if row["state"] == "adopted":
                if row["adopted_strategy_sha256"] != strategy_sha256:
                    raise BrowserAdvisorConflict("adopted strategy changed")
                return self._adoption_envelope(row)
            if row["state"] != "imported":
                raise BrowserAdvisorStateError(
                    f"only an imported report can be adopted, not {row['state']}"
                )
            now = time.time_ns()
            db.execute(
                "UPDATE advisor_requests SET state='adopted',adopted_strategy=?,"
                "adopted_strategy_sha256=?,adopted_ns=?,updated_ns=? "
                "WHERE request_id=? AND state='imported'",
                (strategy, strategy_sha256, now, now, request_id),
            )
            self._event(
                db,
                request_id,
                "adopted",
                {
                    "adopted_strategy_sha256": strategy_sha256,
                    "adopted_strategy_bytes": strategy_bytes,
                    "advisor_reply_sha256": row["reply_sha256"],
                    "reviewed_as_untrusted": True,
                },
            )
            row = self._get_in_tx(db, request_id)
        return self._adoption_envelope(row)

    def _envelope(self, row: dict[str, Any], *, response: str) -> dict[str, Any]:
        signals = json.loads(row["control_signals_json"] or "[]")
        elapsed = max(0.0, (row["updated_ns"] - row["created_ns"]) / 1_000_000_000)
        return {
            "transport": CHATGPT_BROWSER_TRANSPORT,
            "model": None,
            "ui_mode": "Pro",
            "effort": None,
            "attempt": "owner-mediated-browser-ui",
            "status": "completed",
            "receipt_state": row["state"],
            "receipt_sha256": self._receipt_sha256(row),
            "seconds": round(elapsed, 1),
            "usage": None,
            "billing_basis": "subscription",
            "cost_usd": None,
            "tool_calls": [],
            "reasoning_summary": "",
            # Ephemeral owner output.  ``response`` is exact-hash checked by
            # ``import_result`` and never read from or written to the ledger DB.
            "reply": response,
            "request_id": row["request_id"],
            "recommendation_id": row["recommendation_id"],
            "context_sha256": _sha256_text(row["context_id"]),
            "prompt_sha256": row["prompt_sha256"],
            "reply_sha256": row["reply_sha256"],
            "conversation_url_sha256": row["conversation_url_sha256"],
            "lineage": self._lineage_public(row),
            "trust": ADVISOR_TRUST,
            "authorities": list(ADVISOR_AUTHORITIES),
            "control_signals": signals,
            "eligible_for_master_guidance": False,
            "adopted": False,
        }

    def _adoption_envelope(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["state"] != "adopted":
            raise BrowserAdvisorStateError("advisor report has not been adopted")
        receipt_schema_version = int(row.get("receipt_schema_version") or 2)
        provenance = {
            "schema_version": 2 if receipt_schema_version >= 5 else 1,
            "transport": CHATGPT_BROWSER_TRANSPORT,
            "request_id": row["request_id"],
            "elaboration_id": row["elaboration_id"],
            "context_id": row["context_id"],
            "recommendation_id": row["recommendation_id"],
            "binding_sha256": row["binding_sha256"],
            "receipt_sha256": self._receipt_sha256(row),
            "prompt_sha256": row["prompt_sha256"],
            "reply_sha256": row["reply_sha256"],
            "adopted_strategy_sha256": row["adopted_strategy_sha256"],
            "trust": "adopted_strategy",
            "billing_basis": "subscription",
            "model": None,
            "ui_mode": "Pro",
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        }
        if receipt_schema_version >= 5:
            provenance.update(
                {
                    "checkpoint_id": row["checkpoint_id"],
                    "checkpoint_sha256": row["checkpoint_sha256"],
                    "checkpoint_bytes": row["checkpoint_bytes"],
                }
            )
        return {
            "transport": CHATGPT_BROWSER_TRANSPORT,
            "model": None,
            "ui_mode": "Pro",
            "effort": None,
            "status": "adopted",
            "receipt_state": "adopted",
            "receipt_sha256": self._receipt_sha256(row),
            "billing_basis": "subscription",
            "usage": None,
            "cost_usd": None,
            "reply": row["adopted_strategy"],
            "request_id": row["request_id"],
            "recommendation_id": row["recommendation_id"],
            "checkpoint_id": row["checkpoint_id"],
            "checkpoint_sha256": row["checkpoint_sha256"],
            "checkpoint_bytes": row["checkpoint_bytes"],
            "context_sha256": _sha256_text(row["context_id"]),
            "advisor_reply_sha256": row["reply_sha256"],
            "adopted_strategy_sha256": row["adopted_strategy_sha256"],
            "lineage": self._lineage_public(row),
            "trust": "adopted_strategy",
            "authorities": list(ADVISOR_AUTHORITIES),
            "eligible_for_master_guidance": True,
            "adopted": True,
            "consult_provenance": provenance,
        }

    @classmethod
    def recommendation_request(
        cls, project_dir: Path | str, *, recommendation_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the one exact recommendation-bound request without writes.

        This lookup is intentionally content-free so owner-resolution code can
        fence paid-generation resume without importing prompts or page text.
        """

        recommendation_id = (
            _safe_optional_id(recommendation_id, label="recommendation id") or ""
        )
        with _read_existing_database(project_dir, required=False) as db:
            if db is None:
                return None
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(advisor_requests)")
            }
            if "recommendation_id" not in columns:
                unsafe = db.execute(
                    "SELECT 1 FROM advisor_requests WHERE state NOT IN "
                    "('imported','adopted','failed_not_submitted','abandoned',"
                    "'owner_abandoned_outcome_unknown','needs_user_input') LIMIT 1"
                ).fetchone()
                if unsafe is not None:
                    raise BrowserAdvisorStateError(
                        "pre-binding browser requests must be closed before "
                        "recommendation resolution"
                    )
                return None
            rows = db.execute(
                "SELECT * FROM advisor_requests WHERE recommendation_id=? "
                "ORDER BY created_ns LIMIT 2",
                (recommendation_id,),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise BrowserAdvisorConflict(
                "recommendation has multiple browser-advisor requests"
            )
        row = cls._row(rows[0])
        if row.get("recommendation_id") != recommendation_id:
            raise BrowserAdvisorConflict(
                "browser-advisor recommendation lookup changed identity"
            )
        return {
            "recommendation_id": recommendation_id,
            "request_id": row["request_id"],
            "state": row["state"],
            "receipt_sha256": cls._receipt_sha256(row),
            "release_safe": row["state"] in _RECOMMENDATION_RELEASE_SAFE_STATES,
        }

    @classmethod
    def assert_recommendation_releasable(
        cls, project_dir: Path | str, *, recommendation_id: str
    ) -> Optional[dict[str, Any]]:
        """Fail closed if browser work could outlive owner paid-work resume."""

        request = cls.recommendation_request(
            project_dir, recommendation_id=recommendation_id
        )
        if request is not None and request["release_safe"] is not True:
            raise BrowserAdvisorStateError(
                "recommendation browser request must reach an explicit release-safe "
                f"state before paid reasoning resumes (state={request['state']})"
            )
        return request

    @classmethod
    def validate_adopted_master_guidance(
        cls,
        project_dir: Path | str,
        *,
        provenance: dict[str, Any],
        evidence: str,
    ) -> None:
        """Bind privileged guidance to one adopted same-project broker row.

        This is a read-only fail-closed check for the gateway.  It does not
        create ``.advisor`` or a database when the receipt is missing, and it
        authenticates the evidence as the exact reviewed synthesis -- never the
        raw browser response.
        """

        evidence, _ = _validate_utf8_text(
            evidence, label="master guidance evidence", max_bytes=MAX_REPLY_BYTES
        )
        request_id = provenance.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise BrowserAdvisorConflict("browser provenance has no request id")
        with _read_existing_database(project_dir, required=True) as db:
            assert db is not None
            stored = db.execute(
                "SELECT * FROM advisor_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        if stored is None:
            raise BrowserAdvisorConflict(
                "browser provenance request is missing from this project"
            )
        row = cls._row(stored)
        if row["state"] != "adopted":
            raise BrowserAdvisorStateError(
                "browser-backed master guidance requires an adopted receipt"
            )
        strategy = row["adopted_strategy"]
        if (
            not isinstance(strategy, str)
            or _sha256_text(strategy) != row["adopted_strategy_sha256"]
        ):
            raise BrowserAdvisorConflict(
                "adopted strategy storage failed integrity check"
            )
        receipt_schema_version = int(row.get("receipt_schema_version") or 2)
        expected = {
            "schema_version": 2 if receipt_schema_version >= 5 else 1,
            "transport": CHATGPT_BROWSER_TRANSPORT,
            "request_id": row["request_id"],
            "elaboration_id": row["elaboration_id"],
            "context_id": row["context_id"],
            "recommendation_id": row["recommendation_id"],
            "binding_sha256": row["binding_sha256"],
            "receipt_sha256": cls._receipt_sha256(row),
            "prompt_sha256": row["prompt_sha256"],
            "reply_sha256": row["reply_sha256"],
            "adopted_strategy_sha256": row["adopted_strategy_sha256"],
            "trust": "adopted_strategy",
            "billing_basis": "subscription",
            "model": None,
            "ui_mode": "Pro",
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        }
        if receipt_schema_version >= 5:
            expected.update(
                {
                    "checkpoint_id": row["checkpoint_id"],
                    "checkpoint_sha256": row["checkpoint_sha256"],
                    "checkpoint_bytes": row["checkpoint_bytes"],
                }
            )
        if provenance != expected:
            raise BrowserAdvisorConflict(
                "browser provenance does not exactly match the adopted project receipt"
            )
        if _sha256_text(evidence) != row["adopted_strategy_sha256"]:
            raise BrowserAdvisorConflict(
                "master guidance evidence is not the adopted strategy synthesis"
            )

    @classmethod
    @contextmanager
    def project_memory_fence(cls, project_dir: Path | str) -> Iterator[None]:
        """Hold the supervisor-owned browser-output/global-memory fence."""

        with _project_memory_fence(project_dir):
            yield

    @classmethod
    def reject_raw_project_text_locked(
        cls,
        project_dir: Path | str,
        *,
        fields: dict[str, object],
    ) -> None:
        """Reject exact raw browser output while the project fence is held.

        Only digests are compared; the broker never rematerializes page text.
        Semantic paraphrase/review remains a trusted-main judgment, while the
        browser-backed authoritative path is separately bound by
        :meth:`validate_adopted_master_guidance`.
        """

        _, identity = _canonical_project_identity(project_dir)
        if identity not in getattr(_FENCE_LOCAL, "project_identities", set()):
            raise BrowserAdvisorError(
                "raw browser-output check requires the supervisor project fence"
            )
        digests = _durable_string_digests(fields)
        with _read_existing_database(project_dir, required=False) as db:
            if db is None:
                return
            stored_digests = {
                str(row["reply_sha256"])
                for row in db.execute(
                    "SELECT DISTINCT reply_sha256 FROM advisor_requests "
                    "WHERE reply_sha256 IS NOT NULL"
                )
            }
        matched_digests = stored_digests.intersection(digest for _, digest in digests)
        if not matched_digests:
            return
        labels = sorted(label for label, digest in digests if digest in matched_digests)
        raise BrowserAdvisorConflict(
            "global-memory field exactly matches untrusted raw browser output "
            f"({','.join(labels)}); review and synthesize before durable storage"
        )

    @classmethod
    def reject_raw_project_text(
        cls,
        project_dir: Path | str,
        *,
        fields: dict[str, object],
    ) -> None:
        """Fence and reject raw browser output in durable project text."""

        with cls.project_memory_fence(project_dir):
            cls.reject_raw_project_text_locked(project_dir, fields=fields)

    def events(self, request_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            self._get_in_tx(db, request_id)
            rows = db.execute(
                "SELECT seq,state,detail_json,created_ns FROM advisor_events "
                "WHERE request_id=? ORDER BY seq",
                (request_id,),
            ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "state": row["state"],
                "detail": json.loads(row["detail_json"]),
                "created_ns": int(row["created_ns"]),
            }
            for row in rows
        ]

    def get(self, request_id: str, *, include_prompt: bool = False) -> dict[str, Any]:
        with self._connect() as db:
            row = self._get_in_tx(db, request_id)
            self._assert_lineage_in_tx(db, row, require_conversation_head=False)
        return self._public(row, include_prompt=include_prompt)

    @staticmethod
    def _lineage_public(row: dict[str, Any]) -> dict[str, Any]:
        kind = str(row["lineage_kind"])
        return {
            "kind": kind,
            "predecessor_request_id": row["predecessor_request_id"],
            "predecessor_receipt_sha256": row["predecessor_receipt_sha256"],
            "predecessor_state": row["predecessor_state"],
            "conversation_url_sha256": row["predecessor_conversation_url_sha256"],
            "lineage_root_request_id": row["lineage_root_request_id"],
            "lineage_depth": row["lineage_depth"],
            "locally_verified": True,
            "grants_authority": False,
        }

    @staticmethod
    def _public(row: dict[str, Any], *, include_prompt: bool = False) -> dict[str, Any]:
        state = str(row["state"])
        result: dict[str, Any] = {
            "schema_version": 1,
            "transport": CHATGPT_BROWSER_TRANSPORT,
            "request_id": row["request_id"],
            "state": state,
            "prompt_sha256": row["prompt_sha256"],
            "prompt_bytes": row["prompt_bytes"],
            "elaboration_id": row["elaboration_id"],
            "context_id": row["context_id"],
            "recommendation_id": row["recommendation_id"],
            "checkpoint_id": row["checkpoint_id"],
            "checkpoint_sha256": row["checkpoint_sha256"],
            "checkpoint_bytes": row["checkpoint_bytes"],
            "context_sha256": _sha256_text(row["context_id"]),
            "binding_sha256": row["binding_sha256"],
            "destination": CHATGPT_DESTINATION,
            "billing_basis": "subscription",
            "model": None,
            "usage": None,
            "cost_usd": None,
            "trust": ADVISOR_TRUST,
            "authorities": list(ADVISOR_AUTHORITIES),
            "recovery_required": state in _AMBIGUOUS_STATES,
            "automatic_redispatch_allowed": False,
            "click_authorized": False,
            "receipt_sha256": BrowserAdvisorBroker._receipt_sha256(row),
            "receipt_schema_version": row["receipt_schema_version"],
            "eligible_for_master_guidance": state == "adopted",
            "adopted": state == "adopted",
            "reply_sha256": row["reply_sha256"],
            "adopted_strategy_sha256": row["adopted_strategy_sha256"],
            "conversation_url_sha256": row["conversation_url_sha256"],
            "lineage": BrowserAdvisorBroker._lineage_public(row),
        }
        if include_prompt:
            result["prompt"] = row["prompt"]
        return result


__all__ = [
    "ADVISOR_AUTHORITIES",
    "ADVISOR_TRUST",
    "BrowserAdvisorBroker",
    "BrowserAdvisorConflict",
    "BrowserAdvisorError",
    "BrowserAdvisorStateError",
    "CHATGPT_BROWSER_TRANSPORT",
    "CHATGPT_DESTINATION",
    "control_signals",
    "secret_markers",
]
