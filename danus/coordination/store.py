"""Crash-durable SQLite admission for reasoning-first paid worker rounds.

The database stores orchestration identities and lifecycle state, plus the
bounded task and generated kickoff snapshots required to make every paid turn
generation-exact and an ambiguous app-server retry byte-identical.  It never
stores mathematical claims, advisor prose, or model output.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .policy import (
    CRITIC_REVIEW_PHASE,
    OWNER_ACTION_REQUIRED_PHASE,
    REASONING_PHASE,
    REASONING_FIRST_MODE,
    CoordinationConfig,
    CoordinationConfigError,
    LaneRoster,
    coordination_config,
    coordination_directive,
    candidate_outcome_releases,
    required_lanes,
    roster_digest,
    select_lane_roster,
)

TWO_LANE_SCHEMA_VERSION = 7
SCHEMA_VERSION = 8
MAX_PROJECT_METADATA_BYTES = 1_000_000
MAX_PINNED_PROMPT_BYTES = 131_072
MAX_TASK_BYTES = 131_072
MAX_OUTCOME_BYTES = 512
MAX_RECONCILE_ENTRIES = 10_000
PREPARED_DEADLINE_OUTCOME = "phase_deadline_known_not_dispatched"
RECOMMENDATION_RESOLUTIONS = frozenset(
    {"adopted_master_guidance", "continue_without_advisor"}
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HEX16_RE = re.compile(r"[0-9a-f]{16}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CoordinationError(RuntimeError):
    """The durable coordinator cannot safely make the requested transition."""


@dataclass(frozen=True)
class Admission:
    slot_id: str
    worker: str
    lane: str
    generation: int
    phase: str
    directive: str
    task: str
    task_sha256: str
    task_bytes: int
    prompt: str | None
    prompt_sha256: str | None
    state: str
    resumed: bool
    phase_deadline_at: float
    hard_timeout_seconds: int
    review_id: str | None
    designated_root_entry_id: str | None


def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise CoordinationError(f"{label} is not a bounded identifier")
    return value


def candidate_receipt_id(
    *,
    slot_id: str,
    candidate_fact_id: str,
    candidate_fact_identity: str,
    source_id: str | None,
    context_digest: str,
) -> str:
    """Derive the sole canonical content-free candidate receipt identity."""

    slot_id = _validate_identifier(slot_id, "slot_id")
    if (
        not isinstance(candidate_fact_id, str)
        or _HEX16_RE.fullmatch(candidate_fact_id) is None
    ):
        raise CoordinationError("candidate_fact_id must be 16 lowercase hex")
    if source_id is not None and (
        not isinstance(source_id, str) or _HEX16_RE.fullmatch(source_id) is None
    ):
        raise CoordinationError("source_id must be null or 16 lowercase hex")
    if (
        not isinstance(candidate_fact_identity, str)
        or _SHA256_RE.fullmatch(candidate_fact_identity) is None
    ):
        raise CoordinationError("candidate_fact_identity must be 64 lowercase hex")
    if (
        not isinstance(context_digest, str)
        or _SHA256_RE.fullmatch(context_digest) is None
    ):
        raise CoordinationError("context_digest must be 64 lowercase hex")
    material = {
        "schema": "danus_reasoning_candidate_receipt_v2",
        "slot_id": slot_id,
        "candidate_fact_id": candidate_fact_id,
        "candidate_fact_identity": candidate_fact_identity,
        "source_id": source_id,
        "context_digest": context_digest,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def obstacle_review_id(
    *,
    generation: int,
    root_entry_id: str,
    root_slot_id: str,
    critic_worker: str,
) -> str:
    """Derive one replay-stable content-free review identity."""

    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise CoordinationError("review generation must be positive")
    material = {
        "schema": "danus_obstacle_review_v1",
        "generation": generation,
        "root_entry_id": _validate_identifier(root_entry_id, "root_entry_id"),
        "root_slot_id": _validate_identifier(root_slot_id, "root_slot_id"),
        "critic_worker": _validate_identifier(critic_worker, "critic_worker"),
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return f"review_{hashlib.sha256(encoded).hexdigest()}"


def recommendation_resolution_id(
    *,
    recommendation_id: str,
    resolution: str,
    owner_acknowledgement: str,
    master_guidance_entry_id: str | None,
    master_guidance_record_sha256: str | None,
    browser_request_id: str | None,
    browser_receipt_sha256: str | None,
) -> str:
    """Derive a replay-stable, content-free owner-resolution identity."""

    recommendation_id = _validate_identifier(recommendation_id, "recommendation_id")
    if resolution not in RECOMMENDATION_RESOLUTIONS:
        raise CoordinationError("recommendation resolution is unsupported")
    if owner_acknowledgement != recommendation_id:
        raise CoordinationError(
            "owner acknowledgement must exactly equal the recommendation id"
        )
    if master_guidance_entry_id is not None and (
        not isinstance(master_guidance_entry_id, str)
        or _HEX16_RE.fullmatch(master_guidance_entry_id) is None
    ):
        raise CoordinationError("master guidance entry id must be 16 lowercase hex")
    for value, label in (
        (master_guidance_record_sha256, "master guidance record digest"),
        (browser_receipt_sha256, "browser receipt digest"),
    ):
        if value is not None and (
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        ):
            raise CoordinationError(f"{label} must be 64 lowercase hex")
    if browser_request_id is not None:
        browser_request_id = _validate_identifier(
            browser_request_id, "browser_request_id"
        )
    if resolution == "continue_without_advisor":
        if any(
            value is not None
            for value in (
                master_guidance_entry_id,
                master_guidance_record_sha256,
                browser_request_id,
                browser_receipt_sha256,
            )
        ):
            raise CoordinationError(
                "continue-without-advisor cannot bind master guidance or browser receipt"
            )
    elif (
        master_guidance_entry_id is None
        or master_guidance_record_sha256 is None
        or (browser_request_id is None) != (browser_receipt_sha256 is None)
    ):
        raise CoordinationError(
            "adopted master guidance requires exact entry/digest and complete browser receipt"
        )
    material = {
        "schema": "danus_recommendation_resolution_v1",
        "recommendation_id": recommendation_id,
        "resolution": resolution,
        "owner_acknowledgement": owner_acknowledgement,
        "master_guidance_entry_id": master_guidance_entry_id,
        "master_guidance_record_sha256": master_guidance_record_sha256,
        "browser_request_id": browser_request_id,
        "browser_receipt_sha256": browser_receipt_sha256,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return f"resolution_{hashlib.sha256(encoded).hexdigest()}"


def load_project_metadata(project_dir: Path) -> dict[str, Any]:
    """Read project.json without following a planted link or oversized file."""

    project_dir = Path(project_dir)
    path = project_dir / "project.json"
    coordination_path = project_dir / ".coordination"
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if os.path.lexists(coordination_path):
            raise CoordinationError(
                "project metadata disappeared while coordination state exists"
            ) from None
        return {}
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_PROJECT_METADATA_BYTES
        ):
            raise CoordinationError("project metadata is not a safe regular file")
        payload = os.read(descriptor, MAX_PROJECT_METADATA_BYTES + 1)
        if len(payload) > MAX_PROJECT_METADATA_BYTES:
            raise CoordinationError("project metadata exceeds its hard limit")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoordinationError("project metadata is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise CoordinationError("project metadata must be an object")
    raw_coordination = decoded.get("coordination")
    if os.path.lexists(coordination_path) and (
        not isinstance(raw_coordination, dict)
        or raw_coordination.get("mode") != REASONING_FIRST_MODE
    ):
        raise CoordinationError(
            "project metadata cannot downgrade existing coordination state"
        )
    return decoded


class CoordinationStore:
    """Project-wide CAS store for protected reasoning-first paid lanes."""

    def __init__(
        self,
        project_dir: Path,
        metadata: Mapping[str, Any] | None = None,
        *,
        create: bool = True,
    ) -> None:
        self.project_dir = Path(os.path.abspath(os.fspath(project_dir)))
        self._validate_project_directory()
        self.metadata = dict(
            load_project_metadata(self.project_dir) if metadata is None else metadata
        )
        try:
            self.config: CoordinationConfig = coordination_config(self.metadata)
            if not self.config.reasoning_first:
                raise CoordinationError(
                    "a legacy project has no reasoning-first coordination store"
                )
            self.roster: LaneRoster = select_lane_roster(self.metadata, self.config)
        except CoordinationConfigError as exc:
            raise CoordinationError(str(exc)) from exc
        self._database_schema_version = (
            SCHEMA_VERSION
            if self.config.max_paid_workers > 2
            else TWO_LANE_SCHEMA_VERSION
        )
        self._lane_sql = (
            "'root', 'critic', 'explorer1', 'explorer2'"
            if self._database_schema_version == SCHEMA_VERSION
            else "'root', 'critic'"
        )
        self._roster_digest = roster_digest(self.metadata, self.config, self.roster)
        self.directory = self.project_dir / ".coordination"
        self.path = self.directory / "state.sqlite3"
        self._ensure_directory(create=create)
        if not create:
            try:
                os.lstat(self.path)
            except FileNotFoundError:
                raise FileNotFoundError(self.path) from None
        self._initialize(create=create)

    @classmethod
    def open_existing(
        cls, project_dir: Path, metadata: Mapping[str, Any] | None = None
    ) -> CoordinationStore | None:
        try:
            return cls(project_dir, metadata, create=False)
        except FileNotFoundError:
            return None

    def _validate_project_directory(self) -> None:
        try:
            info = os.lstat(self.project_dir)
        except FileNotFoundError as exc:
            raise CoordinationError("project directory does not exist") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CoordinationError("project directory is unsafe")

    def _ensure_directory(self, *, create: bool) -> None:
        created = False
        if create:
            try:
                os.mkdir(self.directory, 0o700)
                created = True
            except FileExistsError:
                pass
        try:
            info = os.lstat(self.directory)
        except FileNotFoundError:
            if not create:
                raise FileNotFoundError(self.directory) from None
            raise
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise CoordinationError("coordination directory is unsafe")
        if create:
            os.chmod(self.directory, 0o700)
        if created:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(self.project_dir, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _verify_database(self) -> tuple[int, int]:
        info = os.lstat(self.path)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
        ):
            raise CoordinationError("coordination database is unsafe")
        return info.st_dev, info.st_ino

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _create_database_file(self) -> tuple[int, int]:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            return self._verify_database()
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise CoordinationError("new coordination database is unsafe")
            os.fsync(descriptor)
            identity = (info.st_dev, info.st_ino)
        finally:
            os.close(descriptor)
        self._fsync_directory()
        return identity

    def _connect(self, *, allow_create: bool = False) -> sqlite3.Connection:
        try:
            before = self._verify_database()
        except FileNotFoundError:
            if not allow_create:
                raise CoordinationError("coordination database disappeared") from None
            before = self._create_database_file()
        connection = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
        )
        try:
            after = self._verify_database()
            if after != before:
                raise CoordinationError("coordination database changed during open")
            os.chmod(self.path, 0o600)
            if self._verify_database() != before:
                raise CoordinationError(
                    "coordination database changed during validation"
                )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=WAL")
            self._fsync_directory()
            return connection
        except BaseException:
            connection.close()
            raise

    def _create_generation_tasks_table_locked(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE generation_tasks (
                worker TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                lane TEXT NOT NULL CHECK (lane IN ({self._lane_sql})),
                task TEXT NOT NULL,
                task_sha256 TEXT NOT NULL,
                task_bytes INTEGER NOT NULL CHECK (
                    task_bytes > 0 AND task_bytes <= 131072
                ),
                staged_at REAL NOT NULL,
                frozen_at REAL,
                PRIMARY KEY (worker, generation)
            )
            """
        )

    def _initialize(self, *, create: bool) -> None:
        if not create:
            self._verify_database()
        connection = self._connect(allow_create=create)
        try:
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS project_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    config_digest TEXT NOT NULL,
                    root_worker TEXT NOT NULL,
                    critic_worker TEXT,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    phase TEXT NOT NULL,
                    phase_started_at REAL NOT NULL,
                    phase_deadline_at REAL NOT NULL,
                    recommendation_id TEXT,
                    active_review_id TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS round_slots (
                    slot_id TEXT PRIMARY KEY,
                    worker TEXT NOT NULL,
                    lane TEXT NOT NULL CHECK (lane IN ({self._lane_sql})),
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    phase TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('prepared', 'active', 'ambiguous', 'terminal')
                    ),
                    directive TEXT NOT NULL,
                    task TEXT,
                    task_sha256 TEXT,
                    task_bytes INTEGER,
                    prompt_task_sha256 TEXT,
                    legacy_task_binding INTEGER NOT NULL DEFAULT 0 CHECK (
                        legacy_task_binding IN (0, 1)
                    ),
                    prompt TEXT,
                    prompt_sha256 TEXT,
                    created_at REAL NOT NULL,
                    activated_at REAL,
                    terminal_at REAL,
                    outcome TEXT,
                    review_id TEXT,
                    designated_root_entry_id TEXT,
                    UNIQUE (worker, generation, phase)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_slot_per_lane
                    ON round_slots(lane)
                    WHERE state IN ('prepared', 'active', 'ambiguous');
                CREATE TABLE IF NOT EXISTS worker_states (
                    worker TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_entries (
                    entry_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    slot_id TEXT NOT NULL,
                    worker TEXT NOT NULL,
                    lane TEXT NOT NULL CHECK (lane IN ('root', 'critic')),
                    kind TEXT NOT NULL CHECK (
                        kind IN ('obstacle', 'dead_end', 'critic_confirmation')
                    ),
                    confirms_entry_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_cursors (
                    worker TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (worker, stream)
                );
                CREATE TABLE IF NOT EXISTS advisor_recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state = 'owner_action_required'),
                    review_id TEXT,
                    root_entry_id TEXT NOT NULL,
                    critic_entry_id TEXT NOT NULL,
                    browser_dispatch_authorized INTEGER NOT NULL CHECK (
                        browser_dispatch_authorized = 0
                    ),
                    advisor_request_id TEXT CHECK (advisor_request_id IS NULL),
                    created_at REAL NOT NULL,
                    UNIQUE (root_entry_id, critic_entry_id)
                );
                CREATE TABLE IF NOT EXISTS obstacle_reviews (
                    review_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    root_entry_id TEXT NOT NULL UNIQUE
                        REFERENCES evidence_entries(entry_id),
                    root_slot_id TEXT NOT NULL UNIQUE
                        REFERENCES round_slots(slot_id),
                    critic_worker TEXT NOT NULL,
                    critic_slot_id TEXT UNIQUE REFERENCES round_slots(slot_id),
                    confirmation_entry_id TEXT UNIQUE
                        REFERENCES evidence_entries(entry_id),
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','active','confirmed','not_confirmed','resolved')
                    ),
                    created_at REAL NOT NULL,
                    activated_at REAL,
                    terminal_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_obstacle_review
                    ON obstacle_reviews((1))
                    WHERE state IN ('pending','active','confirmed');
                CREATE TABLE IF NOT EXISTS recommendation_resolutions (
                    resolution_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL UNIQUE
                        REFERENCES advisor_recommendations(recommendation_id),
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    resolution TEXT NOT NULL CHECK (
                        resolution IN (
                            'adopted_master_guidance','continue_without_advisor'
                        )
                    ),
                    owner_acknowledgement TEXT NOT NULL,
                    master_guidance_entry_id TEXT,
                    master_guidance_record_sha256 TEXT,
                    browser_request_id TEXT,
                    browser_receipt_sha256 TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    slot_id TEXT,
                    candidate_fact_id TEXT,
                    candidate_fact_identity TEXT,
                    source_id TEXT,
                    context_digest TEXT,
                    worker TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'observed', 'promising', 'rejected', 'selected',
                            'active', 'outcome_unknown', 'terminal'
                        )
                    ),
                    outcome TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    terminal_at REAL,
                    owner_resolution TEXT CHECK (
                        owner_resolution IS NULL OR owner_resolution IN (
                            'known_no_promotion','abandon_unknown'
                        )
                    ),
                    owner_acknowledged_unknown INTEGER CHECK (
                        owner_acknowledged_unknown IS NULL
                        OR owner_acknowledged_unknown = 1
                    ),
                    candidate_fact_active_at_resolution INTEGER CHECK (
                        candidate_fact_active_at_resolution IS NULL
                        OR candidate_fact_active_at_resolution IN (0, 1)
                    ),
                    owner_resolved_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_candidate_overlay
                    ON candidates((1))
                    WHERE state IN ('active','outcome_unknown');
                """
            )
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                self._create_generation_tasks_table_locked(connection)
                connection.execute(
                    """
                    INSERT INTO project_state(
                        singleton, schema_version, mode, config_digest,
                        root_worker, critic_worker, generation, phase,
                        phase_started_at, phase_deadline_at, recommendation_id,
                        active_review_id,
                        updated_at
                    ) VALUES(1, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        self._database_schema_version,
                        self.config.mode,
                        self._roster_digest,
                        self.roster.root,
                        self.roster.critic,
                        REASONING_PHASE,
                        now,
                        now + self.config.phase_timeout_seconds,
                        now,
                    ),
                )
            elif (
                row["mode"] != self.config.mode
                or row["config_digest"] != self._roster_digest
                or row["root_worker"] != self.roster.root
                or row["critic_worker"] != self.roster.critic
            ):
                raise CoordinationError(
                    "project coordination configuration changed after initialization"
                )
            else:
                schema_version = int(row["schema_version"])
                columns = {
                    str(column["name"])
                    for column in connection.execute(
                        "PRAGMA table_info(candidates)"
                    ).fetchall()
                }
                if schema_version == 4:
                    if "candidate_fact_identity" not in columns:
                        overlay_count = int(
                            connection.execute(
                                """
                                SELECT COUNT(*) FROM candidates
                                WHERE state IN ('active','outcome_unknown')
                                """
                            ).fetchone()[0]
                        )
                        if overlay_count:
                            raise CoordinationError(
                                "coordination schema v4 has an active candidate "
                                "whose full identity cannot be safely migrated"
                            )
                        connection.execute(
                            "ALTER TABLE candidates "
                            "ADD COLUMN candidate_fact_identity TEXT"
                        )
                        columns.add("candidate_fact_identity")
                    changed = connection.execute(
                        """
                        UPDATE project_state
                        SET schema_version=5, updated_at=?
                        WHERE singleton=1 AND schema_version=4
                        """,
                        (now,),
                    ).rowcount
                    if changed != 1:
                        raise CoordinationError(
                            "coordination schema migration lost its CAS"
                        )
                    schema_version = 5
                elif schema_version not in {
                    5,
                    6,
                    TWO_LANE_SCHEMA_VERSION,
                    SCHEMA_VERSION,
                }:
                    raise CoordinationError(
                        f"unsupported coordination schema version {schema_version}"
                    )
                if "candidate_fact_identity" not in columns:
                    raise CoordinationError(
                        "coordination schema is missing candidate full identity"
                    )
                if schema_version == 5:
                    migration_columns = {
                        "project_state": {"active_review_id": "TEXT"},
                        "round_slots": {
                            "review_id": "TEXT",
                            "designated_root_entry_id": "TEXT",
                        },
                        "advisor_recommendations": {"review_id": "TEXT"},
                    }
                    for table, additions in migration_columns.items():
                        existing_columns = {
                            str(column["name"])
                            for column in connection.execute(
                                f"PRAGMA table_info({table})"
                            ).fetchall()
                        }
                        for name, column_type in additions.items():
                            if name not in existing_columns:
                                connection.execute(
                                    f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"
                                )
                    recommendation_id = row["recommendation_id"]
                    if recommendation_id is not None:
                        self._migrate_v5_open_recommendation_locked(
                            connection,
                            recommendation_id=str(recommendation_id),
                            generation=int(row["generation"]),
                        )
                    elif self.roster.critic is not None:
                        current_root_entries = connection.execute(
                            """
                            SELECT evidence.entry_id, evidence.generation,
                                   evidence.slot_id
                            FROM evidence_entries AS evidence
                            JOIN round_slots AS slots
                              ON slots.slot_id=evidence.slot_id
                            WHERE evidence.generation=? AND evidence.lane='root'
                              AND evidence.kind IN ('obstacle','dead_end')
                            ORDER BY evidence.created_at, evidence.entry_id
                            """,
                            (int(row["generation"]),),
                        ).fetchall()
                        if len(current_root_entries) > 1:
                            raise CoordinationError(
                                "current generation has multiple root obstacles; "
                                "review migration cannot designate one safely"
                            )
                        if current_root_entries:
                            evidence = current_root_entries[0]
                            self._ensure_review_for_root_locked(
                                connection,
                                root_entry_id=str(evidence["entry_id"]),
                                root_slot_id=str(evidence["slot_id"]),
                                generation=int(evidence["generation"]),
                                created_at=now,
                            )
                    changed = connection.execute(
                        """
                        UPDATE project_state
                        SET schema_version=6, updated_at=?
                        WHERE singleton=1 AND schema_version=5
                        """,
                        (now,),
                    ).rowcount
                    if changed != 1:
                        raise CoordinationError(
                            "coordination review schema migration lost its CAS"
                        )
                    schema_version = 6
                if schema_version == 6:
                    if self.config.max_paid_workers > 2:
                        raise CoordinationError(
                            "coordination schema v6 cannot be expanded to explorer lanes"
                        )
                    nonterminal_slots = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM round_slots
                            WHERE state!='terminal'
                            """
                        ).fetchone()[0]
                    )
                    if nonterminal_slots:
                        raise CoordinationError(
                            "coordination schema v6 has a nonterminal paid slot "
                            "whose exact task binding cannot be safely migrated"
                        )
                    migration_project = self._state(connection)
                    current_generation = int(migration_project["generation"])
                    current_slots = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM round_slots
                            WHERE generation=?
                            """,
                            (current_generation,),
                        ).fetchone()[0]
                    )
                    if current_slots:
                        recommendation_id = migration_project["recommendation_id"]
                        if (
                            migration_project["phase"] != OWNER_ACTION_REQUIRED_PHASE
                            or recommendation_id is None
                        ):
                            raise CoordinationError(
                                "coordination schema v6 has current-generation paid "
                                "slot history whose complete exact task binding cannot "
                                "be safely migrated"
                            )
                        # The sole safe exception is immutable terminal history at
                        # an exact owner gate.  That transition freezes a complete
                        # generation+1 task set and never carries these legacy
                        # current-generation tasks forward.
                        try:
                            self._open_recommendation_projection_locked(
                                connection,
                                str(recommendation_id),
                            )
                        except CoordinationError as exc:
                            raise CoordinationError(
                                "coordination schema v6 owner-action slot history is "
                                "not a complete terminal recommendation and cannot be "
                                "safely migrated"
                            ) from exc
                    if (
                        connection.execute(
                            """
                            SELECT 1 FROM sqlite_master
                            WHERE type='table' AND name='generation_tasks'
                            """
                        ).fetchone()
                        is not None
                    ):
                        raise CoordinationError(
                            "coordination schema v6 has an unexpected task table"
                        )
                    slot_columns = {
                        str(column["name"])
                        for column in connection.execute(
                            "PRAGMA table_info(round_slots)"
                        ).fetchall()
                    }
                    task_columns = {
                        "task": "TEXT",
                        "task_sha256": "TEXT",
                        "task_bytes": "INTEGER",
                        "prompt_task_sha256": "TEXT",
                        "legacy_task_binding": (
                            "INTEGER NOT NULL DEFAULT 1 CHECK "
                            "(legacy_task_binding IN (0, 1))"
                        ),
                    }
                    for name, column_type in task_columns.items():
                        if name in slot_columns:
                            raise CoordinationError(
                                "coordination schema v6 has partial task-binding columns"
                            )
                        connection.execute(
                            f"ALTER TABLE round_slots ADD COLUMN {name} {column_type}"
                        )
                    self._create_generation_tasks_table_locked(connection)
                    changed = connection.execute(
                        """
                        UPDATE project_state
                        SET schema_version=?, updated_at=?
                        WHERE singleton=1 AND schema_version=6
                        """,
                        (TWO_LANE_SCHEMA_VERSION, now),
                    ).rowcount
                    if changed != 1:
                        raise CoordinationError(
                            "coordination task-binding schema migration lost its CAS"
                        )
                    schema_version = TWO_LANE_SCHEMA_VERSION
                if (
                    schema_version == TWO_LANE_SCHEMA_VERSION
                    and self.config.max_paid_workers > 2
                ):
                    raise CoordinationError(
                        "coordination schema v7 cannot represent explorer lanes"
                    )
                invalid_overlay = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM candidates
                        WHERE state IN ('active','outcome_unknown')
                          AND (
                            candidate_fact_identity IS NULL
                            OR length(candidate_fact_identity) != 64
                            OR candidate_fact_identity GLOB '*[^0-9a-f]*'
                          )
                        """
                    ).fetchone()[0]
                )
                if invalid_overlay:
                    raise CoordinationError(
                        "active candidate has no canonical full fact identity"
                    )
            self._audit_task_bindings_locked(connection)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _task_identity(task: str) -> tuple[str, int]:
        if not isinstance(task, str) or not task.strip():
            raise CoordinationError("task assignment must be non-empty text")
        try:
            encoded = task.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CoordinationError("task assignment is not valid UTF-8") from exc
        if len(encoded) > MAX_TASK_BYTES:
            raise CoordinationError("task assignment exceeds its hard limit")
        return hashlib.sha256(encoded).hexdigest(), len(encoded)

    def _validate_generation_task_row(
        self,
        row: sqlite3.Row,
    ) -> tuple[str, int]:
        worker = str(row["worker"])
        lane = str(row["lane"])
        if self.roster.lanes.get(worker) != lane:
            raise CoordinationError(
                "generation task is outside the protected paid-lane roster"
            )
        generation = row["generation"]
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise CoordinationError("generation task has an invalid generation")
        digest, byte_count = self._task_identity(row["task"])
        if (
            row["task_sha256"] != digest
            or isinstance(row["task_bytes"], bool)
            or row["task_bytes"] != byte_count
        ):
            raise CoordinationError(
                "generation task bytes do not match their durable identity"
            )
        for value in (row["staged_at"], row["frozen_at"]):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise CoordinationError("generation task has an invalid timestamp")
        if row["frozen_at"] is not None and float(row["frozen_at"]) < float(
            row["staged_at"]
        ):
            raise CoordinationError(
                "generation task freeze predates its durable staging"
            )
        return digest, byte_count

    @staticmethod
    def _prompt_binding_markers(slot: sqlite3.Row) -> tuple[str, str, str]:
        return (
            f"coordination_slot_id={slot['slot_id']}",
            f"generation={int(slot['generation'])}",
            f"task_sha256={slot['task_sha256']}",
        )

    def _validate_slot_task_binding_locked(
        self,
        connection: sqlite3.Connection,
        slot: sqlite3.Row,
    ) -> None:
        legacy = slot["legacy_task_binding"]
        if legacy == 1:
            if slot["state"] != "terminal" or any(
                slot[key] is not None
                for key in (
                    "task",
                    "task_sha256",
                    "task_bytes",
                    "prompt_task_sha256",
                )
            ):
                raise CoordinationError(
                    "legacy task binding is not an exact terminal migration"
                )
            return
        if legacy != 0:
            raise CoordinationError("round slot has an invalid task-binding version")
        digest, byte_count = self._task_identity(slot["task"])
        if (
            slot["task_sha256"] != digest
            or isinstance(slot["task_bytes"], bool)
            or slot["task_bytes"] != byte_count
        ):
            raise CoordinationError("round slot task snapshot failed its durable hash")
        assignment = connection.execute(
            """
            SELECT * FROM generation_tasks
            WHERE worker=? AND generation=?
            """,
            (slot["worker"], slot["generation"]),
        ).fetchone()
        if assignment is None:
            raise CoordinationError(
                "round slot has no matching durable generation task"
            )
        self._validate_generation_task_row(assignment)
        if (
            assignment["lane"] != slot["lane"]
            or assignment["task"] != slot["task"]
            or assignment["task_sha256"] != digest
            or assignment["task_bytes"] != byte_count
            or assignment["frozen_at"] is None
        ):
            raise CoordinationError(
                "round slot conflicts with its durable generation task"
            )
        prompt = slot["prompt"]
        prompt_digest = slot["prompt_sha256"]
        prompt_task_digest = slot["prompt_task_sha256"]
        if prompt is None:
            if prompt_digest is not None or prompt_task_digest is not None:
                raise CoordinationError(
                    "round slot has a partial generated-prompt binding"
                )
            if slot["state"] in {"active", "ambiguous"}:
                raise CoordinationError("dispatched round slot has no pinned prompt")
            return
        if not isinstance(prompt, str) or not prompt:
            raise CoordinationError("round slot has an invalid pinned prompt")
        try:
            encoded = prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CoordinationError("round slot prompt is not valid UTF-8") from exc
        prompt_lines = set(prompt.splitlines())
        if (
            len(encoded) > MAX_PINNED_PROMPT_BYTES
            or not isinstance(prompt_digest, str)
            or _SHA256_RE.fullmatch(prompt_digest) is None
            or hashlib.sha256(encoded).hexdigest() != prompt_digest
            or prompt_task_digest != digest
            or any(
                marker not in prompt_lines
                for marker in self._prompt_binding_markers(slot)
            )
        ):
            raise CoordinationError(
                "round slot prompt conflicts with its task/slot/generation binding"
            )

    def _audit_task_bindings_locked(self, connection: sqlite3.Connection) -> None:
        project = self._state(connection)
        schema_version = int(project["schema_version"])
        supported_versions = (
            {SCHEMA_VERSION}
            if self.config.max_paid_workers > 2
            else {TWO_LANE_SCHEMA_VERSION, SCHEMA_VERSION}
        )
        if schema_version not in supported_versions:
            raise CoordinationError(
                "coordination task-binding schema version is inconsistent"
            )
        slot_columns = {
            str(column["name"])
            for column in connection.execute(
                "PRAGMA table_info(round_slots)"
            ).fetchall()
        }
        required_slot_columns = {
            "task",
            "task_sha256",
            "task_bytes",
            "prompt_task_sha256",
            "legacy_task_binding",
        }
        if not required_slot_columns.issubset(slot_columns):
            raise CoordinationError(
                "coordination schema is missing round-slot task bindings"
            )
        task_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='generation_tasks'
            """
        ).fetchone()
        if task_table is None:
            raise CoordinationError(
                "coordination schema is missing durable generation tasks"
            )
        task_columns = {
            str(column["name"])
            for column in connection.execute(
                "PRAGMA table_info(generation_tasks)"
            ).fetchall()
        }
        if task_columns != {
            "worker",
            "generation",
            "lane",
            "task",
            "task_sha256",
            "task_bytes",
            "staged_at",
            "frozen_at",
        }:
            raise CoordinationError("coordination generation-task schema is malformed")
        current_generation = int(project["generation"])
        for assignment in connection.execute(
            "SELECT * FROM generation_tasks"
        ).fetchall():
            self._validate_generation_task_row(assignment)
            if int(assignment["generation"]) > current_generation + 1:
                raise CoordinationError(
                    "generation task targets an impossible future generation"
                )
            if (
                int(assignment["generation"]) == current_generation + 1
                and project["phase"] != OWNER_ACTION_REQUIRED_PHASE
            ):
                raise CoordinationError(
                    "future generation task exists outside owner-action staging"
                )
        for slot in connection.execute("SELECT * FROM round_slots").fetchall():
            self._validate_slot_task_binding_locked(connection, slot)

    def _state(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM project_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise CoordinationError("coordination project_state is missing")
        return row

    def _worker_lane(self, worker: str) -> str:
        _validate_identifier(worker, "worker")
        return self.roster.lanes.get(worker, "observer")

    def _required_task_workers(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.roster.lanes.items())

    def _task_staging_projection_locked(
        self,
        connection: sqlite3.Connection,
        *,
        generation: int,
    ) -> dict[str, Any]:
        required = self._required_task_workers()
        rows = {
            str(row["worker"]): row
            for row in connection.execute(
                """
                SELECT * FROM generation_tasks
                WHERE generation=?
                ORDER BY lane, worker
                """,
                (generation,),
            ).fetchall()
        }
        assignments: list[dict[str, Any]] = []
        missing: list[str] = []
        for worker, lane in required:
            row = rows.get(worker)
            if row is None:
                missing.append(worker)
                continue
            self._validate_generation_task_row(row)
            if row["lane"] != lane:
                raise CoordinationError(
                    "generation task lane conflicts with the protected roster"
                )
            assignments.append(
                {
                    "worker": worker,
                    "lane": lane,
                    "generation": generation,
                    "task_sha256": str(row["task_sha256"]),
                    "task_bytes": int(row["task_bytes"]),
                    "frozen": row["frozen_at"] is not None,
                }
            )
        unexpected = set(rows).difference(worker for worker, _lane in required)
        if unexpected:
            raise CoordinationError(
                "generation task includes a worker outside the paid roster"
            )
        return {
            "generation": generation,
            "required_workers": [worker for worker, _lane in required],
            "assignments": assignments,
            "missing_workers": missing,
            "ready": not missing,
        }

    def staged_task_assignments(
        self,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """Return digest-only coverage for one durable task generation."""

        connection = self._connect()
        try:
            project = self._state(connection)
            current = int(project["generation"])
            target = (
                current + 1
                if generation is None
                and project["phase"] == OWNER_ACTION_REQUIRED_PHASE
                else current
                if generation is None
                else generation
            )
            if (
                isinstance(target, bool)
                or not isinstance(target, int)
                or target < 1
                or target > current + 1
            ):
                raise CoordinationError(
                    "task-staging generation is outside the durable horizon"
                )
            return self._task_staging_projection_locked(
                connection,
                generation=target,
            )
        finally:
            connection.close()

    def stage_task_assignment(
        self,
        worker: str,
        task: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Stage exact task bytes for current admission or the gated next generation.

        A conflicting assignment may replace an unfrozen staged row, preserving
        assign overwrite semantics. Once any slot exists for that worker and
        generation, or owner resolution freezes the next generation, only
        byte-identical replay is accepted.
        """

        worker = _validate_identifier(worker, "worker")
        lane = self._worker_lane(worker)
        if worker not in self.roster.lanes:
            raise CoordinationError(
                "only protected paid-lane workers have durable task assignments"
            )
        digest, byte_count = self._task_identity(task)
        staged_at = None if now is None else float(now)
        if staged_at is not None and not math.isfinite(staged_at):
            raise CoordinationError("task assignment timestamp is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if staged_at is None:
                staged_at = time.time()
            project = self._state(connection)
            generation = int(project["generation"])
            phase = str(project["phase"])
            if self._active_candidate(connection) is not None:
                raise CoordinationError(
                    "active candidate freezes durable task assignment"
                )
            target_generation = (
                generation + 1 if phase == OWNER_ACTION_REQUIRED_PHASE else generation
            )
            existing = connection.execute(
                """
                SELECT * FROM generation_tasks
                WHERE worker=? AND generation=?
                """,
                (worker, target_generation),
            ).fetchone()
            slot_exists = connection.execute(
                """
                SELECT 1 FROM round_slots
                WHERE worker=? AND generation=?
                """,
                (worker, target_generation),
            ).fetchone()
            if existing is not None:
                self._validate_generation_task_row(existing)
                same = (
                    existing["lane"] == lane
                    and existing["task"] == task
                    and existing["task_sha256"] == digest
                    and existing["task_bytes"] == byte_count
                )
                if same:
                    connection.commit()
                    return {
                        "worker": worker,
                        "lane": lane,
                        "generation": target_generation,
                        "task_sha256": digest,
                        "task_bytes": byte_count,
                        "frozen": existing["frozen_at"] is not None,
                        "replayed": True,
                        "replaced": False,
                    }
                if existing["frozen_at"] is not None or slot_exists is not None:
                    raise CoordinationError(
                        "durable task assignment is frozen for this generation"
                    )
                changed = connection.execute(
                    """
                    UPDATE generation_tasks
                    SET lane=?, task=?, task_sha256=?, task_bytes=?, staged_at=?
                    WHERE worker=? AND generation=? AND frozen_at IS NULL
                    """,
                    (
                        lane,
                        task,
                        digest,
                        byte_count,
                        staged_at,
                        worker,
                        target_generation,
                    ),
                ).rowcount
                if changed != 1:
                    raise CoordinationError("task assignment replacement lost its CAS")
                replaced = True
            else:
                if slot_exists is not None:
                    raise CoordinationError(
                        "round slot exists without its durable task assignment"
                    )
                connection.execute(
                    """
                    INSERT INTO generation_tasks(
                        worker, generation, lane, task, task_sha256, task_bytes,
                        staged_at, frozen_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        worker,
                        target_generation,
                        lane,
                        task,
                        digest,
                        byte_count,
                        staged_at,
                    ),
                )
                replaced = False
            connection.commit()
            return {
                "worker": worker,
                "lane": lane,
                "generation": target_generation,
                "task_sha256": digest,
                "task_bytes": byte_count,
                "frozen": False,
                "replayed": False,
                "replaced": replaced,
            }
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _freeze_required_generation_tasks_locked(
        self,
        connection: sqlite3.Connection,
        *,
        generation: int,
        frozen_at: float,
    ) -> dict[str, Any]:
        projection = self._task_staging_projection_locked(
            connection,
            generation=generation,
        )
        if not projection["ready"]:
            missing = ", ".join(projection["missing_workers"])
            raise CoordinationError(
                f"generation {generation} task staging is incomplete: {missing}"
            )
        changed = connection.execute(
            """
            UPDATE generation_tasks
            SET frozen_at=?
            WHERE generation=? AND frozen_at IS NULL
            """,
            (frozen_at, generation),
        ).rowcount
        expected_unfrozen = sum(
            not bool(item["frozen"]) for item in projection["assignments"]
        )
        if changed != expected_unfrozen:
            raise CoordinationError("generation task freeze lost its exact CAS")
        return self._task_staging_projection_locked(
            connection,
            generation=generation,
        )

    def _copy_forward_generation_tasks_locked(
        self,
        connection: sqlite3.Connection,
        *,
        generation: int,
        staged_at: float,
    ) -> None:
        source = self._task_staging_projection_locked(
            connection,
            generation=generation,
        )
        if not source["ready"] or any(
            not bool(item["frozen"]) for item in source["assignments"]
        ):
            raise CoordinationError(
                "completed generation has no exact frozen task set to carry forward"
            )
        if (
            connection.execute(
                "SELECT 1 FROM generation_tasks WHERE generation=?",
                (generation + 1,),
            ).fetchone()
            is not None
        ):
            raise CoordinationError(
                "automatic generation advance found conflicting future tasks"
            )
        for worker, lane in self._required_task_workers():
            row = connection.execute(
                """
                SELECT * FROM generation_tasks
                WHERE worker=? AND generation=?
                """,
                (worker, generation),
            ).fetchone()
            if row is None:
                raise CoordinationError(
                    "completed generation task disappeared during carry-forward"
                )
            connection.execute(
                """
                INSERT INTO generation_tasks(
                    worker, generation, lane, task, task_sha256, task_bytes,
                    staged_at, frozen_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    worker,
                    generation + 1,
                    lane,
                    row["task"],
                    row["task_sha256"],
                    row["task_bytes"],
                    staged_at,
                ),
            )

    def _canonical_paid_slot(
        self,
        connection: sqlite3.Connection,
        worker: str,
        *,
        slot_id: str | None = None,
    ) -> sqlite3.Row:
        """Resolve provenance only from a canonical dispatched/recovering slot."""

        worker = _validate_identifier(worker, "worker")
        if slot_id is not None:
            slot_id = _validate_identifier(slot_id, "slot_id")
        row = connection.execute(
            """
            SELECT * FROM round_slots
            WHERE worker=? AND state IN ('active','ambiguous')
              AND (? IS NULL OR slot_id=?)
            """,
            (worker, slot_id, slot_id),
        ).fetchone()
        if row is None:
            raise CoordinationError("worker has no matching canonical paid slot")
        project = self._state(connection)
        if int(row["generation"]) != int(project["generation"]) or str(
            row["lane"]
        ) != self._worker_lane(worker):
            raise CoordinationError("paid slot is not in the current canonical phase")
        self._validate_slot_task_binding_locked(connection, row)
        return row

    def _terminal_reconciliation_slot(
        self,
        connection: sqlite3.Connection,
        worker: str,
        *,
        slot_id: str,
    ) -> sqlite3.Row:
        """Resolve one exact live-or-terminal slot for receipt-bound replay."""

        worker = _validate_identifier(worker, "worker")
        slot_id = _validate_identifier(slot_id, "slot_id")
        row = connection.execute(
            """
            SELECT * FROM round_slots
            WHERE worker=? AND slot_id=?
              AND state IN ('active','ambiguous','terminal')
            """,
            (worker, slot_id),
        ).fetchone()
        if row is None:
            raise CoordinationError("worker has no exact terminal-reconciliation slot")
        if str(row["lane"]) != self._worker_lane(worker):
            raise CoordinationError(
                "terminal-reconciliation slot is outside the canonical roster"
            )
        project_generation = int(self._state(connection)["generation"])
        slot_generation = int(row["generation"])
        if row["state"] != "terminal" and slot_generation != project_generation:
            raise CoordinationError(
                "live terminal-reconciliation slot is outside the current generation"
            )
        if row["state"] == "terminal" and slot_generation > project_generation:
            raise CoordinationError(
                "terminal-reconciliation slot is from an impossible future generation"
            )
        return row

    def paid_slot_provenance(self, worker: str) -> dict[str, Any] | None:
        """Return the protected, content-free provenance injected by the gateway."""

        connection = self._connect()
        try:
            try:
                slot = self._canonical_paid_slot(connection, worker)
            except CoordinationError:
                return None
            return {
                "slot_id": str(slot["slot_id"]),
                "generation": int(slot["generation"]),
                "lane": str(slot["lane"]),
            }
        finally:
            connection.close()

    def validate_memory_publication(
        self,
        worker: str,
        *,
        slot_id: str,
        kind: str,
        confirms_entry_id: str | None,
    ) -> dict[str, Any]:
        """Preflight review-sensitive GM writes before the append boundary.

        The return value contains only protected coordinator identities.  It is
        intentionally separate from ``links.coordination`` so the durable GM
        provenance shape remains the exact three-field receipt contract.
        """

        if not isinstance(kind, str):
            raise CoordinationError("global-memory kind must be text")
        if confirms_entry_id is not None:
            confirms_entry_id = _validate_identifier(
                confirms_entry_id, "confirms_entry_id"
            )
        connection = self._connect()
        try:
            slot = self._canonical_paid_slot(
                connection,
                worker,
                slot_id=slot_id,
            )
            lane = str(slot["lane"])
            sensitive = lane == "root" and kind in {"obstacle", "dead_end"}
            requires_existing_replay = False
            registered_entry_id = None
            if lane == "root":
                if confirms_entry_id is not None:
                    raise CoordinationError(
                        "root publications cannot carry critic confirmation links"
                    )
                if sensitive:
                    prior = connection.execute(
                        "SELECT * FROM obstacle_reviews WHERE root_slot_id=?",
                        (slot["slot_id"],),
                    ).fetchone()
                    if prior is not None:
                        requires_existing_replay = True
                        registered_entry_id = str(prior["root_entry_id"])
            elif lane == "critic" and confirms_entry_id is not None:
                if kind not in {"obstacle", "dead_end"}:
                    raise CoordinationError(
                        "critic confirmation links require obstacle/dead_end evidence"
                    )
                if (
                    slot["phase"] != CRITIC_REVIEW_PHASE
                    or slot["review_id"] is None
                    or slot["designated_root_entry_id"] != confirms_entry_id
                ):
                    raise CoordinationError(
                        "critic confirmation requires its exact designated review slot"
                    )
                review = connection.execute(
                    "SELECT * FROM obstacle_reviews WHERE review_id=?",
                    (slot["review_id"],),
                ).fetchone()
                if (
                    review is None
                    or review["state"] not in {"active", "confirmed"}
                    or review["critic_slot_id"] != slot["slot_id"]
                    or review["root_entry_id"] != confirms_entry_id
                ):
                    raise CoordinationError(
                        "critic confirmation conflicts with the active designated review"
                    )
                if review["state"] == "confirmed":
                    if review["confirmation_entry_id"] is None:
                        raise CoordinationError(
                            "confirmed critic review has no canonical confirmation"
                        )
                    requires_existing_replay = True
                    registered_entry_id = str(review["confirmation_entry_id"])
                elif review["confirmation_entry_id"] is not None:
                    raise CoordinationError(
                        "active critic review already names a confirmation"
                    )
                sensitive = True
            elif confirms_entry_id is not None:
                raise CoordinationError(
                    "explorer publications cannot confirm root evidence"
                )
            return {
                "slot_id": str(slot["slot_id"]),
                "generation": int(slot["generation"]),
                "lane": lane,
                "phase": str(slot["phase"]),
                "review_id": (
                    str(slot["review_id"]) if slot["review_id"] is not None else None
                ),
                "designated_root_entry_id": (
                    str(slot["designated_root_entry_id"])
                    if slot["designated_root_entry_id"] is not None
                    else None
                ),
                "bounded_review_record": sensitive,
                "requires_existing_replay": requires_existing_replay,
                "registered_entry_id": registered_entry_id,
            }
        finally:
            connection.close()

    def _canonical_candidate_slot(
        self,
        connection: sqlite3.Connection,
        worker: str,
        *,
        slot_id: str,
        require_current_generation: bool = True,
    ) -> sqlite3.Row:
        worker = _validate_identifier(worker, "worker")
        slot_id = _validate_identifier(slot_id, "slot_id")
        row = connection.execute(
            """
            SELECT * FROM round_slots
            WHERE worker=? AND slot_id=?
              AND state IN ('active','ambiguous','terminal')
            """,
            (worker, slot_id),
        ).fetchone()
        if row is None:
            raise CoordinationError("candidate has no matching canonical slot")
        project = self._state(connection)
        if str(row["lane"]) != self._worker_lane(worker) or (
            require_current_generation
            and int(row["generation"]) != int(project["generation"])
        ):
            raise CoordinationError("candidate slot is not in the canonical generation")
        return row

    @staticmethod
    def _active_candidate(
        connection: sqlite3.Connection,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM candidates
            WHERE state IN ('active','outcome_unknown')
            ORDER BY created_at, candidate_id LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _review_projection(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "review_id": str(row["review_id"]),
            "generation": int(row["generation"]),
            "root_entry_id": str(row["root_entry_id"]),
            "root_slot_id": str(row["root_slot_id"]),
            "critic_worker": str(row["critic_worker"]),
            "critic_slot_id": (
                str(row["critic_slot_id"])
                if row["critic_slot_id"] is not None
                else None
            ),
            "confirmation_entry_id": (
                str(row["confirmation_entry_id"])
                if row["confirmation_entry_id"] is not None
                else None
            ),
            "state": str(row["state"]),
        }

    @staticmethod
    def _resolution_projection(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "resolution_id": str(row["resolution_id"]),
            "recommendation_id": str(row["recommendation_id"]),
            "generation": int(row["generation"]),
            "resolution": str(row["resolution"]),
            "owner_acknowledgement": str(row["owner_acknowledgement"]),
            "master_guidance_entry_id": row["master_guidance_entry_id"],
            "master_guidance_record_sha256": row["master_guidance_record_sha256"],
            "browser_request_id": row["browser_request_id"],
            "browser_receipt_sha256": row["browser_receipt_sha256"],
            "created_at": float(row["created_at"]),
        }

    def _active_review(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        project = self._state(connection)
        review_id = project["active_review_id"]
        if review_id is None:
            return None
        review = connection.execute(
            "SELECT * FROM obstacle_reviews WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if review is None or review["state"] not in {
            "pending",
            "active",
            "confirmed",
        }:
            raise CoordinationError("active obstacle review pointer is inconsistent")
        return review

    def _ensure_review_for_root_locked(
        self,
        connection: sqlite3.Connection,
        *,
        root_entry_id: str,
        root_slot_id: str,
        generation: int,
        created_at: float,
    ) -> str | None:
        """Create/replay the one immutable review designated by a root slot."""

        if self.roster.critic is None:
            return None
        root_entry_id = _validate_identifier(root_entry_id, "root_entry_id")
        root_slot_id = _validate_identifier(root_slot_id, "root_slot_id")
        expected_review_id = obstacle_review_id(
            generation=generation,
            root_entry_id=root_entry_id,
            root_slot_id=root_slot_id,
            critic_worker=self.roster.critic,
        )
        slot = connection.execute(
            "SELECT * FROM round_slots WHERE slot_id=?",
            (root_slot_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT * FROM evidence_entries WHERE entry_id=?",
            (root_entry_id,),
        ).fetchone()
        if (
            slot is None
            or evidence is None
            or slot["lane"] != "root"
            or slot["worker"] != self.roster.root
            or int(slot["generation"]) != generation
            or evidence["slot_id"] != root_slot_id
            or int(evidence["generation"]) != generation
            or evidence["lane"] != "root"
            or evidence["worker"] != self.roster.root
            or evidence["worker"] != slot["worker"]
            or evidence["kind"] not in {"obstacle", "dead_end"}
        ):
            raise CoordinationError("root review identity is not canonical evidence")
        existing_for_slot = connection.execute(
            "SELECT * FROM obstacle_reviews WHERE root_slot_id=?",
            (root_slot_id,),
        ).fetchone()
        if existing_for_slot is not None:
            expected = (
                expected_review_id,
                generation,
                root_entry_id,
                root_slot_id,
                self.roster.critic,
            )
            observed = tuple(
                existing_for_slot[key]
                for key in (
                    "review_id",
                    "generation",
                    "root_entry_id",
                    "root_slot_id",
                    "critic_worker",
                )
            )
            if observed != expected:
                raise CoordinationError(
                    "root slot already designated a different obstacle review"
                )
            return str(existing_for_slot["review_id"])
        other_live = connection.execute(
            """
            SELECT review_id FROM obstacle_reviews
            WHERE state IN ('pending','active','confirmed')
            """
        ).fetchone()
        if other_live is not None:
            raise CoordinationError(
                "another obstacle review is already live for this project"
            )
        connection.execute(
            """
            INSERT INTO obstacle_reviews(
                review_id, generation, root_entry_id, root_slot_id,
                critic_worker, critic_slot_id, confirmation_entry_id, state,
                created_at, activated_at, terminal_at
            ) VALUES(?, ?, ?, ?, ?, NULL, NULL, 'pending', ?, NULL, NULL)
            """,
            (
                expected_review_id,
                generation,
                root_entry_id,
                root_slot_id,
                self.roster.critic,
                created_at,
            ),
        )
        changed = connection.execute(
            """
            UPDATE project_state SET active_review_id=?, updated_at=?
            WHERE singleton=1 AND generation=? AND active_review_id IS NULL
              AND recommendation_id IS NULL
            """,
            (expected_review_id, created_at, generation),
        ).rowcount
        if changed != 1:
            raise CoordinationError("root review pointer transition lost its CAS")
        return expected_review_id

    def _migrate_v5_open_recommendation_locked(
        self,
        connection: sqlite3.Connection,
        *,
        recommendation_id: str,
        generation: int,
    ) -> str:
        """Bind one exact legacy open recommendation to a confirmed v6 review."""

        recommendation_id = _validate_identifier(
            recommendation_id,
            "recommendation_id",
        )
        open_recommendations = connection.execute(
            """
            SELECT recommendation_id FROM advisor_recommendations
            WHERE generation=? AND state=?
            ORDER BY recommendation_id
            """,
            (generation, OWNER_ACTION_REQUIRED_PHASE),
        ).fetchall()
        if (
            len(open_recommendations) != 1
            or open_recommendations[0]["recommendation_id"] != recommendation_id
        ):
            raise CoordinationError(
                "v5 migration does not have one unique open recommendation"
            )
        recommendation = connection.execute(
            "SELECT * FROM advisor_recommendations WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        if (
            recommendation is None
            or int(recommendation["generation"]) != generation
            or recommendation["state"] != OWNER_ACTION_REQUIRED_PHASE
            or bool(recommendation["browser_dispatch_authorized"])
            or recommendation["advisor_request_id"] is not None
            or recommendation["review_id"] is not None
            or self.roster.critic is None
        ):
            raise CoordinationError(
                "v5 open recommendation is not a unique fail-closed owner action"
            )
        if (
            connection.execute(
                "SELECT 1 FROM recommendation_resolutions WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
            is not None
        ):
            raise CoordinationError("v5 open recommendation is already resolved")

        root_rows = connection.execute(
            """
            SELECT evidence.*, slots.state AS slot_state,
                   slots.worker AS slot_worker, slots.lane AS slot_lane,
                   slots.generation AS slot_generation,
                   slots.phase AS slot_phase,
                   slots.directive AS slot_directive,
                   slots.prompt AS slot_prompt,
                   slots.prompt_sha256 AS slot_prompt_sha256,
                   slots.created_at AS slot_created_at,
                   slots.activated_at AS slot_activated_at,
                   slots.terminal_at AS slot_terminal_at,
                   slots.outcome AS slot_outcome,
                   slots.review_id AS slot_review_id,
                   slots.designated_root_entry_id AS slot_designated_root_entry_id
            FROM evidence_entries AS evidence
            JOIN round_slots AS slots ON slots.slot_id=evidence.slot_id
            WHERE evidence.generation=? AND evidence.lane='root'
              AND evidence.kind IN ('obstacle','dead_end')
            ORDER BY evidence.created_at, evidence.entry_id
            """,
            (generation,),
        ).fetchall()
        if len(root_rows) != 1:
            raise CoordinationError(
                "v5 open recommendation does not have one unique root obstacle"
            )
        root = root_rows[0]
        if (
            root["entry_id"] != recommendation["root_entry_id"]
            or root["worker"] != self.roster.root
            or root["slot_worker"] != self.roster.root
            or root["slot_lane"] != "root"
            or int(root["slot_generation"]) != generation
            or root["slot_state"] != "terminal"
            or root["slot_terminal_at"] is None
            or root["confirms_entry_id"] is not None
        ):
            raise CoordinationError(
                "v5 open recommendation root evidence is not exact terminal evidence"
            )
        self._validate_v5_terminal_dispatch_provenance(
            root,
            lane="root",
            worker=self.roster.root,
            generation=generation,
        )

        critic_rows = connection.execute(
            """
            SELECT evidence.*, slots.state AS slot_state,
                   slots.worker AS slot_worker, slots.lane AS slot_lane,
                   slots.generation AS slot_generation,
                   slots.phase AS slot_phase,
                   slots.directive AS slot_directive,
                   slots.prompt AS slot_prompt,
                   slots.prompt_sha256 AS slot_prompt_sha256,
                   slots.created_at AS slot_created_at,
                   slots.activated_at AS slot_activated_at,
                   slots.terminal_at AS slot_terminal_at,
                   slots.outcome AS slot_outcome,
                   slots.review_id AS slot_review_id,
                   slots.designated_root_entry_id AS slot_designated_root_entry_id
            FROM evidence_entries AS evidence
            JOIN round_slots AS slots ON slots.slot_id=evidence.slot_id
            WHERE evidence.generation=? AND evidence.lane='critic'
              AND evidence.kind='critic_confirmation'
            ORDER BY evidence.created_at, evidence.entry_id
            """,
            (generation,),
        ).fetchall()
        if len(critic_rows) != 1:
            raise CoordinationError(
                "v5 open recommendation does not have one unique critic confirmation"
            )
        critic = critic_rows[0]
        if (
            critic["entry_id"] != recommendation["critic_entry_id"]
            or critic["confirms_entry_id"] != root["entry_id"]
            or critic["worker"] != self.roster.critic
            or critic["slot_worker"] != self.roster.critic
            or critic["slot_lane"] != "critic"
            or int(critic["slot_generation"]) != generation
            or critic["slot_state"] != "terminal"
            or critic["slot_terminal_at"] is None
            or critic["slot_id"] == root["slot_id"]
        ):
            raise CoordinationError(
                "v5 open recommendation critic evidence is not exact terminal evidence"
            )
        _validate_identifier(str(critic["entry_id"]), "critic_entry_id")
        _validate_identifier(str(critic["slot_id"]), "critic_slot_id")
        self._validate_v5_terminal_dispatch_provenance(
            critic,
            lane="critic",
            worker=self.roster.critic,
            generation=generation,
        )
        open_slots = int(
            connection.execute(
                "SELECT COUNT(*) FROM round_slots "
                "WHERE generation=? AND state!='terminal'",
                (generation,),
            ).fetchone()[0]
        )
        if open_slots:
            raise CoordinationError(
                "v5 open recommendation generation has nonterminal paid slots"
            )
        if (
            connection.execute("SELECT 1 FROM obstacle_reviews LIMIT 1").fetchone()
            is not None
        ):
            raise CoordinationError(
                "v5 open recommendation migration found an existing review"
            )

        review_id = obstacle_review_id(
            generation=generation,
            root_entry_id=str(root["entry_id"]),
            root_slot_id=str(root["slot_id"]),
            critic_worker=self.roster.critic,
        )
        connection.execute(
            """
            INSERT INTO obstacle_reviews(
                review_id, generation, root_entry_id, root_slot_id,
                critic_worker, critic_slot_id, confirmation_entry_id, state,
                created_at, activated_at, terminal_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?)
            """,
            (
                review_id,
                generation,
                root["entry_id"],
                root["slot_id"],
                self.roster.critic,
                critic["slot_id"],
                critic["entry_id"],
                float(recommendation["created_at"]),
                critic["slot_activated_at"],
                critic["slot_terminal_at"],
            ),
        )
        changed_recommendation = connection.execute(
            """
            UPDATE advisor_recommendations SET review_id=?
            WHERE recommendation_id=? AND generation=? AND review_id IS NULL
            """,
            (review_id, recommendation_id, generation),
        ).rowcount
        changed_project = connection.execute(
            """
            UPDATE project_state SET active_review_id=?
            WHERE singleton=1 AND generation=? AND phase=?
              AND recommendation_id=? AND active_review_id IS NULL
            """,
            (
                review_id,
                generation,
                OWNER_ACTION_REQUIRED_PHASE,
                recommendation_id,
            ),
        ).rowcount
        if changed_recommendation != 1 or changed_project != 1:
            raise CoordinationError(
                "v5 open recommendation review binding lost its exact CAS"
            )
        return review_id

    def _validate_v5_terminal_dispatch_provenance(
        self,
        row: sqlite3.Row,
        *,
        lane: str,
        worker: str,
        generation: int,
    ) -> None:
        """Fail closed unless one legacy evidence slot proves a paid dispatch."""

        prompt = row["slot_prompt"]
        prompt_digest = row["slot_prompt_sha256"]
        try:
            prompt_bytes = prompt.encode("utf-8") if isinstance(prompt, str) else b""
        except UnicodeEncodeError as exc:
            raise CoordinationError(
                f"v5 {lane} paid slot lacks canonical dispatch provenance"
            ) from exc
        timestamps = (
            row["slot_created_at"],
            row["slot_activated_at"],
            row["slot_terminal_at"],
        )
        expected_directive = coordination_directive(
            lane=lane,
            generation=generation,
            phase=REASONING_PHASE,
        )
        if (
            row["slot_worker"] != worker
            or row["slot_lane"] != lane
            or int(row["slot_generation"]) != generation
            or row["slot_phase"] != REASONING_PHASE
            or row["slot_state"] != "terminal"
            or row["slot_directive"] != expected_directive
            or row["slot_review_id"] is not None
            or row["slot_designated_root_entry_id"] is not None
            or not isinstance(prompt, str)
            or not prompt
            or len(prompt_bytes) > MAX_PINNED_PROMPT_BYTES
            or not isinstance(prompt_digest, str)
            or _SHA256_RE.fullmatch(prompt_digest) is None
            or hashlib.sha256(prompt_bytes).hexdigest() != prompt_digest
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in timestamps
            )
            or not isinstance(row["slot_outcome"], str)
            or not row["slot_outcome"]
            or len(row["slot_outcome"].encode("utf-8")) > MAX_OUTCOME_BYTES
        ):
            raise CoordinationError(
                f"v5 {lane} paid slot lacks canonical dispatch provenance"
            )

    def _write_worker_state(
        self,
        connection: sqlite3.Connection,
        *,
        worker: str,
        lane: str,
        state: str,
        generation: int,
        phase: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO worker_states(worker, lane, state, generation, phase, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker) DO UPDATE SET
                lane=excluded.lane,
                state=excluded.state,
                generation=excluded.generation,
                phase=excluded.phase,
                updated_at=excluded.updated_at
            """,
            (worker, lane, state, generation, phase, now),
        )

    def _terminalize_expired_prepared_locked(
        self,
        connection: sqlite3.Connection,
        slot: sqlite3.Row,
        *,
        observed_at: float,
    ) -> None:
        """Close one proven-unspent prepared slot at the paid activation fence."""

        if slot["state"] != "prepared":
            raise CoordinationError("only a prepared slot is expiry-terminalizable")
        self._validate_slot_task_binding_locked(connection, slot)
        changed = connection.execute(
            """
            UPDATE round_slots
            SET state='terminal', terminal_at=?, outcome=?
            WHERE slot_id=? AND state='prepared'
            """,
            (observed_at, PREPARED_DEADLINE_OUTCOME, slot["slot_id"]),
        ).rowcount
        if changed != 1:
            raise CoordinationError("prepared slot expiry transition lost its CAS")
        self._write_worker_state(
            connection,
            worker=str(slot["worker"]),
            lane=str(slot["lane"]),
            state="phase_expired_not_dispatched",
            generation=int(slot["generation"]),
            phase=str(slot["phase"]),
            now=observed_at,
        )
        self._advance_generation_if_ready(
            connection,
            observed_at=observed_at,
        )

    def _admission(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        resumed: bool,
    ) -> Admission:
        self._validate_slot_task_binding_locked(connection, row)
        expected_directive = coordination_directive(
            lane=str(row["lane"]),
            generation=int(row["generation"]),
            phase=str(row["phase"]),
            review_id=(str(row["review_id"]) if row["review_id"] is not None else None),
            designated_root_entry_id=(
                str(row["designated_root_entry_id"])
                if row["designated_root_entry_id"] is not None
                else None
            ),
        )
        if row["directive"] != expected_directive:
            raise CoordinationError(
                "paid slot directive conflicts with protected identity"
            )
        return Admission(
            slot_id=str(row["slot_id"]),
            worker=str(row["worker"]),
            lane=str(row["lane"]),
            generation=int(row["generation"]),
            phase=str(row["phase"]),
            directive=str(row["directive"]),
            task=str(row["task"]),
            task_sha256=str(row["task_sha256"]),
            task_bytes=int(row["task_bytes"]),
            prompt=str(row["prompt"]) if row["prompt"] is not None else None,
            prompt_sha256=(
                str(row["prompt_sha256"]) if row["prompt_sha256"] is not None else None
            ),
            state=str(row["state"]),
            resumed=resumed,
            phase_deadline_at=float(row["phase_deadline_at"]),
            hard_timeout_seconds=self.config.phase_timeout_seconds,
            review_id=(str(row["review_id"]) if row["review_id"] is not None else None),
            designated_root_entry_id=(
                str(row["designated_root_entry_id"])
                if row["designated_root_entry_id"] is not None
                else None
            ),
        )

    def admit(self, worker: str, *, now: float | None = None) -> Admission | None:
        """CAS-reserve one protected paid slot without creating a paid attempt."""

        lane = self._worker_lane(worker)
        observed_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if now is None:
                observed_at = time.time()
            project = self._state(connection)
            generation = int(project["generation"])
            phase = str(project["phase"])
            phase_expired = observed_at >= float(project["phase_deadline_at"])
            existing = connection.execute(
                """
                SELECT slots.*, state.phase_deadline_at
                FROM round_slots AS slots
                JOIN project_state AS state ON state.singleton=1
                WHERE slots.worker=? AND slots.state IN ('prepared','active','ambiguous')
                """,
                (worker,),
            ).fetchone()
            if existing is not None:
                if phase_expired and existing["state"] == "prepared":
                    self._terminalize_expired_prepared_locked(
                        connection,
                        existing,
                        observed_at=observed_at,
                    )
                    connection.commit()
                    return None
                self._write_worker_state(
                    connection,
                    worker=worker,
                    lane=lane,
                    state="admitted",
                    generation=int(existing["generation"]),
                    phase=str(existing["phase"]),
                    now=observed_at,
                )
                admission = self._admission(connection, existing, resumed=True)
                connection.commit()
                return admission

            candidate_overlay = self._active_candidate(connection)
            review = self._active_review(connection)
            lane_eligible = worker in self.roster.lanes
            if phase == CRITIC_REVIEW_PHASE:
                lane_eligible = (
                    lane == "critic"
                    and review is not None
                    and review["state"] in {"pending", "active"}
                    and review["critic_worker"] == worker
                    and int(review["generation"]) == generation
                )
            if (
                not lane_eligible
                or phase == OWNER_ACTION_REQUIRED_PHASE
                or candidate_overlay is not None
                or phase_expired
            ):
                self._write_worker_state(
                    connection,
                    worker=worker,
                    lane=lane,
                    state=("phase_expired" if phase_expired else "waiting_admission"),
                    generation=generation,
                    phase=phase,
                    now=observed_at,
                )
                connection.commit()
                return None
            already_terminal = connection.execute(
                """
                SELECT 1 FROM round_slots
                WHERE worker=? AND generation=? AND phase=? AND state='terminal'
                """,
                (worker, generation, phase),
            ).fetchone()
            open_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM round_slots
                    WHERE state IN ('prepared','active','ambiguous')
                    """
                ).fetchone()[0]
            )
            if (
                already_terminal is not None
                or open_count >= self.config.max_paid_workers
            ):
                self._write_worker_state(
                    connection,
                    worker=worker,
                    lane=lane,
                    state="waiting_admission",
                    generation=generation,
                    phase=phase,
                    now=observed_at,
                )
                connection.commit()
                return None

            slot_review = review if phase == CRITIC_REVIEW_PHASE else None
            review_id = (
                str(slot_review["review_id"]) if slot_review is not None else None
            )
            designated_root_entry_id = (
                str(slot_review["root_entry_id"]) if slot_review is not None else None
            )
            task_assignment = connection.execute(
                """
                SELECT * FROM generation_tasks
                WHERE worker=? AND generation=?
                """,
                (worker, generation),
            ).fetchone()
            if task_assignment is None:
                raise CoordinationError(
                    "current generation has no durable task assignment for worker"
                )
            task_digest, task_bytes = self._validate_generation_task_row(
                task_assignment
            )
            if task_assignment["lane"] != lane:
                raise CoordinationError(
                    "current generation task lane conflicts with admission"
                )
            if task_assignment["frozen_at"] is None:
                frozen = connection.execute(
                    """
                    UPDATE generation_tasks SET frozen_at=?
                    WHERE worker=? AND generation=? AND frozen_at IS NULL
                    """,
                    (observed_at, worker, generation),
                ).rowcount
                if frozen != 1:
                    raise CoordinationError(
                        "generation task admission freeze lost its CAS"
                    )
            directive = coordination_directive(
                lane=lane,
                generation=generation,
                phase=phase,
                review_id=review_id,
                designated_root_entry_id=designated_root_entry_id,
            )
            slot_id = f"slot_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO round_slots(
                    slot_id, worker, lane, generation, phase, state, directive,
                    task, task_sha256, task_bytes, prompt_task_sha256,
                    legacy_task_binding,
                    prompt, prompt_sha256, created_at, activated_at,
                    terminal_at, outcome, review_id, designated_root_entry_id
                ) VALUES(
                    ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, NULL, 0,
                    NULL, NULL, ?, NULL, NULL, NULL, ?, ?
                )
                """,
                (
                    slot_id,
                    worker,
                    lane,
                    generation,
                    phase,
                    directive,
                    task_assignment["task"],
                    task_digest,
                    task_bytes,
                    observed_at,
                    review_id,
                    designated_root_entry_id,
                ),
            )
            if slot_review is not None:
                changed = connection.execute(
                    """
                    UPDATE obstacle_reviews
                    SET state='active', critic_slot_id=?, activated_at=?
                    WHERE review_id=? AND state='pending' AND critic_slot_id IS NULL
                    """,
                    (slot_id, observed_at, review_id),
                ).rowcount
                if changed != 1:
                    raise CoordinationError("critic review admission lost its CAS")
            self._write_worker_state(
                connection,
                worker=worker,
                lane=lane,
                state="admitted",
                generation=generation,
                phase=phase,
                now=observed_at,
            )
            row = connection.execute(
                """
                SELECT slots.*, state.phase_deadline_at
                FROM round_slots AS slots
                JOIN project_state AS state ON state.singleton=1
                WHERE slots.slot_id=?
                """,
                (slot_id,),
            ).fetchone()
            if row is None:
                raise CoordinationError("admitted round slot disappeared")
            result = self._admission(connection, row, resumed=False)
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def pin_prompt(self, slot_id: str, prompt: str) -> Admission:
        """CAS-pin the exact generated kickoff before app-server intent prepare."""

        _validate_identifier(slot_id, "slot_id")
        if not isinstance(prompt, str) or not prompt:
            raise CoordinationError("pinned prompt must be non-empty")
        encoded = prompt.encode("utf-8")
        if len(encoded) > MAX_PINNED_PROMPT_BYTES:
            raise CoordinationError("pinned prompt exceeds its hard limit")
        digest = hashlib.sha256(encoded).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM round_slots WHERE slot_id=?", (slot_id,)
            ).fetchone()
            if row is None or row["state"] not in {"prepared", "active", "ambiguous"}:
                raise CoordinationError("round slot is not prompt-pinnable")
            self._validate_slot_task_binding_locked(connection, row)
            prompt_lines = set(prompt.splitlines())
            if any(
                marker not in prompt_lines
                for marker in self._prompt_binding_markers(row)
            ):
                raise CoordinationError(
                    "pinned prompt does not bind exact slot/generation/task identity"
                )
            if row["prompt"] is None:
                connection.execute(
                    """
                    UPDATE round_slots
                    SET prompt=?, prompt_sha256=?, prompt_task_sha256=?
                    WHERE slot_id=? AND prompt IS NULL
                    """,
                    (prompt, digest, row["task_sha256"], slot_id),
                )
            elif row["prompt"] != prompt or row["prompt_sha256"] != digest:
                # Existing pins are immutable. Return them rather than allowing
                # fresh task/config bytes to rewrite an ambiguous paid intent.
                pass
            joined = connection.execute(
                """
                SELECT slots.*, state.phase_deadline_at
                FROM round_slots AS slots
                JOIN project_state AS state ON state.singleton=1
                WHERE slots.slot_id=?
                """,
                (slot_id,),
            ).fetchone()
            if joined is None:
                raise CoordinationError("round slot disappeared while pinning prompt")
            result = self._admission(
                connection,
                joined,
                resumed=row["prompt"] is not None,
            )
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def activate(self, slot_id: str, *, now: float | None = None) -> Admission:
        """Activate a pinned slot immediately before attempts/log/paid dispatch."""

        _validate_identifier(slot_id, "slot_id")
        activated_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM round_slots WHERE slot_id=?", (slot_id,)
            ).fetchone()
            if row is None or row["state"] not in {"prepared", "active", "ambiguous"}:
                raise CoordinationError("round slot is not activatable")
            self._validate_slot_task_binding_locked(connection, row)
            if row["prompt"] is None or row["prompt_sha256"] is None:
                raise CoordinationError("round slot has no pinned prompt")
            if row["state"] == "prepared":
                project = self._state(connection)
                if activated_at >= float(project["phase_deadline_at"]):
                    self._terminalize_expired_prepared_locked(
                        connection,
                        row,
                        observed_at=activated_at,
                    )
                    connection.commit()
                    raise CoordinationError(
                        "phase deadline exceeded before paid slot activation"
                    )
                connection.execute(
                    """
                    UPDATE round_slots SET state='active', activated_at=?
                    WHERE slot_id=? AND state='prepared'
                    """,
                    (activated_at, slot_id),
                )
            joined = connection.execute(
                """
                SELECT slots.*, state.phase_deadline_at
                FROM round_slots AS slots
                JOIN project_state AS state ON state.singleton=1
                WHERE slots.slot_id=?
                """,
                (slot_id,),
            ).fetchone()
            if joined is None:
                raise CoordinationError("round slot disappeared during activation")
            result = self._admission(
                connection,
                joined,
                resumed=row["state"] != "prepared",
            )
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def mark_ambiguous(self, slot_id: str) -> None:
        _validate_identifier(slot_id, "slot_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            slot = connection.execute(
                "SELECT * FROM round_slots WHERE slot_id=?", (slot_id,)
            ).fetchone()
            if slot is None or slot["state"] not in {
                "prepared",
                "active",
                "ambiguous",
            }:
                raise CoordinationError("round slot is not ambiguity-preservable")
            self._validate_slot_task_binding_locked(connection, slot)
            changed = connection.execute(
                """
                UPDATE round_slots SET state='ambiguous'
                WHERE slot_id=? AND state IN ('prepared','active','ambiguous')
                """,
                (slot_id,),
            ).rowcount
            if changed != 1:
                raise CoordinationError("round slot is not ambiguity-preservable")
            self._write_worker_state(
                connection,
                worker=str(slot["worker"]),
                lane=str(slot["lane"]),
                state="ambiguous",
                generation=int(slot["generation"]),
                phase=str(slot["phase"]),
                now=time.time(),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _advance_generation_if_ready(
        self,
        connection: sqlite3.Connection,
        *,
        observed_at: float,
    ) -> bool:
        project = self._state(connection)
        generation = int(project["generation"])
        phase = str(project["phase"])
        if (
            phase == OWNER_ACTION_REQUIRED_PHASE
            or self._active_candidate(connection) is not None
        ):
            return False
        completed_lanes = {
            str(row["lane"])
            for row in connection.execute(
                """
                SELECT lane FROM round_slots
                WHERE generation=? AND phase=? AND state='terminal'
                """,
                (generation, phase),
            ).fetchall()
        }
        required = (
            {"critic"}
            if phase == CRITIC_REVIEW_PHASE
            else set(required_lanes(self.roster))
        )
        if not required.issubset(completed_lanes):
            return False
        review = self._active_review(connection)
        if phase == REASONING_PHASE and review is not None:
            if review["state"] != "pending" or int(review["generation"]) != generation:
                raise CoordinationError(
                    "reasoning phase has a non-pending obstacle review"
                )
            changed = connection.execute(
                """
                UPDATE project_state
                SET phase=?, phase_started_at=?, phase_deadline_at=?, updated_at=?
                WHERE singleton=1 AND generation=? AND phase=?
                  AND active_review_id=? AND recommendation_id IS NULL
                """,
                (
                    CRITIC_REVIEW_PHASE,
                    observed_at,
                    observed_at + self.config.phase_timeout_seconds,
                    observed_at,
                    generation,
                    REASONING_PHASE,
                    review["review_id"],
                ),
            ).rowcount
            if changed != 1:
                raise CoordinationError("critic review phase transition lost its CAS")
            return True
        if phase == CRITIC_REVIEW_PHASE:
            if review is None or review["state"] != "active":
                raise CoordinationError(
                    "terminal critic review has no exact active review"
                )
            changed_review = connection.execute(
                """
                UPDATE obstacle_reviews
                SET state='not_confirmed', terminal_at=?
                WHERE review_id=? AND state='active'
                  AND confirmation_entry_id IS NULL
                """,
                (observed_at, review["review_id"]),
            ).rowcount
            if changed_review != 1:
                raise CoordinationError(
                    "unconfirmed critic review terminal transition lost its CAS"
                )
        self._copy_forward_generation_tasks_locked(
            connection,
            generation=generation,
            staged_at=observed_at,
        )
        changed = connection.execute(
            """
            UPDATE project_state
            SET generation=?, phase=?, phase_started_at=?, active_review_id=NULL,
                phase_deadline_at=?, updated_at=?
            WHERE singleton=1 AND generation=? AND phase=?
              AND recommendation_id IS NULL
            """,
            (
                generation + 1,
                REASONING_PHASE,
                observed_at,
                observed_at + self.config.phase_timeout_seconds,
                observed_at,
                generation,
                phase,
            ),
        ).rowcount
        if changed != 1:
            raise CoordinationError("generation advance lost its CAS")
        return True

    def complete(
        self, slot_id: str, *, outcome: str, now: float | None = None
    ) -> dict[str, Any]:
        """Release only a terminal slot and advance after every lane is safe."""

        _validate_identifier(slot_id, "slot_id")
        if not isinstance(outcome, str) or not outcome:
            raise CoordinationError("round outcome must be non-empty")
        if len(outcome.encode("utf-8")) > MAX_OUTCOME_BYTES:
            raise CoordinationError("round outcome exceeds its hard limit")
        terminal_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            slot = connection.execute(
                "SELECT * FROM round_slots WHERE slot_id=?", (slot_id,)
            ).fetchone()
            if slot is None:
                raise CoordinationError("round slot does not exist")
            self._validate_slot_task_binding_locked(connection, slot)
            if slot["state"] == "terminal":
                if slot["outcome"] != outcome:
                    raise CoordinationError("round terminal outcome conflicts")
                # Exact replay is a read-only lookup. In particular, do not
                # overwrite this worker's state after a later generation has
                # already begun.
                connection.commit()
                return self.project_status()
            if slot["state"] not in {"prepared", "active", "ambiguous"}:
                raise CoordinationError("round slot has an invalid terminal state")
            connection.execute(
                """
                UPDATE round_slots
                SET state='terminal', terminal_at=?, outcome=?
                WHERE slot_id=?
                """,
                (terminal_at, outcome, slot_id),
            )
            if slot["phase"] == CRITIC_REVIEW_PHASE and slot["review_id"] is not None:
                changed_review = connection.execute(
                    """
                    UPDATE obstacle_reviews SET terminal_at=COALESCE(terminal_at, ?)
                    WHERE review_id=? AND critic_slot_id=?
                      AND state IN ('active','confirmed')
                    """,
                    (terminal_at, slot["review_id"], slot_id),
                ).rowcount
                if changed_review != 1:
                    raise CoordinationError(
                        "critic review terminal receipt conflicts with review identity"
                    )
            self._write_worker_state(
                connection,
                worker=str(slot["worker"]),
                lane=str(slot["lane"]),
                state="terminal",
                generation=int(slot["generation"]),
                phase=str(slot["phase"]),
                now=terminal_at,
            )
            self._advance_generation_if_ready(
                connection,
                observed_at=terminal_at,
            )
            connection.commit()
            return self.project_status()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _open_recommendation_projection_locked(
        self,
        connection: sqlite3.Connection,
        recommendation_id: str,
    ) -> dict[str, Any]:
        recommendation = connection.execute(
            "SELECT * FROM advisor_recommendations WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        if recommendation is None:
            raise CoordinationError("recommendation does not exist")
        resolved = connection.execute(
            "SELECT 1 FROM recommendation_resolutions WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        if resolved is not None:
            raise CoordinationError("recommendation is already resolved")
        project = self._state(connection)
        generation = int(recommendation["generation"])
        if (
            int(project["generation"]) != generation
            or project["phase"] != OWNER_ACTION_REQUIRED_PHASE
            or project["recommendation_id"] != recommendation_id
            or recommendation["state"] != OWNER_ACTION_REQUIRED_PHASE
        ):
            raise CoordinationError(
                "recommendation is not the current open owner-action requirement"
            )
        if (
            bool(recommendation["browser_dispatch_authorized"])
            or recommendation["advisor_request_id"] is not None
        ):
            raise CoordinationError(
                "recommendation browser authority fields are not fail-closed"
            )
        review_id = recommendation["review_id"]
        if review_id is None or project["active_review_id"] != review_id:
            raise CoordinationError(
                "recommendation is not bound to one active durable review"
            )
        review = connection.execute(
            "SELECT * FROM obstacle_reviews WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if (
            review is None
            or review["state"] != "confirmed"
            or int(review["generation"]) != generation
            or review["root_entry_id"] != recommendation["root_entry_id"]
            or review["confirmation_entry_id"] != recommendation["critic_entry_id"]
        ):
            raise CoordinationError(
                "recommendation durable review binding is not ready"
            )
        open_slots = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM round_slots
                WHERE generation=? AND state!='terminal'
                """,
                (generation,),
            ).fetchone()[0]
        )
        if open_slots:
            raise CoordinationError(
                "recommendation generation has nonterminal paid slots"
            )
        if self._active_candidate(connection) is not None:
            raise CoordinationError(
                "recommendation generation has an active candidate overlay"
            )
        return {
            "recommendation_id": recommendation_id,
            "generation": generation,
            "state": str(recommendation["state"]),
            "review_id": str(review_id),
            "root_entry_id": str(recommendation["root_entry_id"]),
            "critic_entry_id": str(recommendation["critic_entry_id"]),
            "browser_dispatch_authorized": False,
            "advisor_request_id": None,
            "ready": True,
        }

    def project_status(self, worker: str | None = None) -> dict[str, Any]:
        connection = self._connect()
        try:
            project = self._state(connection)
            observed_at = time.time()
            phase_deadline_at = float(project["phase_deadline_at"])
            phase_deadline_exceeded = observed_at >= phase_deadline_at
            paid_active = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM round_slots
                    WHERE state IN ('active','ambiguous')
                    """
                ).fetchone()[0]
            )
            reserved = int(
                connection.execute(
                    "SELECT COUNT(*) FROM round_slots WHERE state='prepared'"
                ).fetchone()[0]
            )
            waiting = int(
                connection.execute(
                    "SELECT COUNT(*) FROM worker_states WHERE state='waiting_admission'"
                ).fetchone()[0]
            )
            recommendation = None
            recommendation_id = project["recommendation_id"]
            if recommendation_id is not None:
                row = connection.execute(
                    """
                    SELECT recommendation_id, generation, state,
                           review_id, root_entry_id, critic_entry_id,
                           browser_dispatch_authorized, advisor_request_id
                    FROM advisor_recommendations WHERE recommendation_id=?
                    """,
                    (recommendation_id,),
                ).fetchone()
                if row is not None:
                    recommendation = {
                        "recommendation_id": row["recommendation_id"],
                        "generation": row["generation"],
                        "state": row["state"],
                        "review_id": row["review_id"],
                        "root_entry_id": row["root_entry_id"],
                        "critic_entry_id": row["critic_entry_id"],
                        "browser_dispatch_authorized": bool(
                            row["browser_dispatch_authorized"]
                        ),
                        "advisor_request_id": row["advisor_request_id"],
                    }
            candidate_row = self._active_candidate(connection)
            candidate = (
                self._candidate_projection(candidate_row)
                if candidate_row is not None
                else None
            )
            review_row = self._active_review(connection)
            review = (
                self._review_projection(review_row) if review_row is not None else None
            )
            resolution_row = connection.execute(
                """
                SELECT * FROM recommendation_resolutions
                ORDER BY created_at DESC, resolution_id DESC LIMIT 1
                """
            ).fetchone()
            resolution = (
                self._resolution_projection(resolution_row)
                if resolution_row is not None
                else None
            )
            recommendation_present = recommendation is not None
            recommendation_ready = False
            if recommendation_present:
                try:
                    self._open_recommendation_projection_locked(
                        connection,
                        str(recommendation_id),
                    )
                    recommendation_ready = True
                except CoordinationError:
                    recommendation_ready = False
            task_staging_generation = (
                int(project["generation"]) + 1
                if project["phase"] == OWNER_ACTION_REQUIRED_PHASE
                else int(project["generation"])
            )
            task_staging = self._task_staging_projection_locked(
                connection,
                generation=task_staging_generation,
            )
            fail_stop_reason = None
            if (
                project["phase"] == OWNER_ACTION_REQUIRED_PHASE
                and not task_staging["ready"]
            ):
                fail_stop_reason = "durable_task_assignment_required"
            elif project["phase"] == OWNER_ACTION_REQUIRED_PHASE:
                fail_stop_reason = "owner_recommendation_resolution_required"
            elif phase_deadline_exceeded:
                fail_stop_reason = (
                    "phase_deadline_exceeded_paid_recovery_required"
                    if paid_active
                    else "phase_deadline_exceeded_no_new_paid_admission"
                )
            elif not task_staging["ready"]:
                fail_stop_reason = "durable_task_assignment_required"
            result: dict[str, Any] = {
                "mode": project["mode"],
                "generation": int(project["generation"]),
                "phase": project["phase"],
                "root_worker": project["root_worker"],
                "critic_worker": project["critic_worker"],
                "explorer_workers": list(self.roster.explorers),
                "paid_active": paid_active,
                "reserved_admission": reserved,
                "waiting_admission": waiting,
                "phase_deadline_at": phase_deadline_at,
                "phase_deadline_exceeded": phase_deadline_exceeded,
                "advisor_reachable": self.roster.critic is not None,
                "advisor_recommendation_present": recommendation_present,
                "advisor_recommendation_ready": recommendation_ready,
                "fail_stop_reason": fail_stop_reason,
                "review": review,
                "recommendation": recommendation,
                "resolution": resolution,
                "candidate": candidate,
                "task_staging": task_staging,
            }
            if worker is not None:
                lane = self._worker_lane(worker)
                state = connection.execute(
                    "SELECT state FROM worker_states WHERE worker=?", (worker,)
                ).fetchone()
                result["worker"] = worker
                result["lane"] = lane
                result["admission_state"] = (
                    state["state"] if state is not None else "eligible"
                )
            return result
        finally:
            connection.close()

    def evidence_entry(self, entry_id: str) -> dict[str, Any] | None:
        """Return one content-free evidence identity for diagnostics/tests."""

        entry_id = _validate_identifier(entry_id, "entry_id")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT entry_id, generation, slot_id, worker, lane, kind,
                       confirms_entry_id
                FROM evidence_entries WHERE entry_id=?
                """,
                (entry_id,),
            ).fetchone()
            if row is None:
                return None
            return {key: row[key] for key in row.keys()}
        finally:
            connection.close()

    def record_root_evidence(
        self,
        worker: str,
        kind: str,
        *,
        entry_id: str,
        slot_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if self._worker_lane(worker) != "root" or kind not in {"obstacle", "dead_end"}:
            raise CoordinationError("only root may record obstacle/dead_end evidence")
        evidence_id = _validate_identifier(entry_id, "entry_id")
        created_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            slot = self._canonical_paid_slot(
                connection,
                worker,
                slot_id=slot_id,
            )
            self._insert_evidence(
                connection,
                entry_id=evidence_id,
                generation=int(slot["generation"]),
                slot_id=str(slot["slot_id"]),
                worker=worker,
                lane="root",
                kind=kind,
                confirms_entry_id=None,
                created_at=created_at,
            )
            review_id = self._ensure_review_for_root_locked(
                connection,
                root_entry_id=evidence_id,
                generation=int(slot["generation"]),
                root_slot_id=str(slot["slot_id"]),
                created_at=created_at,
            )
            connection.commit()
            return {
                "entry_id": evidence_id,
                "generation": int(slot["generation"]),
                "slot_id": str(slot["slot_id"]),
                "worker": worker,
                "lane": "root",
                "kind": kind,
                "confirms_entry_id": None,
                "review_id": review_id,
                "recommendation_id": None,
            }
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def confirm_root_evidence(
        self,
        worker: str,
        confirms_entry_id: str,
        *,
        entry_id: str,
        slot_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if self._worker_lane(worker) != "critic":
            raise CoordinationError("only critic may confirm root evidence")
        root_entry_id = _validate_identifier(confirms_entry_id, "confirms_entry_id")
        created_at = time.time() if now is None else float(now)
        critic_entry_id = _validate_identifier(entry_id, "entry_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            slot = self._canonical_paid_slot(
                connection,
                worker,
                slot_id=slot_id,
            )
            if (
                slot["phase"] != CRITIC_REVIEW_PHASE
                or slot["review_id"] is None
                or slot["designated_root_entry_id"] != root_entry_id
            ):
                raise CoordinationError(
                    "critic confirmation requires its exact designated review slot"
                )
            review = connection.execute(
                "SELECT * FROM obstacle_reviews WHERE review_id=?",
                (slot["review_id"],),
            ).fetchone()
            if (
                review is None
                or review["critic_slot_id"] != slot["slot_id"]
                or review["root_entry_id"] != root_entry_id
                or review["state"] not in {"active", "confirmed"}
            ):
                raise CoordinationError(
                    "critic confirmation conflicts with the durable designated review"
                )
            root = connection.execute(
                """
                SELECT 1 FROM evidence_entries
                WHERE entry_id=? AND generation=? AND lane='root'
                  AND kind IN ('obstacle','dead_end')
                """,
                (root_entry_id, int(slot["generation"])),
            ).fetchone()
            if root is None:
                raise CoordinationError(
                    "critic confirmation must name exact current-generation root evidence"
                )
            self._insert_evidence(
                connection,
                entry_id=critic_entry_id,
                generation=int(slot["generation"]),
                slot_id=str(slot["slot_id"]),
                worker=worker,
                lane="critic",
                kind="critic_confirmation",
                confirms_entry_id=root_entry_id,
                created_at=created_at,
            )
            if review["state"] == "active":
                changed = connection.execute(
                    """
                    UPDATE obstacle_reviews
                    SET state='confirmed', confirmation_entry_id=?
                    WHERE review_id=? AND state='active'
                      AND confirmation_entry_id IS NULL
                    """,
                    (critic_entry_id, review["review_id"]),
                ).rowcount
                if changed != 1:
                    raise CoordinationError("critic review confirmation lost its CAS")
            elif review["confirmation_entry_id"] != critic_entry_id:
                raise CoordinationError(
                    "critic review already has another confirmation"
                )
            recommendation_id = self._ensure_recommendation(
                connection,
                review_id=str(review["review_id"]),
                root_entry_id=root_entry_id,
                critic_entry_id=critic_entry_id,
                generation=int(slot["generation"]),
                created_at=created_at,
            )
            connection.commit()
            result = {
                "entry_id": critic_entry_id,
                "generation": int(slot["generation"]),
                "slot_id": str(slot["slot_id"]),
                "worker": worker,
                "lane": "critic",
                "kind": "critic_confirmation",
                "confirms_entry_id": root_entry_id,
                "recommendation_id": recommendation_id,
            }
            if recommendation_id is not None:
                result.update(
                    {
                        "state": "owner_action_required",
                        "browser_dispatch_authorized": False,
                        "advisor_request_id": None,
                    }
                )
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def resolve_recommendation(
        self,
        recommendation_id: str,
        *,
        resolution: str,
        owner_acknowledgement: str,
        master_guidance_entry_id: str | None = None,
        master_guidance_record_sha256: str | None = None,
        browser_request_id: str | None = None,
        browser_receipt_sha256: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Owner-only exact-CAS resume after a terminal reviewed recommendation."""

        resolution_id = recommendation_resolution_id(
            recommendation_id=recommendation_id,
            resolution=resolution,
            owner_acknowledgement=owner_acknowledgement,
            master_guidance_entry_id=master_guidance_entry_id,
            master_guidance_record_sha256=master_guidance_record_sha256,
            browser_request_id=browser_request_id,
            browser_receipt_sha256=browser_receipt_sha256,
        )
        resolved_at = time.time() if now is None else float(now)
        expected = (
            resolution_id,
            recommendation_id,
            resolution,
            owner_acknowledgement,
            master_guidance_entry_id,
            master_guidance_record_sha256,
            browser_request_id,
            browser_receipt_sha256,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if now is None:
                resolved_at = time.time()
            prior = connection.execute(
                "SELECT * FROM recommendation_resolutions WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
            if prior is not None:
                observed = tuple(
                    prior[key]
                    for key in (
                        "resolution_id",
                        "recommendation_id",
                        "resolution",
                        "owner_acknowledgement",
                        "master_guidance_entry_id",
                        "master_guidance_record_sha256",
                        "browser_request_id",
                        "browser_receipt_sha256",
                    )
                )
                if observed != expected:
                    raise CoordinationError(
                        "recommendation already has a conflicting owner resolution"
                    )
                result = self._resolution_projection(prior)
                connection.commit()
                return result

            recommendation = connection.execute(
                "SELECT * FROM advisor_recommendations WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
            if recommendation is None:
                raise CoordinationError("recommendation does not exist")
            project = self._state(connection)
            generation = int(recommendation["generation"])
            if (
                int(project["generation"]) != generation
                or project["phase"] != OWNER_ACTION_REQUIRED_PHASE
                or project["recommendation_id"] != recommendation_id
            ):
                raise CoordinationError(
                    "recommendation is not the current owner-action requirement"
                )
            open_slots = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM round_slots
                    WHERE generation=? AND state!='terminal'
                    """,
                    (generation,),
                ).fetchone()[0]
            )
            if open_slots:
                raise CoordinationError(
                    "all recommendation-generation slots must be terminal before resume"
                )
            if self._active_candidate(connection) is not None:
                raise CoordinationError(
                    "candidate overlay must be terminal before recommendation resume"
                )
            review_id = recommendation["review_id"]
            review = None
            if review_id is not None:
                review = connection.execute(
                    "SELECT * FROM obstacle_reviews WHERE review_id=?",
                    (review_id,),
                ).fetchone()
                if (
                    review is None
                    or review["state"] != "confirmed"
                    or review["confirmation_entry_id"]
                    != recommendation["critic_entry_id"]
                    or review["root_entry_id"] != recommendation["root_entry_id"]
                    or int(review["generation"]) != generation
                    or project["active_review_id"] != review_id
                ):
                    raise CoordinationError(
                        "recommendation is not bound to one confirmed durable review"
                    )
            elif project["active_review_id"] is not None:
                raise CoordinationError(
                    "legacy recommendation has an inconsistent active review pointer"
                )

            self._freeze_required_generation_tasks_locked(
                connection,
                generation=generation + 1,
                frozen_at=resolved_at,
            )
            connection.execute(
                """
                INSERT INTO recommendation_resolutions(
                    resolution_id, recommendation_id, generation, resolution,
                    owner_acknowledgement, master_guidance_entry_id,
                    master_guidance_record_sha256, browser_request_id,
                    browser_receipt_sha256, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_id,
                    recommendation_id,
                    generation,
                    resolution,
                    owner_acknowledgement,
                    master_guidance_entry_id,
                    master_guidance_record_sha256,
                    browser_request_id,
                    browser_receipt_sha256,
                    resolved_at,
                ),
            )
            if review is not None:
                changed_review = connection.execute(
                    """
                    UPDATE obstacle_reviews SET state='resolved'
                    WHERE review_id=? AND state='confirmed'
                      AND confirmation_entry_id=?
                    """,
                    (review_id, recommendation["critic_entry_id"]),
                ).rowcount
                if changed_review != 1:
                    raise CoordinationError(
                        "recommendation review resolution lost its CAS"
                    )
            changed_project = connection.execute(
                """
                UPDATE project_state
                SET generation=?, phase=?, phase_started_at=?, phase_deadline_at=?,
                    recommendation_id=NULL, active_review_id=NULL, updated_at=?
                WHERE singleton=1 AND generation=? AND phase=?
                  AND recommendation_id=? AND active_review_id IS ?
                """,
                (
                    generation + 1,
                    REASONING_PHASE,
                    resolved_at,
                    resolved_at + self.config.phase_timeout_seconds,
                    resolved_at,
                    generation,
                    OWNER_ACTION_REQUIRED_PHASE,
                    recommendation_id,
                    review_id,
                ),
            ).rowcount
            if changed_project != 1:
                raise CoordinationError(
                    "recommendation owner resolution lost its project CAS"
                )
            row = connection.execute(
                "SELECT * FROM recommendation_resolutions WHERE resolution_id=?",
                (resolution_id,),
            ).fetchone()
            if row is None:
                raise CoordinationError("recommendation resolution disappeared")
            result = self._resolution_projection(row)
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def recommendation_resolution(
        self, recommendation_id: str
    ) -> dict[str, Any] | None:
        """Return the content-free owner resolution for exact CLI replay."""

        recommendation_id = _validate_identifier(recommendation_id, "recommendation_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM recommendation_resolutions WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
            return self._resolution_projection(row) if row is not None else None
        finally:
            connection.close()

    def validate_open_recommendation(self, recommendation_id: str) -> dict[str, Any]:
        """Fail closed unless one exact recommendation is ready for owner review."""

        recommendation_id = _validate_identifier(recommendation_id, "recommendation_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            result = self._open_recommendation_projection_locked(
                connection,
                recommendation_id,
            )
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        entry_id: str,
        generation: int,
        slot_id: str,
        worker: str,
        lane: str,
        kind: str,
        confirms_entry_id: str | None,
        created_at: float,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM evidence_entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        expected = (
            generation,
            slot_id,
            worker,
            lane,
            kind,
            confirms_entry_id,
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO evidence_entries(
                    entry_id, generation, slot_id, worker, lane, kind,
                    confirms_entry_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, *expected, created_at),
            )
            return
        observed = tuple(
            existing[key]
            for key in (
                "generation",
                "slot_id",
                "worker",
                "lane",
                "kind",
                "confirms_entry_id",
            )
        )
        if observed != expected:
            raise CoordinationError("evidence entry id conflicts with prior state")

    def _ensure_recommendation(
        self,
        connection: sqlite3.Connection,
        *,
        review_id: str,
        root_entry_id: str,
        critic_entry_id: str,
        generation: int,
        created_at: float,
    ) -> str | None:
        review = connection.execute(
            "SELECT * FROM obstacle_reviews WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if (
            review is None
            or int(review["generation"]) != generation
            or review["root_entry_id"] != root_entry_id
            or review["confirmation_entry_id"] != critic_entry_id
            or review["state"] != "confirmed"
        ):
            return None
        root = connection.execute(
            """
            SELECT 1 FROM evidence_entries
            WHERE entry_id=? AND generation=? AND lane='root'
              AND kind IN ('obstacle','dead_end')
            """,
            (root_entry_id, generation),
        ).fetchone()
        critic = connection.execute(
            """
            SELECT 1 FROM evidence_entries
            WHERE entry_id=? AND generation=? AND lane='critic'
              AND kind='critic_confirmation' AND confirms_entry_id=?
            """,
            (critic_entry_id, generation, root_entry_id),
        ).fetchone()
        if root is None or critic is None:
            return None
        project = self._state(connection)
        if (
            int(project["generation"]) != generation
            or project["active_review_id"] != review_id
            or project["phase"]
            not in {
                CRITIC_REVIEW_PHASE,
                OWNER_ACTION_REQUIRED_PHASE,
            }
        ):
            return None
        if project["recommendation_id"] is not None:
            existing = connection.execute(
                """
                SELECT review_id, root_entry_id, critic_entry_id
                FROM advisor_recommendations WHERE recommendation_id=?
                """,
                (project["recommendation_id"],),
            ).fetchone()
            if (
                existing is not None
                and existing["review_id"] == review_id
                and existing["root_entry_id"] == root_entry_id
                and existing["critic_entry_id"] == critic_entry_id
            ):
                return str(project["recommendation_id"])
            return None
        recommendation_id = f"recommendation_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO advisor_recommendations(
                recommendation_id, generation, state, review_id, root_entry_id,
                critic_entry_id, browser_dispatch_authorized,
                advisor_request_id, created_at
            ) VALUES(?, ?, 'owner_action_required', ?, ?, ?, 0, NULL, ?)
            """,
            (
                recommendation_id,
                generation,
                review_id,
                root_entry_id,
                critic_entry_id,
                created_at,
            ),
        )
        changed = connection.execute(
            """
            UPDATE project_state
            SET phase=?, recommendation_id=?, updated_at=?
            WHERE singleton=1 AND generation=? AND phase=?
              AND active_review_id=? AND recommendation_id IS NULL
            """,
            (
                OWNER_ACTION_REQUIRED_PHASE,
                recommendation_id,
                created_at,
                generation,
                CRITIC_REVIEW_PHASE,
                review_id,
            ),
        ).rowcount
        if changed != 1:
            raise CoordinationError("recommendation phase transition lost its CAS")
        return recommendation_id

    @staticmethod
    def _matching_memory_evidence(
        entry: Mapping[str, Any], slot: sqlite3.Row
    ) -> tuple[str, str, str | None] | None:
        if not isinstance(entry, Mapping):
            return None
        kind = entry.get("kind")
        if kind not in {"obstacle", "dead_end"}:
            return None
        entry_id = entry.get("id")
        author = entry.get("author")
        links = entry.get("links")
        if not isinstance(entry_id, str) or author != slot["worker"]:
            return None
        if not isinstance(links, Mapping):
            return None
        provenance = links.get("coordination")
        expected = {
            "slot_id": str(slot["slot_id"]),
            "generation": int(slot["generation"]),
            "lane": str(slot["lane"]),
        }
        if (
            not isinstance(provenance, Mapping)
            or set(provenance) != set(expected)
            or not isinstance(provenance.get("slot_id"), str)
            or provenance.get("slot_id") != expected["slot_id"]
            or isinstance(provenance.get("generation"), bool)
            or not isinstance(provenance.get("generation"), int)
            or provenance.get("generation") != expected["generation"]
            or not isinstance(provenance.get("lane"), str)
            or provenance.get("lane") != expected["lane"]
        ):
            return None
        try:
            entry_id = _validate_identifier(entry_id, "entry_id")
        except CoordinationError:
            return None
        if slot["lane"] == "root":
            return entry_id, str(kind), None
        if slot["lane"] != "critic":
            return None
        confirms_entry_id = links.get("confirms_entry_id")
        try:
            confirms_entry_id = _validate_identifier(
                confirms_entry_id, "confirms_entry_id"
            )
        except CoordinationError:
            return None
        return entry_id, "critic_confirmation", confirms_entry_id

    def reconcile_terminal_memory_entries(
        self,
        slot_id: str,
        worker: str,
        entries: list[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Idempotently close GM->SQLite gaps at a proven terminal boundary."""

        if not isinstance(entries, list) or len(entries) > MAX_RECONCILE_ENTRIES:
            raise CoordinationError(
                "memory reconciliation batch exceeds its hard limit"
            )
        reconciled_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            slot = self._terminal_reconciliation_slot(
                connection,
                worker,
                slot_id=slot_id,
            )
            identities: dict[str, tuple[str, str, str | None]] = {}
            for entry in entries:
                identity = self._matching_memory_evidence(entry, slot)
                if identity is None:
                    continue
                prior = identities.get(identity[0])
                if prior is not None and prior != identity:
                    raise CoordinationError(
                        "terminal memory entry id has conflicting evidence identity"
                    )
                identities[identity[0]] = identity

            if slot["lane"] == "root" and len(identities) > 1:
                raise CoordinationError(
                    "root slot has multiple obstacle entries; review designation is ambiguous"
                )
            if slot["lane"] == "critic" and identities:
                if slot["phase"] != CRITIC_REVIEW_PHASE:
                    raise CoordinationError(
                        "generic critic confirmation cannot create a recommendation"
                    )
                if len(identities) > 1:
                    raise CoordinationError(
                        "critic review has multiple confirmations; designation is ambiguous"
                    )
            if slot["lane"] not in {"root", "critic"} and identities:
                raise CoordinationError(
                    "explorer memory cannot enter obstacle-review evidence"
                )

            accepted: list[str] = []
            review_id: str | None = None
            recommendation_id: str | None = None
            for identity in identities.values():
                entry_id, kind, confirms_entry_id = identity
                if slot["lane"] == "critic" and (
                    slot["review_id"] is None
                    or slot["designated_root_entry_id"] != confirms_entry_id
                ):
                    raise CoordinationError(
                        "critic confirmation does not match its designated root entry"
                    )
                self._insert_evidence(
                    connection,
                    entry_id=entry_id,
                    generation=int(slot["generation"]),
                    slot_id=str(slot["slot_id"]),
                    worker=worker,
                    lane=str(slot["lane"]),
                    kind=kind,
                    confirms_entry_id=confirms_entry_id,
                    created_at=reconciled_at,
                )
                accepted.append(entry_id)
                if slot["lane"] == "root":
                    review_id = self._ensure_review_for_root_locked(
                        connection,
                        root_entry_id=entry_id,
                        root_slot_id=str(slot["slot_id"]),
                        generation=int(slot["generation"]),
                        created_at=reconciled_at,
                    )
                elif slot["lane"] == "critic":
                    review = connection.execute(
                        "SELECT * FROM obstacle_reviews WHERE review_id=?",
                        (slot["review_id"],),
                    ).fetchone()
                    if (
                        review is None
                        or review["critic_slot_id"] != slot["slot_id"]
                        or review["root_entry_id"] != confirms_entry_id
                        or review["state"] not in {"active", "confirmed"}
                    ):
                        raise CoordinationError(
                            "terminal critic confirmation has no active designated review"
                        )
                    if review["state"] == "active":
                        changed = connection.execute(
                            """
                            UPDATE obstacle_reviews
                            SET state='confirmed', confirmation_entry_id=?
                            WHERE review_id=? AND state='active'
                              AND confirmation_entry_id IS NULL
                            """,
                            (entry_id, review["review_id"]),
                        ).rowcount
                        if changed != 1:
                            raise CoordinationError(
                                "terminal critic confirmation lost its review CAS"
                            )
                    elif review["confirmation_entry_id"] != entry_id:
                        raise CoordinationError(
                            "critic review already has another confirmation"
                        )
                    review_id = str(review["review_id"])
                    recommendation_id = self._ensure_recommendation(
                        connection,
                        review_id=review_id,
                        root_entry_id=str(confirms_entry_id),
                        critic_entry_id=entry_id,
                        generation=int(slot["generation"]),
                        created_at=reconciled_at,
                    )
                else:
                    raise CoordinationError(
                        "explorer memory cannot enter obstacle-review evidence"
                    )
            connection.commit()
            return {
                "slot_id": str(slot["slot_id"]),
                "generation": int(slot["generation"]),
                "lane": str(slot["lane"]),
                "accepted_entry_ids": accepted,
                "review_id": review_id,
                "recommendation_id": recommendation_id,
            }
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def set_memory_cursor(
        self,
        worker: str,
        stream: str,
        cursor: str,
        *,
        generation: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        worker = _validate_identifier(worker, "worker")
        stream = _validate_identifier(stream, "stream")
        cursor = _validate_identifier(cursor, "cursor")
        observed_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            project = self._state(connection)
            current_generation = int(project["generation"])
            target_generation = current_generation if generation is None else generation
            if target_generation != current_generation:
                raise CoordinationError("memory cursor generation is not current")
            connection.execute(
                """
                INSERT INTO memory_cursors(worker, stream, cursor, generation, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(worker, stream) DO UPDATE SET
                    cursor=excluded.cursor,
                    generation=excluded.generation,
                    updated_at=excluded.updated_at
                """,
                (worker, stream, cursor, target_generation, observed_at),
            )
            return {
                "worker": worker,
                "stream": stream,
                "cursor": cursor,
                "generation": target_generation,
            }
        finally:
            connection.close()

    @staticmethod
    def _candidate_projection(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "candidate_receipt_id": row["candidate_id"],
            "generation": int(row["generation"]),
            "slot_id": row["slot_id"],
            "candidate_fact_id": row["candidate_fact_id"],
            "candidate_fact_identity": row["candidate_fact_identity"],
            "source_id": row["source_id"],
            "context_digest": row["context_digest"],
            "worker": row["worker"],
            "lane": row["lane"],
            "state": row["state"],
            "outcome": row["outcome"],
            "owner_resolution": row["owner_resolution"],
            "owner_acknowledged_unknown": (
                bool(row["owner_acknowledged_unknown"])
                if row["owner_acknowledged_unknown"] is not None
                else None
            ),
            "candidate_fact_active_at_resolution": (
                bool(row["candidate_fact_active_at_resolution"])
                if row["candidate_fact_active_at_resolution"] is not None
                else None
            ),
            "owner_resolved_at": row["owner_resolved_at"],
        }

    def register_candidate(
        self,
        worker: str,
        candidate_receipt_id: str,
        *,
        slot_id: str,
        candidate_fact_id: str,
        candidate_fact_identity: str,
        source_id: str | None,
        context_digest: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Freeze new paid slots immediately before a candidate verify launch."""

        expected_receipt = globals()["candidate_receipt_id"](
            slot_id=slot_id,
            candidate_fact_id=candidate_fact_id,
            candidate_fact_identity=candidate_fact_identity,
            source_id=source_id,
            context_digest=context_digest,
        )
        if candidate_receipt_id != expected_receipt:
            raise CoordinationError(
                "candidate receipt does not bind its exact identity"
            )
        registered_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_receipt_id,),
            ).fetchone()
            slot = (
                self._canonical_paid_slot(connection, worker, slot_id=slot_id)
                if existing is None
                else self._canonical_candidate_slot(
                    connection,
                    worker,
                    slot_id=slot_id,
                    require_current_generation=existing["state"] != "terminal",
                )
            )
            identity = (
                int(slot["generation"]),
                slot_id,
                candidate_fact_id,
                candidate_fact_identity,
                source_id,
                context_digest,
                worker,
                str(slot["lane"]),
            )
            if existing is None:
                overlay = self._active_candidate(connection)
                if overlay is not None:
                    raise CoordinationError(
                        "another candidate overlay is already active"
                    )
                connection.execute(
                    """
                    INSERT INTO candidates(
                        candidate_id, generation, slot_id, candidate_fact_id,
                        candidate_fact_identity, source_id, context_digest,
                        worker, lane, state, outcome, created_at, updated_at,
                        terminal_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, NULL)
                    """,
                    (
                        candidate_receipt_id,
                        *identity,
                        registered_at,
                        registered_at,
                    ),
                )
            else:
                observed = tuple(
                    existing[key]
                    for key in (
                        "generation",
                        "slot_id",
                        "candidate_fact_id",
                        "candidate_fact_identity",
                        "source_id",
                        "context_digest",
                        "worker",
                        "lane",
                    )
                )
                if observed != identity or existing["state"] not in {
                    "active",
                    "outcome_unknown",
                    "terminal",
                }:
                    raise CoordinationError(
                        "candidate receipt conflicts with prior coordination state"
                    )
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_receipt_id,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise CoordinationError("registered candidate disappeared")
            return self._candidate_projection(row)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def terminalize_candidate(
        self,
        worker: str,
        candidate_receipt_id: str,
        *,
        slot_id: str,
        outcome: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record every verify exit; only an exact unknown remains frozen."""

        if (
            not isinstance(candidate_receipt_id, str)
            or _SHA256_RE.fullmatch(candidate_receipt_id) is None
        ):
            raise CoordinationError("candidate_receipt_id must be 64 lowercase hex")
        try:
            releases_overlay = candidate_outcome_releases(outcome)
        except CoordinationConfigError as exc:
            raise CoordinationError(str(exc)) from exc
        terminal_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_receipt_id,),
            ).fetchone()
            self._canonical_candidate_slot(
                connection,
                worker,
                slot_id=slot_id,
                require_current_generation=(row is None or row["state"] != "terminal"),
            )
            if (
                row is None
                or row["worker"] != worker
                or row["slot_id"] != slot_id
                or row["state"] not in {"active", "outcome_unknown", "terminal"}
            ):
                raise CoordinationError("candidate is not terminalizable by this slot")
            if row["state"] == "outcome_unknown":
                if outcome != "outcome_unknown" or row["outcome"] != outcome:
                    raise CoordinationError(
                        "outcome-unknown candidate requires explicit owner resolution"
                    )
            elif row["state"] == "terminal":
                if row["outcome"] != outcome:
                    raise CoordinationError("candidate terminal outcome conflicts")
            else:
                next_state = (
                    "outcome_unknown" if outcome == "outcome_unknown" else "terminal"
                )
                connection.execute(
                    """
                    UPDATE candidates
                    SET state=?, outcome=?, updated_at=?, terminal_at=?
                    WHERE candidate_id=? AND state='active'
                    """,
                    (
                        next_state,
                        outcome,
                        terminal_at,
                        terminal_at if next_state == "terminal" else None,
                        candidate_receipt_id,
                    ),
                )
            if releases_overlay:
                self._advance_generation_if_ready(
                    connection,
                    observed_at=terminal_at,
                )
            final = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_receipt_id,),
            ).fetchone()
            connection.commit()
            if final is None:
                raise CoordinationError("terminal candidate disappeared")
            return self._candidate_projection(final)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def resolve_candidate_outcome_unknown(
        self,
        candidate_receipt_id: str,
        *,
        resolution: str,
        acknowledge_paid_outcome_unknown: bool,
        candidate_fact_active: bool,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Explicit owner-only release for a crash/unknown candidate overlay."""

        if (
            not isinstance(candidate_receipt_id, str)
            or _SHA256_RE.fullmatch(candidate_receipt_id) is None
        ):
            raise CoordinationError("candidate_receipt_id must be 64 lowercase hex")
        if resolution not in {"known_no_promotion", "abandon_unknown"}:
            raise CoordinationError("candidate owner resolution is unsupported")
        if acknowledge_paid_outcome_unknown is not True:
            raise CoordinationError(
                "owner must acknowledge that the paid candidate outcome is unknown"
            )
        if not isinstance(candidate_fact_active, bool):
            raise CoordinationError("candidate fact activity must be attested")
        if resolution == "known_no_promotion" and candidate_fact_active:
            raise CoordinationError(
                "known-no-promotion cannot resolve an active candidate fact"
            )
        resolved_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_receipt_id,),
            ).fetchone()
            if row is None:
                raise CoordinationError("candidate receipt does not exist")
            if row["state"] in {"active", "outcome_unknown"}:
                slot = connection.execute(
                    "SELECT state FROM round_slots WHERE slot_id=?",
                    (row["slot_id"],),
                ).fetchone()
                if slot is None or slot["state"] != "terminal":
                    raise CoordinationError(
                        "candidate source slot is not terminal; first "
                        "fail-stop and reconcile the paid turn"
                    )
            if row["state"] in {"active", "outcome_unknown"}:
                connection.execute(
                    """
                    UPDATE candidates
                    SET state='terminal', outcome='outcome_unknown',
                        updated_at=?, terminal_at=?, owner_resolution=?,
                        owner_acknowledged_unknown=1,
                        candidate_fact_active_at_resolution=?, owner_resolved_at=?
                    WHERE candidate_id=?
                      AND state IN ('active','outcome_unknown')
                    """,
                    (
                        resolved_at,
                        resolved_at,
                        resolution,
                        int(candidate_fact_active),
                        resolved_at,
                        candidate_receipt_id,
                    ),
                )
            elif row["state"] == "terminal" and row["owner_resolution"] is not None:
                if (
                    row["owner_resolution"] != resolution
                    or bool(row["owner_acknowledged_unknown"]) is not True
                    or bool(row["candidate_fact_active_at_resolution"])
                    is not candidate_fact_active
                ):
                    raise CoordinationError("candidate owner resolution conflicts")
            else:
                raise CoordinationError(
                    "a known terminal candidate does not need owner resolution"
                )
            self._advance_generation_if_ready(
                connection,
                observed_at=resolved_at,
            )
            final = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_receipt_id,),
            ).fetchone()
            connection.commit()
            if final is None:
                raise CoordinationError("owner-resolved candidate disappeared")
            return self._candidate_projection(final)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def record_candidate(
        self,
        worker: str,
        candidate_id: str,
        *,
        state: str = "observed",
        generation: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Content-free candidate identity stub for later root integration."""

        worker = _validate_identifier(worker, "worker")
        candidate_id = _validate_identifier(candidate_id, "candidate_id")
        if state not in {"observed", "promising", "rejected", "selected"}:
            raise CoordinationError("candidate state is invalid")
        lane = self._worker_lane(worker)
        observed_at = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            project = self._state(connection)
            current_generation = int(project["generation"])
            target_generation = current_generation if generation is None else generation
            if target_generation != current_generation:
                raise CoordinationError("candidate generation is not current")
            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, generation, worker, lane, state,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at
                WHERE candidates.generation=excluded.generation
                  AND candidates.worker=excluded.worker
                  AND candidates.lane=excluded.lane
                """,
                (
                    candidate_id,
                    target_generation,
                    worker,
                    lane,
                    state,
                    observed_at,
                    observed_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if row is None or (
                row["generation"] != target_generation
                or row["worker"] != worker
                or row["lane"] != lane
            ):
                raise CoordinationError("candidate id conflicts with prior state")
            return {
                "candidate_id": candidate_id,
                "candidate_fact_id": None,
                "candidate_fact_identity": None,
                "generation": target_generation,
                "worker": worker,
                "lane": lane,
                "state": row["state"],
            }
        finally:
            connection.close()

    def candidate_entry(self, candidate_receipt_id: str) -> dict[str, Any] | None:
        if (
            not isinstance(candidate_receipt_id, str)
            or _SHA256_RE.fullmatch(candidate_receipt_id) is None
        ):
            raise CoordinationError("candidate_receipt_id must be 64 lowercase hex")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_receipt_id,),
            ).fetchone()
            return self._candidate_projection(row) if row is not None else None
        finally:
            connection.close()

    def list_candidates(self, *, generation: int | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            project = self._state(connection)
            target_generation = (
                int(project["generation"]) if generation is None else generation
            )
            return [
                {
                    "candidate_id": row["candidate_id"],
                    "candidate_fact_id": row["candidate_fact_id"],
                    "candidate_fact_identity": row["candidate_fact_identity"],
                    "generation": row["generation"],
                    "worker": row["worker"],
                    "lane": row["lane"],
                    "state": row["state"],
                }
                for row in connection.execute(
                    """
                    SELECT candidate_id, candidate_fact_id,
                           candidate_fact_identity, generation, worker, lane, state
                    FROM candidates WHERE generation=? ORDER BY candidate_id
                    """,
                    (target_generation,),
                ).fetchall()
            ]
        finally:
            connection.close()
