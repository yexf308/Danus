"""fact graph — project-shared, verified, content-addressed DAG.

One human/agent-readable markdown file per fact: YAML frontmatter (fact_id /
problem_id / author / predecessors / glossary_introduces) + a markdown body
(## statement / ## proof / optional ## intuition). Plus the project glossary, a
revocation log, and a ``_revoked/`` archive. See DATA_MODEL.md §3.

Pure data-structure I/O. *Whether* a claim deserves to be a fact is the
verifier's call (the gate lives in ``fact submit``, which calls ``add`` only on
accept). ``add`` keeps the project glossary up to date and exposes a glossary
coverage check so the graph stays readable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections import deque
from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Deque, Dict, Iterator, List, Optional, Set, Tuple

from . import bm25
from . import glossary as _glossary
from ._util import utc_now
from .schema import (
    Fact,
    clean_external_refs,
    compute_fact_id,
    compute_fact_identity,
)

_PRED_RE = re.compile(r"^predecessors:\s*\[(.*)\]\s*$")
_GLOSS_LINE_RE = re.compile(r"^\s{2}([^:]+):\s*(.*)$")
# Active graphs use content-addressed 16-hex ids, while legacy fixtures/imports
# may still carry semantic ids such as ``fact_main``. Storage accepts either as
# long as the id is one safe path segment; the verification HTTP contract remains
# stricter and accepts only content-addressed ids.
_SAFE_FACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTENT_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_FULL_FACT_IDENTITY_RE = re.compile(r"^[0-9a-f]{64}$")
_LINE_BREAK_RE = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
FACT_CONTEXT_SCHEMA_VERSION = 1
VERIFICATION_CONTEXT_SCHEMA_VERSION = 3
VERIFICATION_CONTEXT_PROJECTION = "full-statement-closure-adaptive-proofs-v1"


class FactPromotionOutcomeUnknown(RuntimeError):
    """The storage outcome cannot be stated definitively after an fsync failure."""


def _term_occurs(term: str, text: str) -> bool:
    """Literal notation match with identifier boundaries where applicable."""
    if not term or not text:
        return False
    prefix = r"(?<![A-Za-z0-9_])" if (term[0].isalnum() or term[0] == "_") else ""
    suffix = r"(?![A-Za-z0-9_])" if (term[-1].isalnum() or term[-1] == "_") else ""
    return re.search(prefix + re.escape(term) + suffix, text) is not None


def fact_context_digest(
    *,
    scope: Dict[str, object],
    facts: List[Dict[str, object]],
    glossary: Optional[Dict[str, str]] = None,
) -> str:
    """Digest exactly the mathematical context sent to a verifier.

    Budget/completeness bookkeeping is deliberately excluded: the digest binds
    the requested scope, hydrated fact records, and selected project/global
    definitions—not transport metadata. It is separate from (and never changes)
    the content-addressed ``fact_id`` contract.
    """
    canonical = json.dumps(
        {
            "schema_version": FACT_CONTEXT_SCHEMA_VERSION,
            "scope": scope,
            "facts": facts,
            "glossary": glossary or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verification_context_digest(
    *,
    context: Dict[str, object],
) -> str:
    """Bind the complete adaptive envelope except for its digest field itself.

    This includes every statement, edge, fact-local/global definition, exact
    proof byte sequence, scope/round, completeness marker, omission report, and
    budget accounting value. Replaying a verdict against any materially
    different context therefore changes the attestation.
    """
    if "digest" in context:
        raise ValueError("verification context digest input must omit digest")
    canonical = json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fact_identity_from_verification_context(
    *,
    verification_context: Dict[str, object],
    problem_id: str,
    predecessors: List[str],
    glossary_introduces: Dict[str, str],
    statement: str,
    proof: str,
) -> str:
    """Derive one full fact identity from an authenticated round-zero snapshot.

    This is deliberately pure: callers pass the exact context object returned by
    :meth:`FactGraph.verification_context`, so candidate admission never takes a
    second, potentially racy graph snapshot.  Transport-only budget accounting
    and glossary entries selected solely by ``intuition`` do not affect the
    semantic identity.
    """

    if not isinstance(verification_context, dict):
        raise ValueError("fact_identity_context_error: context must be an object")
    if not isinstance(problem_id, str) or not problem_id:
        raise ValueError("fact_identity_context_error: problem_id is invalid")
    if not isinstance(predecessors, list) or any(
        not isinstance(item, str) or _CONTENT_FACT_ID_RE.fullmatch(item) is None
        for item in predecessors
    ):
        raise ValueError("fact_identity_context_error: predecessors are invalid")
    if len(predecessors) != len(set(predecessors)):
        raise ValueError("fact_identity_context_error: predecessors contain duplicates")
    if not isinstance(glossary_introduces, dict) or any(
        not isinstance(term, str) or not isinstance(definition, str)
        for term, definition in glossary_introduces.items()
    ):
        raise ValueError("fact_identity_context_error: definitions are invalid")
    if not isinstance(statement, str) or not isinstance(proof, str):
        raise ValueError("fact_identity_context_error: statement/proof are invalid")

    digest = verification_context.get("digest")
    context_without_digest = {
        key: value for key, value in verification_context.items() if key != "digest"
    }
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or verification_context_digest(context=context_without_digest) != digest
    ):
        raise ValueError("fact_identity_context_error: context digest is invalid")

    scope = verification_context.get("scope")
    facts = verification_context.get("facts")
    glossary = verification_context.get("glossary")
    expected_fact_id = compute_fact_id(
        problem_id=problem_id,
        predecessors=predecessors,
        glossary_introduces=glossary_introduces,
        statement=statement,
        proof=proof,
    )
    if (
        verification_context.get("schema_version")
        != VERIFICATION_CONTEXT_SCHEMA_VERSION
        or not isinstance(scope, dict)
        or scope.get("projection") != VERIFICATION_CONTEXT_PROJECTION
        or scope.get("candidate_fact_id") != expected_fact_id
        or scope.get("requested_fact_ids") != predecessors
        or scope.get("predecessor_depth") is not None
        or scope.get("proof_mode") != "adaptive"
        or scope.get("include_project_glossary") is not False
        or scope.get("expansion_round") != 0
        or scope.get("expanded_proof_ids") != []
        or verification_context.get("expanded_proofs") != []
        or verification_context.get("complete") is not True
        or verification_context.get("truncated") is not False
        or any(
            verification_context.get(field) != []
            for field in (
                "missing_fact_ids",
                "revoked_fact_ids",
                "omitted_fact_ids",
                "omitted_glossary_terms",
                "omitted_expanded_proof_ids",
            )
        )
        or not isinstance(facts, list)
        or not isinstance(glossary, dict)
        or any(
            not isinstance(term, str) or not isinstance(definition, str)
            for term, definition in glossary.items()
        )
        or any(term in glossary for term in glossary_introduces)
    ):
        raise ValueError(
            "fact_identity_context_error: context is not an exact complete "
            "round-zero candidate snapshot"
        )

    closure_fact_ids = scope.get("closure_fact_ids")
    if not isinstance(closure_fact_ids, list) or any(
        not isinstance(item, str) or _CONTENT_FACT_ID_RE.fullmatch(item) is None
        for item in closure_fact_ids
    ):
        raise ValueError("fact_identity_context_error: closure binding is invalid")
    if len(closure_fact_ids) != len(set(closure_fact_ids)):
        raise ValueError("fact_identity_context_error: closure binding has duplicates")

    canonical_facts: List[Dict[str, object]] = []
    semantic_glossary_texts = [
        statement,
        proof,
        *(str(value) for value in glossary_introduces.values()),
    ]
    observed_fact_ids: List[str] = []
    for record in facts:
        if not isinstance(record, dict) or set(record) != {
            "fact_id",
            "statement",
            "predecessors",
            "glossary_introduces",
        }:
            raise ValueError("fact_identity_context_error: malformed fact-card binding")
        record_id = record.get("fact_id")
        record_statement = record.get("statement")
        record_predecessors = record.get("predecessors")
        record_glossary = record.get("glossary_introduces")
        if (
            not isinstance(record_id, str)
            or _CONTENT_FACT_ID_RE.fullmatch(record_id) is None
            or not isinstance(record_statement, str)
            or not isinstance(record_predecessors, list)
            or any(
                not isinstance(item, str) or _CONTENT_FACT_ID_RE.fullmatch(item) is None
                for item in record_predecessors
            )
            or len(record_predecessors) != len(set(record_predecessors))
            or not isinstance(record_glossary, dict)
            or any(
                not isinstance(term, str) or not isinstance(definition, str)
                for term, definition in record_glossary.items()
            )
        ):
            raise ValueError("fact_identity_context_error: malformed fact-card binding")
        observed_fact_ids.append(record_id)
        semantic_glossary_texts.append(record_statement)
        semantic_glossary_texts.extend(str(value) for value in record_glossary.values())
        canonical_facts.append(
            {
                "fact_id": record_id,
                "statement": re.sub(r"\s+", " ", record_statement).strip(),
                "predecessors": sorted(record_predecessors),
                "glossary_introduces": {
                    str(term): re.sub(r"\s+", " ", str(definition)).strip()
                    for term, definition in record_glossary.items()
                },
            }
        )
    if observed_fact_ids != closure_fact_ids:
        raise ValueError("fact_identity_context_error: closure/fact binding differs")

    canonical_facts.sort(key=lambda record: str(record["fact_id"]))
    semantic_glossary = select_referenced_definitions(
        semantic_glossary_texts,
        {str(term): str(definition) for term, definition in glossary.items()},
    )
    context_bindings = {
        "schema_version": verification_context["schema_version"],
        "projection": scope["projection"],
        "closure_fact_ids": sorted(observed_fact_ids),
        "facts": canonical_facts,
    }
    return compute_fact_identity(
        problem_id=problem_id,
        predecessors=sorted(predecessors),
        glossary_introduces=glossary_introduces,
        statement=statement,
        proof=proof,
        context_bindings=context_bindings,
        glossary_bindings=semantic_glossary,
    )


def dependency_closure_digest(records: List[Dict[str, object]]) -> str:
    """Commit to the canonical full DAG closure without transporting its skeleton."""
    canonical = json.dumps(
        {"schema_version": 1, "records": records},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def select_referenced_definitions(
    texts: List[str], available: Dict[str, str]
) -> Dict[str, str]:
    """Select literal referenced definitions, following definition dependencies."""
    remaining = {
        str(term): str(definition)
        for term, definition in available.items()
        if str(term)
    }
    selected: Dict[str, str] = {}
    pending: Deque[str] = deque(text for text in texts if text)
    while pending and remaining:
        text = pending.popleft()
        matched = sorted(term for term in remaining if _term_occurs(term, text))
        for term in matched:
            definition = remaining.pop(term)
            selected[term] = definition
            if definition:
                pending.append(definition)
    return {term: selected[term] for term in sorted(selected)}


def statement_of(text: str) -> str:
    """The fact's ``## statement`` body (up to ``## proof``), as a
    one-line snippet — what a searcher needs to recognize a fact."""
    out: List[str] = []
    in_stmt = False
    for line in text.splitlines():
        heading = line.strip().lower()
        if not in_stmt:
            if heading == "## statement":
                in_stmt = True
            continue
        if heading == "## proof":
            break
        out.append(line.strip())
    return " ".join(s for s in out if s).strip()


def _proof_of(text: str) -> str:
    """Return the fact's complete ``## proof`` body without ever slicing it."""
    out: List[str] = []
    in_proof = False
    intuition_flag = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if index > 0 and line.strip() == "---":
            break
        if line.startswith("has_intuition:"):
            intuition_flag = line.split(":", 1)[1].strip().lower() == "true"
            break
    for line in lines:
        heading = line.strip().lower()
        if not in_proof:
            if heading == "## proof":
                in_proof = True
            continue
        out.append(line)
    intuition_headings = [
        index
        for index, line in enumerate(out)
        if line.strip().lower() == "## intuition"
    ]
    # New files explicitly say whether an intuition section exists. The final
    # matching heading is the serializer's boundary, so a proof may itself use
    # an earlier ``## intuition`` subsection. Legacy files keep the historical
    # first-heading boundary; files marked false treat every heading as proof.
    if intuition_headings and intuition_flag is True:
        out = out[: intuition_headings[-1]]
    elif intuition_headings and intuition_flag is None:
        out = out[: intuition_headings[0]]
    return "\n".join(out).strip()


def serialize_fact(fact: Fact) -> str:
    """Render a Fact to its readable markdown-with-frontmatter form."""
    lines = [
        "---",
        f"fact_id: {fact.fact_id}",
        f"problem_id: {fact.problem_id}",
        f"author: {fact.author}",
        f"predecessors: [{', '.join(fact.predecessors)}]",
        "has_intuition: " + ("true" if fact.intuition.strip() else "false"),
    ]
    # A JSON flow-object is valid YAML and remains human-readable while making
    # arbitrary string keys/values (colons, quotes, and newlines included)
    # exactly round-trippable. The parser still accepts legacy block mappings.
    lines.append(
        "glossary_introduces: "
        + json.dumps(dict(sorted(fact.glossary_introduces.items())), ensure_ascii=True)
    )
    if fact.fact_identity:
        lines.append(f"fact_identity: {fact.fact_identity}")
    # external_refs: a JSON flow-array on one line (valid YAML, trivially parsed).
    # Always emitted (`[]` when empty), like glossary_introduces.
    lines.append("external_refs: " + json.dumps(fact.external_refs, ensure_ascii=True))
    lines += [
        "---",
        "",
        "## statement",
        fact.statement.strip(),
        "",
        "## proof",
        fact.proof.strip(),
    ]
    if fact.intuition.strip():
        lines += ["", "## intuition", fact.intuition.strip()]
    lines.append("")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> Dict[str, object]:
    """Extract the canonical and mutable fields from a fact's frontmatter.

    ``external_refs`` defaults to ``[]`` for facts written before the field
    existed. Unknown fields remain ignored for backward compatibility.
    """
    fact_id = ""
    problem_id = ""
    preds: List[str] = []
    gloss: Dict[str, str] = {}
    refs: List[Dict[str, object]] = []
    fact_identity: Optional[str] = None
    in_gloss = False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if i > 0 and line.strip() == "---":
            break
        if line.startswith("fact_id:"):
            fact_id = line.split(":", 1)[1].strip()
            continue
        if line.startswith("problem_id:"):
            problem_id = line.split(":", 1)[1].strip()
            continue
        m = _PRED_RE.match(line.strip())
        if m:
            preds = [x.strip() for x in m.group(1).split(",") if x.strip()]
            in_gloss = False
            continue
        if line.strip().startswith("glossary_introduces:"):
            payload = line.strip()[len("glossary_introduces:") :].strip()
            if payload:
                try:
                    parsed_gloss = json.loads(payload)
                except json.JSONDecodeError:
                    parsed_gloss = None
                if isinstance(parsed_gloss, dict):
                    gloss = {str(k): str(v) for k, v in parsed_gloss.items()}
                    in_gloss = False
                    continue
            in_gloss = not payload
            continue
        if line.strip().startswith("external_refs:"):
            in_gloss = False
            payload = line.strip()[len("external_refs:") :].strip()
            try:
                refs = json.loads(payload) if payload else []
            except json.JSONDecodeError:
                refs = []
            continue
        if line.startswith("fact_identity:"):
            fact_identity = line.split(":", 1)[1].strip()
            in_gloss = False
            continue
        if in_gloss:
            gm = _GLOSS_LINE_RE.match(line)
            if gm:
                gloss[gm.group(1).strip()] = gm.group(2).strip()
            else:
                in_gloss = False
    return {
        "fact_id": fact_id,
        "problem_id": problem_id,
        "predecessors": preds,
        "glossary_introduces": gloss,
        "fact_identity": fact_identity,
        "external_refs": refs,
    }


class FactGraph:
    """Rooted at the project directory; the only correctness source."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "fact_graph"
        self.facts_dir = self.dir / "facts"
        self.revoked_dir = self.dir / "_revoked"
        self.glossary_path = self.dir / "glossary.json"
        self.revocation_log = self.dir / "revocation_log.jsonl"
        self.pending_add_path = self.dir / ".pending_add.json"
        self.pending_add_commit_path = self.dir / ".pending_add.committed.json"
        self.pending_add_abort_path = self.dir / ".pending_add.rollback_required.json"
        self.pending_revocation_path = self.dir / ".pending_revocation.json"

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Persist a rename/unlink in ``directory`` before reporting success."""
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_directory_with_retry(self, directory: Path) -> None:
        """Prove directory-entry durability, retrying one transient failure.

        A successful ``unlink`` followed by a failed directory ``fsync`` leaves
        the pathname absent in the running kernel while the old entry may still
        reappear after power loss.  Retrying the barrier is therefore materially
        different from checking ``Path.exists()``: only a successful fsync proves
        that a later mutation cannot be followed by resurrection of an obsolete
        transaction marker.
        """
        try:
            self._fsync_directory(directory)
        except Exception as first_error:
            try:
                self._fsync_directory(directory)
            except Exception as retry_error:
                raise retry_error from first_error

    def _mkdir_durable(self, directory: Path) -> None:
        """Create ``directory`` and durably publish its path entry.

        The parent barrier also runs when ``directory`` is already visible.  It
        may be a retry after an earlier ``mkdir`` succeeded but the parent fsync
        failed; fsyncing the child directory cannot make its own parent entry
        durable.
        """
        missing: List[Path] = []
        cursor = directory
        while not cursor.exists():
            missing.append(cursor)
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        directory.mkdir(parents=True, exist_ok=True)
        parents_to_sync = (
            [created.parent for created in reversed(missing)]
            if missing
            else [directory.parent]
        )
        for parent in parents_to_sync:
            self._fsync_directory_with_retry(parent)

    def _atomic_write_text(self, path: Path, text: str) -> None:
        """Write ``text`` through a same-directory durable atomic replace."""
        self._mkdir_durable(path.parent)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            self._fsync_directory(path.parent)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _unlink_durable(self, path: Path) -> None:
        """Remove ``path`` and prove the parent entry state is durable.

        The barrier also runs when the pathname is already absent.  That case can
        be a retry after an earlier process observed a successful unlink but lost
        the following fsync; treating absence as success would let recovery erase
        its own journal and later resurrect stale bytes after another mutation.
        """
        if path.exists():
            path.unlink()
        self._fsync_directory_with_retry(path.parent)

    def _assert_graph_readable(self) -> None:
        pending = []
        prepared_add = self._load_pending_add_unlocked()
        aborted_add = self._load_pending_add_abort_unlocked()
        if aborted_add is not None:
            pending.append(self.pending_add_abort_path.name)
        elif (committed_add := self._load_pending_add_commit_unlocked()) is not None:
            # The commit marker may be visible after ``os.replace`` even though
            # the creating process crashed before fsyncing this directory.  A
            # read must not expose that transient state as authoritative: power
            # loss could otherwise discard the marker, leaving prepared-only
            # recovery to roll back a fact that callers already observed.
            try:
                self._fsync_directory_with_retry(self.dir)
            except Exception as exc:
                raise ValueError(
                    "fact_graph_recovery_required: committed add marker "
                    "durability barrier failed"
                ) from exc
            self._validate_committed_add_unlocked(prepared_add, committed_add)
        elif prepared_add is not None:
            pending.append(self.pending_add_path.name)
        if self.pending_revocation_path.exists():
            pending.append(self.pending_revocation_path.name)
        if pending:
            raise ValueError(
                "fact_graph_recovery_required: pending graph transaction "
                + ", ".join(pending)
                + "; retry a graph mutation or the original revoke"
            )

    def _path(self, fact_id: str) -> Path:
        if not isinstance(fact_id, str) or not _SAFE_FACT_ID_RE.fullmatch(fact_id):
            raise ValueError(f"invalid fact_id: {fact_id!r}")
        return self.facts_dir / f"{fact_id}.md"

    def _revoked_path(self, fact_id: str) -> Path:
        if not isinstance(fact_id, str) or not _SAFE_FACT_ID_RE.fullmatch(fact_id):
            raise ValueError(f"invalid fact_id: {fact_id!r}")
        return self.revoked_dir / f"{fact_id}.md"

    def _validate_fact_integrity(
        self, fact_id: str, raw: str, frontmatter: Dict[str, object]
    ) -> None:
        """Verify filename/frontmatter/content-address agreement for active ids."""
        if self._revoked_path(fact_id).exists():
            raise ValueError(
                f"fact_integrity_error: {fact_id} exists in both active and revoked stores"
            )
        recorded_id = frontmatter.get("fact_id")
        if recorded_id and recorded_id != fact_id:
            raise ValueError(
                f"fact_integrity_error: filename {fact_id} != frontmatter {recorded_id}"
            )
        # Legacy imported semantic ids are readable but predate content addressing.
        # Every id produced by current ``add`` is 16-hex and must verify exactly.
        if not _CONTENT_FACT_ID_RE.fullmatch(fact_id):
            return
        problem_id = frontmatter.get("problem_id")
        if recorded_id != fact_id or not isinstance(problem_id, str) or not problem_id:
            raise ValueError(
                f"fact_integrity_error: incomplete frontmatter for {fact_id}"
            )
        recorded_identity = frontmatter.get("fact_identity")
        if recorded_identity is not None and (
            not isinstance(recorded_identity, str)
            or _FULL_FACT_IDENTITY_RE.fullmatch(recorded_identity) is None
        ):
            raise ValueError(
                f"fact_integrity_error: invalid full fact identity for {fact_id}"
            )
        expected = compute_fact_id(
            problem_id=problem_id,
            predecessors=frontmatter["predecessors"],  # type: ignore[arg-type]
            glossary_introduces=frontmatter["glossary_introduces"],  # type: ignore[arg-type]
            statement=statement_of(raw),
            proof=_proof_of(raw),
        )
        if expected != fact_id:
            raise ValueError(
                f"fact_integrity_error: content hash {expected} != fact_id {fact_id}"
            )

    @contextmanager
    def _graph_lock(self, operation: int) -> Iterator[None]:
        """Take the stable cross-process lock shared by readers and writers."""
        self._mkdir_durable(self.dir)
        lock_path = self.dir / ".mutation.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        """Serialize dependency checks and mutations across processes."""
        with self._graph_lock(fcntl.LOCK_EX):
            # Stabilize every prior transaction-marker create/unlink before
            # inspecting recovery state or changing fact/glossary bytes.  This is
            # the cross-process guard for the narrow case where a prior cleanup
            # made a marker visibly absent but could not fsync that absence.
            try:
                self._fsync_directory_with_retry(self.dir)
            except Exception as exc:
                raise RuntimeError(
                    "fact_graph_recovery_required: mutation directory durability "
                    "barrier failed"
                ) from exc
            yield

    @contextmanager
    def _snapshot_lock(self) -> Iterator[None]:
        """Hold one linearizable public truth snapshot across all of its reads.

        Merely checking for a pending journal before reading has a TOCTOU window:
        a cascade can move its first file after the check and expose a graph that
        is neither the pre- nor post-revocation state. A shared lock pairs every
        public snapshot with the writers' exclusive lock. If a prior process died
        with a journal, the lock is released by the kernel and the check below
        fails closed until a mutation performs recovery.
        """
        with self._graph_lock(fcntl.LOCK_SH):
            self._assert_graph_readable()
            yield

    def _load_pending_add_unlocked(self) -> Optional[Dict[str, object]]:
        if not self.pending_add_path.exists():
            return None
        try:
            payload = json.loads(self.pending_add_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                "fact_graph_recovery_error: unreadable pending add"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("fact_graph_recovery_error: malformed pending add")
        fact_id = payload.get("fact_id")
        previous_fact = payload.get("previous_fact")
        if (
            not isinstance(fact_id, str)
            or not _SAFE_FACT_ID_RE.fullmatch(fact_id)
            or (previous_fact is not None and not isinstance(previous_fact, str))
        ):
            raise ValueError("fact_graph_recovery_error: malformed pending add")
        if payload.get("schema_version") == 1:
            previous_glossary = payload.get("previous_glossary")
            glossary_existed = payload.get("glossary_existed")
            if (
                not isinstance(previous_glossary, dict)
                or any(
                    not isinstance(term, str) or not isinstance(definition, str)
                    for term, definition in previous_glossary.items()
                )
                or not isinstance(glossary_existed, bool)
            ):
                raise ValueError("fact_graph_recovery_error: malformed pending add")
            return payload
        if (
            payload.get("schema_version") != 2
            or payload.get("state") != "prepared"
            or not _CONTENT_FACT_ID_RE.fullmatch(fact_id)
            or not isinstance(payload.get("transaction_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", str(payload["transaction_id"]))
            or (
                payload.get("previous_glossary_text") is not None
                and not isinstance(payload.get("previous_glossary_text"), str)
            )
            or not isinstance(payload.get("expected_fact_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload["expected_fact_sha256"]))
            or (
                payload.get("expected_glossary_sha256") is not None
                and (
                    not isinstance(payload.get("expected_glossary_sha256"), str)
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(payload["expected_glossary_sha256"]),
                    )
                )
            )
        ):
            raise ValueError("fact_graph_recovery_error: malformed pending add")
        return payload

    def _load_pending_add_commit_unlocked(self) -> Optional[Dict[str, object]]:
        if not self.pending_add_commit_path.exists():
            return None
        try:
            payload = json.loads(
                self.pending_add_commit_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                "fact_graph_recovery_error: unreadable committed add marker"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                "fact_graph_recovery_error: malformed committed add marker"
            )
        if (
            payload.get("schema_version") != 2
            or payload.get("state") != "committed"
            or not isinstance(payload.get("transaction_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", str(payload["transaction_id"]))
            or not isinstance(payload.get("fact_id"), str)
            or not _CONTENT_FACT_ID_RE.fullmatch(str(payload["fact_id"]))
            or not isinstance(payload.get("expected_fact_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload["expected_fact_sha256"]))
            or (
                payload.get("expected_glossary_sha256") is not None
                and (
                    not isinstance(payload.get("expected_glossary_sha256"), str)
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(payload["expected_glossary_sha256"]),
                    )
                )
            )
        ):
            raise ValueError(
                "fact_graph_recovery_error: malformed committed add marker"
            )
        return payload

    def _load_pending_add_abort_unlocked(self) -> Optional[Dict[str, object]]:
        if not self.pending_add_abort_path.exists():
            return None
        try:
            payload = json.loads(
                self.pending_add_abort_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                "fact_graph_recovery_error: unreadable add rollback marker"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("state") != "rollback_required"
            or not isinstance(payload.get("transaction_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", str(payload["transaction_id"]))
            or not isinstance(payload.get("fact_id"), str)
            or not _CONTENT_FACT_ID_RE.fullmatch(str(payload["fact_id"]))
        ):
            raise ValueError("fact_graph_recovery_error: malformed add rollback marker")
        return payload

    @staticmethod
    def _pending_add_text(payload: Dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"

    def _restore_pending_add_snapshot_unlocked(
        self, payload: Dict[str, object]
    ) -> None:
        """Idempotently restore the exact snapshot preceding an add."""
        fact_path = self._path(str(payload["fact_id"]))
        previous_fact = payload["previous_fact"]
        if isinstance(previous_fact, str):
            self._atomic_write_text(fact_path, previous_fact)
        else:
            self._unlink_durable(fact_path)

        if payload["schema_version"] == 1:
            previous_glossary = payload["previous_glossary"]
            if payload["glossary_existed"]:
                assert isinstance(previous_glossary, dict)
                self._atomic_write_text(
                    self.glossary_path,
                    json.dumps(
                        dict(sorted(previous_glossary.items())),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
            else:
                self._unlink_durable(self.glossary_path)
            return

        previous_glossary_text = payload["previous_glossary_text"]
        if isinstance(previous_glossary_text, str):
            self._atomic_write_text(
                self.glossary_path,
                previous_glossary_text,
            )
        else:
            self._unlink_durable(self.glossary_path)

    def _rollback_pending_add_unlocked(self) -> None:
        """Rollback a prepared add; committed transactions are never reverted."""
        aborted = self._load_pending_add_abort_unlocked()
        if aborted is not None:
            self._recover_aborted_add_unlocked(aborted)
            return
        committed = self._load_pending_add_commit_unlocked()
        if committed is not None:
            prepared = self._load_pending_add_unlocked()
            self._validate_committed_add_unlocked(prepared, committed)
            if not self._cleanup_committed_add_unlocked():
                raise RuntimeError(
                    "fact_graph_recovery_required: committed add cleanup did not complete"
                )
            return
        payload = self._load_pending_add_unlocked()
        if payload is None:
            return
        self._restore_pending_add_snapshot_unlocked(payload)
        self._unlink_durable(self.pending_add_path)

    def _recover_aborted_add_unlocked(self, aborted: Dict[str, object]) -> None:
        """Idempotently finish rollback after an uncertain commit-marker write."""
        prepared = self._load_pending_add_unlocked()
        if (
            prepared is None
            or prepared.get("schema_version") != 2
            or prepared.get("transaction_id") != aborted.get("transaction_id")
            or prepared.get("fact_id") != aborted.get("fact_id")
        ):
            raise RuntimeError(
                "fact_graph_recovery_required: add rollback snapshot is unavailable"
            )

        # The rollback marker stays durable throughout, so even a partially
        # restored snapshot cannot be observed as graph truth.
        try:
            self._unlink_durable(self.pending_add_commit_path)
        except Exception:
            pass
        self._restore_pending_add_snapshot_unlocked(prepared)
        if self.pending_add_commit_path.exists():
            raise RuntimeError(
                "fact_graph_recovery_required: uncertain add commit marker remains"
            )
        try:
            self._fsync_directory(self.dir)
        except Exception as exc:
            raise RuntimeError(
                "fact_graph_recovery_required: add rollback is not durable"
            ) from exc
        # Removing the abort marker first is safe: a crash then leaves the
        # prepared snapshot, whose normal recovery repeats this rollback.
        self._unlink_durable(self.pending_add_abort_path)
        self._unlink_durable(self.pending_add_path)

    def _validate_committed_add_unlocked(
        self,
        prepared: Optional[Dict[str, object]],
        committed: Dict[str, object],
    ) -> None:
        """Fail closed unless a committed marker matches exact graph bytes."""
        try:
            if prepared is not None:
                if prepared.get("schema_version") != 2 or any(
                    prepared.get(key) != committed.get(key)
                    for key in (
                        "transaction_id",
                        "fact_id",
                        "expected_fact_sha256",
                        "expected_glossary_sha256",
                    )
                ):
                    raise ValueError("prepared and committed add markers disagree")
            fact_id = str(committed["fact_id"])
            fact_path = self._path(fact_id)
            raw_bytes = fact_path.read_bytes()
            if (
                hashlib.sha256(raw_bytes).hexdigest()
                != committed["expected_fact_sha256"]
            ):
                raise ValueError("committed fact bytes do not match marker")
            raw = raw_bytes.decode("utf-8")
            self._validate_fact_integrity(fact_id, raw, parse_frontmatter(raw))

            expected_glossary_sha256 = committed["expected_glossary_sha256"]
            if expected_glossary_sha256 is None:
                if self.glossary_path.exists():
                    raise ValueError(
                        "committed glossary existence does not match marker"
                    )
            else:
                glossary_bytes = self.glossary_path.read_bytes()
                if (
                    hashlib.sha256(glossary_bytes).hexdigest()
                    != expected_glossary_sha256
                ):
                    raise ValueError("committed glossary bytes do not match marker")
                self._read_project_glossary(strict=True)
        except Exception as exc:
            raise ValueError(
                "fact_graph_recovery_required: committed add state is not exact"
            ) from exc

    def _cleanup_committed_add_unlocked(self) -> bool:
        """Best-effort cleanup in an order that can never turn commit into rollback."""
        if self.pending_add_path.exists():
            try:
                self._unlink_durable(self.pending_add_path)
            except Exception:
                if self.pending_add_path.exists():
                    return False
                # A monkeypatched/platform unlink helper may raise after removing
                # the entry but before proving the parent directory durable.
                try:
                    self._fsync_directory_with_retry(self.dir)
                except Exception:
                    return False
        else:
            # A prior cleanup may have unlinked the prepared snapshot and then
            # failed its directory fsync.  Confirm that absence before removing
            # the only marker that tells restart recovery to preserve the data.
            try:
                self._fsync_directory_with_retry(self.dir)
            except Exception:
                return False
        try:
            self._unlink_durable(self.pending_add_commit_path)
        except Exception:
            if self.pending_add_commit_path.exists():
                return False
            # Visible absence is not a durability proof.  A one-shot post-unlink
            # fsync failure is retried here; persistent failure leaves cleanup
            # incomplete, and the next mutation's mandatory root barrier blocks
            # any byte change until the absence can be proven durable.
            try:
                self._fsync_directory_with_retry(self.dir)
            except Exception:
                return False
        return not (
            self.pending_add_path.exists() or self.pending_add_commit_path.exists()
        )

    def _recover_pending_transactions_unlocked(self) -> Optional[Dict[str, object]]:
        self._rollback_pending_add_unlocked()
        return self._resume_pending_revocation_unlocked()

    def _canonical_fact_identity_unlocked(
        self,
        *,
        problem_id: str,
        predecessors: List[str],
        glossary_introduces: Dict[str, str],
        statement: str,
        proof: str,
        fact_id: str,
    ) -> str:
        """Bind a candidate to one complete authenticated dependency snapshot."""

        stable_predecessors = sorted(predecessors)
        context = self._verification_context_unlocked(
            stable_predecessors,
            max_chars=None,
            candidate_fact_id=fact_id,
            expanded_proof_ids=[],
            expansion_round=0,
            expanded_proof_max_chars=None,
            glossary_texts=[
                statement,
                proof,
                *(str(value) for value in glossary_introduces.values()),
            ],
            glossary_exclude_terms=[str(term) for term in glossary_introduces],
        )
        if (
            context.get("complete") is not True
            or context.get("truncated") is not False
            or context.get("missing_fact_ids")
            or context.get("revoked_fact_ids")
            or context.get("omitted_fact_ids")
            or context.get("omitted_glossary_terms")
            or context.get("omitted_expanded_proof_ids")
        ):
            raise ValueError(
                "fact_identity_context_error: dependency context is not complete"
            )
        return fact_identity_from_verification_context(
            verification_context=context,
            problem_id=problem_id,
            predecessors=stable_predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
        )

    def _lookup_active_exact_identity_unlocked(
        self,
        *,
        problem_id: str,
        predecessors: List[str],
        glossary_introduces: Dict[str, str],
        statement: str,
        proof: str,
        fact_id: str,
    ) -> Optional[Tuple[str, str]]:
        """Return the exact short/full identity under the caller's graph lock."""

        raw = self._get_raw_unchecked(fact_id)
        if raw is None:
            return None
        frontmatter = parse_frontmatter(raw)
        self._validate_fact_integrity(fact_id, raw, frontmatter)
        existing_problem_id = frontmatter.get("problem_id")
        existing_predecessors = frontmatter.get("predecessors")
        existing_glossary = frontmatter.get("glossary_introduces")
        if (
            not isinstance(existing_problem_id, str)
            or not isinstance(existing_predecessors, list)
            or any(not isinstance(item, str) for item in existing_predecessors)
            or not isinstance(existing_glossary, dict)
            or any(
                not isinstance(term, str) or not isinstance(definition, str)
                for term, definition in existing_glossary.items()
            )
        ):
            raise ValueError(
                f"fact_integrity_error: malformed identity fields for {fact_id}"
            )
        candidate_identity = self._canonical_fact_identity_unlocked(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
            fact_id=fact_id,
        )
        existing_identity = self._canonical_fact_identity_unlocked(
            problem_id=existing_problem_id,
            predecessors=existing_predecessors,
            glossary_introduces=existing_glossary,  # type: ignore[arg-type]
            statement=statement_of(raw),
            proof=_proof_of(raw),
            fact_id=fact_id,
        )
        recorded_identity = frontmatter.get("fact_identity")
        if recorded_identity is not None and recorded_identity != existing_identity:
            raise ValueError(
                f"fact_integrity_error: full identity mismatch for {fact_id}"
            )
        if candidate_identity != existing_identity:
            raise ValueError(
                "fact_identity_collision: short fact_id "
                f"{fact_id} maps to a different full identity"
            )
        return fact_id, existing_identity

    def _active_fact_identity_unlocked(self, fact_id: str) -> Optional[str]:
        """Return one active fact's authenticated full identity under graph lock."""

        if (
            not isinstance(fact_id, str)
            or _CONTENT_FACT_ID_RE.fullmatch(fact_id) is None
        ):
            raise ValueError("fact_id must be a 16-hex content identity")
        raw = self._get_raw_unchecked(fact_id)
        if raw is None:
            return None
        frontmatter = parse_frontmatter(raw)
        self._validate_fact_integrity(fact_id, raw, frontmatter)
        problem_id = frontmatter.get("problem_id")
        predecessors = frontmatter.get("predecessors")
        glossary_introduces = frontmatter.get("glossary_introduces")
        if (
            not isinstance(problem_id, str)
            or not isinstance(predecessors, list)
            or any(not isinstance(item, str) for item in predecessors)
            or not isinstance(glossary_introduces, dict)
            or any(
                not isinstance(term, str) or not isinstance(definition, str)
                for term, definition in glossary_introduces.items()
            )
        ):
            raise ValueError(
                f"fact_integrity_error: malformed identity fields for {fact_id}"
            )
        identity = self._canonical_fact_identity_unlocked(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,  # type: ignore[arg-type]
            statement=statement_of(raw),
            proof=_proof_of(raw),
            fact_id=fact_id,
        )
        recorded_identity = frontmatter.get("fact_identity")
        if recorded_identity is not None and recorded_identity != identity:
            raise ValueError(
                f"fact_integrity_error: full identity mismatch for {fact_id}"
            )
        return identity

    def lookup_active_exact_identity(
        self,
        *,
        problem_id: str,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> Optional[Tuple[str, str]]:
        """Return exact short/full identity from one linearizable graph snapshot."""

        with self.locked_active_exact_identity(
            problem_id=problem_id,
            statement=statement,
            proof=proof,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
        ) as identity:
            return identity

    @contextmanager
    def locked_active_exact_identity(
        self,
        *,
        problem_id: str,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> Iterator[Optional[Tuple[str, str]]]:
        """Yield an exact identity while retaining the shared graph lock."""

        roots = [item for item in (predecessors or []) if item]
        definitions = dict(glossary_introduces or {})
        fact_id = compute_fact_id(
            problem_id=problem_id,
            predecessors=roots,
            glossary_introduces=definitions,
            statement=statement,
            proof=proof,
        )
        with self._snapshot_lock():
            yield self._lookup_active_exact_identity_unlocked(
                problem_id=problem_id,
                predecessors=roots,
                glossary_introduces=definitions,
                statement=statement,
                proof=proof,
                fact_id=fact_id,
            )

    def lookup_active_exact(
        self,
        *,
        problem_id: str,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Find an exact active fact without mutating graph or fact metadata.

        The active-path test, revocation exclusion, short/full identity check,
        dependency reads, and integrity validation all occur under one shared
        graph snapshot.  A revoked or absent candidate returns ``None``.  An
        active short-id collision fails closed instead of being overwritten.
        """

        identity = self.lookup_active_exact_identity(
            problem_id=problem_id,
            statement=statement,
            proof=proof,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
        )
        return identity[0] if identity is not None else None

    @contextmanager
    def locked_active_fact_identity(self, fact_id: str) -> Iterator[Optional[str]]:
        """Yield an active full identity while retaining the shared graph lock."""

        with self._snapshot_lock():
            yield self._active_fact_identity_unlocked(fact_id)

    # ------------------------------------------------------------------ write
    def add(
        self,
        *,
        problem_id: str,
        author: str,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
        intuition: str = "",
        external_refs: Optional[List[Dict[str, object]]] = None,
    ) -> str:
        """Write a verified fact; return its content-addressed fact_id.

        Refuses an unknown or revoked predecessor (DAG integrity). Idempotent:
        identical content -> identical id -> identical file. Merges the fact's
        introduced symbols into the project glossary. ``external_refs`` is
        structured bibliography for cited external results; it does NOT affect
        the fact_id (mutable metadata — see ``compute_fact_id``).
        """
        with self._mutation_lock():
            self._recover_pending_transactions_unlocked()
            return self._add_unlocked(
                problem_id=problem_id,
                author=author,
                statement=statement,
                proof=proof,
                predecessors=predecessors,
                glossary_introduces=glossary_introduces,
                intuition=intuition,
                external_refs=external_refs,
            )

    def _add_unlocked(
        self,
        *,
        problem_id: str,
        author: str,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
        intuition: str = "",
        external_refs: Optional[List[Dict[str, object]]] = None,
    ) -> str:
        """Implementation for callers already holding ``_mutation_lock``."""
        if (
            not isinstance(problem_id, str)
            or not problem_id
            or problem_id != problem_id.strip()
            or _LINE_BREAK_RE.search(problem_id)
        ):
            raise ValueError("problem_id must be a non-empty single-line string")
        if not isinstance(author, str) or _LINE_BREAK_RE.search(author):
            raise ValueError("author must be a single-line string")
        if not isinstance(statement, str) or not isinstance(proof, str):
            raise ValueError("statement and proof must be strings")
        if any(line.strip().lower() == "## proof" for line in statement.splitlines()):
            raise ValueError(
                "statement may not contain the reserved '## proof' boundary"
            )
        if not isinstance(intuition, str):
            raise ValueError("intuition must be a string")
        if any(
            line.strip().lower() == "## intuition" for line in intuition.splitlines()
        ):
            raise ValueError(
                "intuition may not contain the reserved '## intuition' boundary"
            )
        predecessors = [p for p in (predecessors or []) if p]
        if len(predecessors) != len(set(predecessors)):
            raise ValueError("duplicate predecessor fact_id")
        glossary_introduces = glossary_introduces or {}
        if not isinstance(glossary_introduces, dict) or any(
            not isinstance(symbol, str) or not isinstance(definition, str)
            for symbol, definition in glossary_introduces.items()
        ):
            raise ValueError("glossary_introduces must map strings to strings")
        external_refs = clean_external_refs(external_refs)
        for pid in predecessors:
            if self._revoked_path(pid).exists():
                raise ValueError(f"predecessor_revoked: {pid}")
            if not self._path(pid).exists():
                raise ValueError(f"predecessor_unknown: {pid}")
        fact_id = compute_fact_id(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
        )
        if self._revoked_path(fact_id).exists():
            raise ValueError(f"fact_revoked: {fact_id}")
        existing = self._lookup_active_exact_identity_unlocked(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
            fact_id=fact_id,
        )
        if existing is not None:
            # The full semantic identity deliberately excludes author,
            # intuition, and mutable bibliography.  Exact active reuse is a
            # read: never rewrite those fields or perturb the fact mtime.
            return existing[0]
        fact_identity = self._canonical_fact_identity_unlocked(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
            fact_id=fact_id,
        )
        merged_glossary = self._prepare_glossary_merge(glossary_introduces)
        fact = Fact(
            fact_id=fact_id,
            problem_id=problem_id,
            author=author,
            predecessors=predecessors,
            statement=statement,
            proof=proof,
            glossary_introduces=glossary_introduces,
            intuition=intuition,
            fact_identity=fact_identity,
            external_refs=external_refs,
        )
        fact_path = self._path(fact_id)
        serialized = serialize_fact(fact)
        self._mkdir_durable(self.facts_dir)

        # Every add, including one that leaves the glossary untouched, follows
        # the same recoverable transaction protocol.  The prepared journal is
        # durable before data changes; the separate committed marker becomes
        # durable only after the exact fact/glossary bytes.  Cleanup deliberately
        # removes the prepared snapshot first, so any crash state is unambiguous:
        # prepared alone rolls back, while a committed marker always preserves.
        previous_fact = self._get_raw_unchecked(fact_id)
        glossary_existed = self.glossary_path.exists()
        previous_glossary_text = (
            self.glossary_path.read_text(encoding="utf-8") if glossary_existed else None
        )
        if glossary_introduces:
            expected_glossary_text = (
                json.dumps(
                    dict(sorted(merged_glossary.items())),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        else:
            expected_glossary_text = previous_glossary_text
        transaction_id = uuid.uuid4().hex
        pending_add = {
            "schema_version": 2,
            "state": "prepared",
            "transaction_id": transaction_id,
            "fact_id": fact_id,
            "previous_fact": previous_fact,
            "previous_glossary_text": previous_glossary_text,
            "expected_fact_sha256": hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest(),
            "expected_glossary_sha256": (
                hashlib.sha256(expected_glossary_text.encode("utf-8")).hexdigest()
                if expected_glossary_text is not None
                else None
            ),
        }
        committed_add = {
            key: value
            for key, value in pending_add.items()
            if key
            in {
                "schema_version",
                "transaction_id",
                "fact_id",
                "expected_fact_sha256",
                "expected_glossary_sha256",
            }
        }
        committed_add["state"] = "committed"
        self._atomic_write_text(
            self.pending_add_path,
            self._pending_add_text(pending_add),
        )
        try:
            self._atomic_write_text(fact_path, serialized)
            if glossary_introduces:
                self._write_project_glossary_atomic(merged_glossary)
        except Exception:
            try:
                self._rollback_pending_add_unlocked()
            except Exception as recovery_error:
                raise RuntimeError(
                    "fact_graph_recovery_required: add rollback did not complete"
                ) from recovery_error
            raise
        try:
            self._atomic_write_text(
                self.pending_add_commit_path,
                self._pending_add_text(committed_add),
            )
        except Exception:
            # A failed directory fsync can leave the new marker visible without
            # proving it durable.  Record rollback intent first so even a commit
            # marker that cannot be removed remains fail closed.
            abort_add = {
                "schema_version": 1,
                "state": "rollback_required",
                "transaction_id": transaction_id,
                "fact_id": fact_id,
            }
            try:
                self._atomic_write_text(
                    self.pending_add_abort_path,
                    self._pending_add_text(abort_add),
                )
            except Exception as marker_error:
                try:
                    visible_abort = self._load_pending_add_abort_unlocked()
                except Exception as recovery_error:
                    raise FactPromotionOutcomeUnknown(
                        "fact_graph_promotion_unknown: rollback intent is uncertain"
                    ) from recovery_error
                if visible_abort != abort_add:
                    raise FactPromotionOutcomeUnknown(
                        "fact_graph_promotion_unknown: rollback intent is uncertain"
                    ) from marker_error
                # Readback proves only visibility, not crash durability.  A
                # successful directory fsync is required before a definitive
                # rollback response is safe.
                try:
                    self._fsync_directory(self.dir)
                except Exception as durability_error:
                    raise FactPromotionOutcomeUnknown(
                        "fact_graph_promotion_unknown: rollback intent is not durable"
                    ) from durability_error
            try:
                self._recover_aborted_add_unlocked(abort_add)
            except Exception as recovery_error:
                raise RuntimeError(
                    "fact_graph_recovery_required: add rollback did not complete"
                ) from recovery_error
            raise
        try:
            self._cleanup_committed_add_unlocked()
        except Exception:
            # The commit marker is already durable.  Cleanup is never allowed to
            # turn a published fact into an error response; a residual marker is
            # validated by readers and finalized by a later mutation.
            pass
        return fact_id

    def add_if_context_unchanged(
        self,
        *,
        expected_context: Dict[str, object],
        context_max_chars: Optional[int],
        context_glossary_texts: Optional[List[str]] = None,
        context_glossary_exclude_terms: Optional[List[str]] = None,
        problem_id: str,
        author: str,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
        intuition: str = "",
        external_refs: Optional[List[Dict[str, object]]] = None,
    ) -> str:
        """Atomically compare verification context and add the accepted fact.

        Revoke uses the same cross-process lock. Thus either revoke runs first and
        this comparison fails, or this add runs first and revoke's subsequent
        descendant snapshot includes the newly written node.
        """
        roots = [p for p in (predecessors or []) if p]
        with self._mutation_lock():
            self._recover_pending_transactions_unlocked()
            scope = expected_context.get("scope", {})
            if not isinstance(scope, dict):
                raise ValueError(
                    "verification_context_changed: malformed expected scope"
                )
            if (
                expected_context.get("schema_version")
                == VERIFICATION_CONTEXT_SCHEMA_VERSION
                and scope.get("projection") == VERIFICATION_CONTEXT_PROJECTION
            ):
                expanded_proof_ids = scope.get("expanded_proof_ids")
                expansion_round = scope.get("expansion_round")
                candidate_fact_id = scope.get("candidate_fact_id")
                expanded_proof_max_chars = expected_context.get(
                    "expanded_proof_character_budget"
                )
                if (
                    not isinstance(expanded_proof_ids, list)
                    or not isinstance(expansion_round, int)
                    or not isinstance(candidate_fact_id, str)
                ):
                    raise ValueError(
                        "verification_context_changed: malformed adaptive scope"
                    )
                actual_candidate_fact_id = compute_fact_id(
                    problem_id=problem_id,
                    predecessors=roots,
                    glossary_introduces=glossary_introduces or {},
                    statement=statement,
                    proof=proof,
                )
                if candidate_fact_id != actual_candidate_fact_id:
                    raise ValueError(
                        "verification_context_changed: candidate content does not "
                        "match the verified adaptive scope"
                    )
                current_context = self._verification_context_unlocked(
                    roots,
                    max_chars=context_max_chars,
                    candidate_fact_id=candidate_fact_id,
                    expanded_proof_ids=expanded_proof_ids,
                    expansion_round=expansion_round,
                    expanded_proof_max_chars=expanded_proof_max_chars,
                    glossary_texts=context_glossary_texts,
                    glossary_exclude_terms=context_glossary_exclude_terms,
                )
            else:
                current_context = self._context_unlocked(
                    roots,
                    predecessor_depth=None,
                    proof_mode="none",
                    max_chars=context_max_chars,
                    glossary_texts=context_glossary_texts,
                    glossary_exclude_terms=context_glossary_exclude_terms,
                    include_project_glossary=bool(
                        scope.get("include_project_glossary", True)
                    ),
                )
            if current_context != expected_context:
                raise ValueError(
                    "verification_context_changed: a predecessor was changed, "
                    "revoked, removed, or became unavailable; retry verification"
                )
            return self._add_unlocked(
                problem_id=problem_id,
                author=author,
                statement=statement,
                proof=proof,
                predecessors=roots,
                glossary_introduces=glossary_introduces,
                intuition=intuition,
                external_refs=external_refs,
            )

    def _read_project_glossary(self, *, strict: bool) -> Dict[str, str]:
        if not self.glossary_path.exists():
            return {}
        try:
            loaded = json.loads(self.glossary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            if strict:
                raise ValueError(
                    "glossary_integrity_error: project glossary is unreadable"
                ) from exc
            return {}
        if not isinstance(loaded, dict) or any(
            not isinstance(term, str) or not isinstance(definition, str)
            for term, definition in loaded.items()
        ):
            if strict:
                raise ValueError(
                    "glossary_integrity_error: project glossary must map strings to strings"
                )
            return {}
        global_definitions = _glossary.global_glossary()
        shadow_conflicts = sorted(
            term
            for term, definition in loaded.items()
            if term in global_definitions and global_definitions[term] != definition
        )
        if shadow_conflicts:
            if strict:
                raise ValueError(
                    "glossary_integrity_error: project glossary conflicts with "
                    "global terms: " + ", ".join(shadow_conflicts)
                )
            return {
                term: definition
                for term, definition in loaded.items()
                if term not in shadow_conflicts
            }
        return dict(loaded)

    @staticmethod
    def _glossary_conflicts_against(
        current: Dict[str, str], new: Dict[str, str]
    ) -> List[str]:
        """Return terms whose proposed definitions change established semantics."""
        global_definitions = _glossary.global_glossary()
        return sorted(
            term
            for term, definition in new.items()
            if (
                (term in current and current[term] != definition)
                or (
                    term in global_definitions
                    and global_definitions[term] != definition
                )
            )
        )

    def glossary_conflicts(
        self, glossary_introduces: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """Read known glossary conflicts from one linearizable graph snapshot.

        This is a read-only preflight for callers that would otherwise pay for
        work before :meth:`add_if_context_unchanged`.  It is deliberately not a
        write authorization: a concurrent definition may appear after this
        shared snapshot, so promotion must still repeat the check under the
        exclusive mutation lock.
        """
        proposed = {} if glossary_introduces is None else glossary_introduces
        if not isinstance(proposed, dict) or any(
            not isinstance(term, str) or not isinstance(definition, str)
            for term, definition in proposed.items()
        ):
            raise ValueError("glossary_introduces must map strings to strings")
        with self._snapshot_lock():
            current = self._read_project_glossary(strict=True)
            return self._glossary_conflicts_against(current, proposed)

    def _prepare_glossary_merge(self, new: Dict[str, str]) -> Dict[str, str]:
        """Validate a stable project glossary and reject semantic redefinition."""
        current = self._read_project_glossary(strict=True)
        conflicts = self._glossary_conflicts_against(current, new)
        if conflicts:
            raise ValueError(
                "glossary_conflict: refusing to redefine project terms: "
                + ", ".join(conflicts)
            )
        merged = dict(current)
        merged.update(new)
        return merged

    def _write_project_glossary_atomic(self, glossary: Dict[str, str]) -> None:
        """Durably replace the derived project glossary under the mutation lock."""
        self._atomic_write_text(
            self.glossary_path,
            json.dumps(dict(sorted(glossary.items())), ensure_ascii=False, indent=2)
            + "\n",
        )

    def _active_glossary_excluding(self, excluded_ids: Set[str]) -> Dict[str, str]:
        """Rebuild glossary state from canonical active fact-local definitions."""
        rebuilt: Dict[str, str] = {}
        for active_id in self._list_unchecked():
            if active_id in excluded_ids:
                continue
            raw = self._get_raw_unchecked(active_id)
            if raw is None:
                raise ValueError(
                    f"fact_integrity_error: missing active fact {active_id}"
                )
            frontmatter = parse_frontmatter(raw)
            self._validate_fact_integrity(active_id, raw, frontmatter)
            local = frontmatter["glossary_introduces"]
            if not isinstance(local, dict):
                raise ValueError(
                    f"fact_integrity_error: invalid glossary for {active_id}"
                )
            for term, definition in sorted(local.items()):
                if not isinstance(term, str) or not isinstance(definition, str):
                    raise ValueError(
                        f"fact_integrity_error: invalid glossary for {active_id}"
                    )
                prior = rebuilt.get(term)
                if prior is not None and prior != definition:
                    raise ValueError(
                        "glossary_integrity_error: conflicting active definitions "
                        f"for {term}"
                    )
                rebuilt[term] = definition
        return rebuilt

    def glossary_predecessor_gaps(
        self,
        *,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Advisory lexical check for likely missing definition-source edges.

        This must never be used as a correctness gate: mathematically equivalent
        notation (for example ``K_F`` versus ``K_{F}``) is not mechanically
        recognizable. Verification therefore excludes the mutable project
        glossary entirely; inherited definitions arrive only on declared fact
        cards. A fact may use terms it introduces itself without a predecessor.
        """
        with self._snapshot_lock():
            return self._glossary_predecessor_gaps_unlocked(
                statement=statement,
                proof=proof,
                predecessors=predecessors,
                glossary_introduces=glossary_introduces,
            )

    def _glossary_predecessor_gaps_unlocked(
        self,
        *,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        project_glossary = self._read_project_glossary(strict=True)
        introduced = set(glossary_introduces or {})
        predecessor_definitions: Dict[str, Set[str]] = {}
        for predecessor_id in predecessors or []:
            if self._revoked_path(predecessor_id).exists():
                raise ValueError(f"predecessor_revoked: {predecessor_id}")
            raw = self._get_raw_unchecked(predecessor_id)
            if raw is None:
                raise ValueError(f"predecessor_unknown: {predecessor_id}")
            frontmatter = parse_frontmatter(raw)
            self._validate_fact_integrity(predecessor_id, raw, frontmatter)
            local = frontmatter["glossary_introduces"]
            if not isinstance(local, dict):
                raise ValueError(
                    f"fact_integrity_error: invalid glossary for {predecessor_id}"
                )
            for term, definition in local.items():
                if isinstance(term, str) and isinstance(definition, str):
                    predecessor_definitions.setdefault(term, set()).add(definition)

        texts = [statement, proof, *(glossary_introduces or {}).values()]
        gaps = []
        for term, definition in project_glossary.items():
            if term in introduced:
                continue
            if not any(_term_occurs(term, text) for text in texts if text):
                continue
            if definition not in predecessor_definitions.get(term, set()):
                gaps.append(term)
        return sorted(gaps)

    # ------------------------------------------------------------------- read
    def _exists_unchecked(self, fact_id: str) -> bool:
        try:
            return self._path(fact_id).exists()
        except ValueError:
            return False

    def exists(self, fact_id: str) -> bool:
        with self._snapshot_lock():
            return self._exists_unchecked(fact_id)

    def _list_unchecked(self) -> List[str]:
        if not self.facts_dir.exists():
            return []
        return sorted(p.stem for p in self.facts_dir.glob("*.md"))

    def list(self) -> List[str]:
        with self._snapshot_lock():
            return self._list_unchecked()

    def _get_raw_unchecked(self, fact_id: str) -> Optional[str]:
        path = self._path(fact_id)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def get_raw(self, fact_id: str) -> Optional[str]:
        """The fact's markdown (agents read markdown directly)."""
        with self._snapshot_lock():
            return self._get_raw_unchecked(fact_id)

    def glossary(self) -> Dict[str, str]:
        """The accumulated project glossary (symbol -> definition)."""
        with self._snapshot_lock():
            return self._read_project_glossary(strict=False)

    def search(self, query: str, limit: int = 10) -> List[Dict[str, object]]:
        """BM25 over the fact bodies (statement + proof + intuition + glossary),
        the derived fact index rebuilt **on demand** from ``facts/*.md`` — no
        persisted board, so no double-write drift (DATA_MODEL.md §3). Returns the
        top matches as ``{fact_id, score, statement}`` for novelty checks ("does a
        fact like this already exist?") and citation lookup ("which verified facts
        bear on my subgoal?"). The fact graph stays the single source of truth;
        this is just a read view over it."""
        with self._snapshot_lock():
            return self._search_unlocked(query, limit=limit)

    def _search_unlocked(self, query: str, limit: int = 10) -> List[Dict[str, object]]:
        fids = self._list_unchecked()
        if not fids:
            return []
        raws = [self._get_raw_unchecked(fid) or "" for fid in fids]
        docs = [bm25.tokenize(r) for r in raws]
        scores = bm25.bm25_scores(query, docs)
        ranked: List[Dict[str, object]] = []
        for fid, raw, score in sorted(zip(fids, raws, scores), key=lambda t: -t[2]):
            if score <= 0:
                break
            ranked.append(
                {"fact_id": fid, "score": score, "statement": statement_of(raw)}
            )
            if len(ranked) >= limit:
                break
        return ranked

    def _resolved_glossary_for_texts(
        self,
        texts: List[str],
        excluded_terms: Optional[Set[str]] = None,
        *,
        include_project_glossary: bool = True,
    ) -> Dict[str, str]:
        """Resolve only project/global definitions actually mentioned by text.

        Interactive callers may include the project discovery index; verifier
        callers disable it and retain only the packaged immutable global
        glossary. Definitions are followed transitively: if the selected
        definition of ``X`` itself mentions ``Y``, ``Y`` is included too. Literal
        matching avoids injecting a whole glossary and is advisory, never proof
        of semantic coverage.
        """
        available = dict(_glossary.global_glossary())
        if include_project_glossary:
            available.update(self._read_project_glossary(strict=True))
        excluded_terms = excluded_terms or set()
        remaining = {
            str(term): str(definition)
            for term, definition in available.items()
            if str(term) and str(term) not in excluded_terms
        }
        return select_referenced_definitions(texts, remaining)

    def context(
        self,
        fact_ids: List[str],
        predecessor_depth: Optional[int] = 0,
        proof_mode: str = "none",
        max_chars: Optional[int] = None,
        glossary_texts: Optional[List[str]] = None,
        glossary_exclude_terms: Optional[List[str]] = None,
        include_project_glossary: bool = True,
    ) -> Dict[str, object]:
        """Return deterministic, lazy context for explicit fact ids.

        ``predecessor_depth`` is the number of predecessor hops to hydrate; use
        ``None`` for the full transitive closure. Proofs are omitted by default,
        included only for the explicitly requested roots in ``selected`` mode,
        and included for every hydrated record in ``all`` mode. The character
        budget charges complete compact-JSON records. Once the next record does
        not fit, that record and every lower-priority record are reported as
        omitted; a fact or glossary definition is never sliced. Definitions from
        the project/global glossaries are selected only when their notation occurs
        in a hydrated statement or ``glossary_texts`` (and then followed through
        dependent definitions). ``glossary_exclude_terms`` names higher-precedence
        candidate definitions that must not be duplicated from lower layers.
        Project glossary entries are discovery metadata, not stable mathematical
        premises: verifier callers set ``include_project_glossary=False`` and
        inherit definitions only through fact-local cards in the declared DAG
        (plus the immutable packaged global glossary).

        Traversal is breadth-first from the de-duplicated roots in caller order,
        so all requested roots retain priority over ancestors. It never lists or
        scans the graph: only reachable active fact files are read, with exact
        revoked-path existence checks for unreachable nodes.
        """
        with self._snapshot_lock():
            return self._context_unlocked(
                fact_ids,
                predecessor_depth=predecessor_depth,
                proof_mode=proof_mode,
                max_chars=max_chars,
                glossary_texts=glossary_texts,
                glossary_exclude_terms=glossary_exclude_terms,
                include_project_glossary=include_project_glossary,
            )

    @contextmanager
    def locked_context(
        self,
        fact_ids: List[str],
        predecessor_depth: Optional[int] = 0,
        proof_mode: str = "none",
        max_chars: Optional[int] = None,
        glossary_texts: Optional[List[str]] = None,
        glossary_exclude_terms: Optional[List[str]] = None,
        include_project_glossary: bool = True,
    ) -> Iterator[Dict[str, object]]:
        """Yield a verified context while retaining the shared snapshot lock.

        This narrow transactional seam lets a caller append metadata whose fact
        links must remain active through the append itself.  Callers must not
        acquire a FactGraph mutation lock from inside the block.  The supported
        lock order is FactGraph shared snapshot, then an independent append-only
        store lock; no GlobalMemory path acquires a FactGraph lock.
        """

        with self._snapshot_lock():
            yield self._context_unlocked(
                fact_ids,
                predecessor_depth=predecessor_depth,
                proof_mode=proof_mode,
                max_chars=max_chars,
                glossary_texts=glossary_texts,
                glossary_exclude_terms=glossary_exclude_terms,
                include_project_glossary=include_project_glossary,
            )

    def _context_unlocked(
        self,
        fact_ids: List[str],
        predecessor_depth: Optional[int] = 0,
        proof_mode: str = "none",
        max_chars: Optional[int] = None,
        glossary_texts: Optional[List[str]] = None,
        glossary_exclude_terms: Optional[List[str]] = None,
        include_project_glossary: bool = True,
    ) -> Dict[str, object]:
        if predecessor_depth is not None and (
            isinstance(predecessor_depth, bool)
            or not isinstance(predecessor_depth, int)
            or predecessor_depth < 0
        ):
            raise ValueError("predecessor_depth must be a non-negative integer or None")
        if proof_mode not in ("none", "selected", "all"):
            raise ValueError("proof_mode must be one of: none, selected, all")
        if max_chars is not None and (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars < 0
        ):
            raise ValueError("max_chars must be a non-negative integer or None")
        if not isinstance(fact_ids, list) or any(
            not isinstance(fid, str) for fid in fact_ids
        ):
            raise ValueError("fact_ids must be a list of strings")
        if glossary_texts is not None and (
            not isinstance(glossary_texts, list)
            or any(not isinstance(text, str) for text in glossary_texts)
        ):
            raise ValueError("glossary_texts must be a list of strings or None")
        if glossary_exclude_terms is not None and (
            not isinstance(glossary_exclude_terms, list)
            or any(not isinstance(term, str) for term in glossary_exclude_terms)
        ):
            raise ValueError("glossary_exclude_terms must be a list of strings or None")
        if not isinstance(include_project_glossary, bool):
            raise ValueError("include_project_glossary must be a boolean")

        roots: List[str] = []
        seen_roots: Set[str] = set()
        for fact_id in fact_ids:
            if fact_id and fact_id not in seen_roots:
                roots.append(fact_id)
                seen_roots.add(fact_id)

        selected = set(roots)
        queue: Deque[Tuple[str, int]] = deque((fact_id, 0) for fact_id in roots)
        discovered: Set[str] = set(roots)
        records: List[Dict[str, object]] = []
        missing: List[str] = []
        revoked: List[str] = []
        glossary_reference_texts = list(glossary_texts or [])
        local_glossary_terms = set(glossary_exclude_terms or [])

        while queue:
            fact_id, depth = queue.popleft()
            raw = self._get_raw_unchecked(fact_id)
            if raw is None:
                if self._revoked_path(fact_id).exists():
                    revoked.append(fact_id)
                else:
                    missing.append(fact_id)
                continue

            frontmatter = parse_frontmatter(raw)
            self._validate_fact_integrity(fact_id, raw, frontmatter)
            predecessors = frontmatter["predecessors"]
            record: Dict[str, object] = {
                "fact_id": fact_id,
                "statement": statement_of(raw),
                "predecessors": predecessors,
                "glossary_introduces": frontmatter["glossary_introduces"],
            }
            glossary_reference_texts.append(str(record["statement"]))
            local_glossary = record["glossary_introduces"]
            if isinstance(local_glossary, dict):
                local_glossary_terms.update(str(k) for k in local_glossary)
                glossary_reference_texts.extend(str(v) for v in local_glossary.values())
            if proof_mode == "all" or (
                proof_mode == "selected" and fact_id in selected
            ):
                proof_text = _proof_of(raw)
                record["proof"] = proof_text
                glossary_reference_texts.append(proof_text)
            records.append(record)

            if predecessor_depth is None or depth < predecessor_depth:
                for predecessor in predecessors:  # type: ignore[union-attr]
                    if predecessor not in discovered:
                        discovered.add(predecessor)
                        queue.append((predecessor, depth + 1))

        resolved_glossary = self._resolved_glossary_for_texts(
            glossary_reference_texts,
            local_glossary_terms,
            include_project_glossary=include_project_glossary,
        )
        glossary_terms = list(resolved_glossary)
        included: List[Dict[str, object]] = []
        omitted: List[str] = []
        characters_used = 0
        for index, record in enumerate(records):
            record_chars = len(
                json.dumps(
                    record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            if max_chars is not None and characters_used + record_chars > max_chars:
                omitted = [str(item["fact_id"]) for item in records[index:]]
                break
            included.append(record)
            characters_used += record_chars

        included_glossary: Dict[str, str] = {}
        omitted_glossary_terms: List[str] = []
        if omitted:
            omitted_glossary_terms = glossary_terms
        else:
            for index, term in enumerate(glossary_terms):
                definition = resolved_glossary[term]
                glossary_chars = len(
                    json.dumps(
                        {"term": term, "definition": definition},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                if (
                    max_chars is not None
                    and characters_used + glossary_chars > max_chars
                ):
                    omitted_glossary_terms = glossary_terms[index:]
                    break
                included_glossary[term] = definition
                characters_used += glossary_chars

        truncated = bool(omitted or omitted_glossary_terms)
        scope: Dict[str, object] = {
            "requested_fact_ids": roots,
            "predecessor_depth": predecessor_depth,
            "proof_mode": proof_mode,
            "include_project_glossary": include_project_glossary,
            "glossary_terms": glossary_terms,
        }
        return {
            "schema_version": FACT_CONTEXT_SCHEMA_VERSION,
            "scope": scope,
            "facts": included,
            "glossary": included_glossary,
            "digest": fact_context_digest(
                scope=scope, facts=included, glossary=included_glossary
            ),
            "complete": not (missing or revoked or omitted or omitted_glossary_terms),
            "truncated": truncated,
            "missing_fact_ids": missing,
            "revoked_fact_ids": revoked,
            "omitted_fact_ids": omitted,
            "omitted_glossary_terms": omitted_glossary_terms,
            "characters_used": characters_used,
            "character_budget": max_chars,
        }

    def verification_context(
        self,
        fact_ids: List[str],
        *,
        max_chars: Optional[int],
        candidate_fact_id: Optional[str] = None,
        expanded_proof_ids: Optional[List[str]] = None,
        expansion_round: int = 0,
        expanded_proof_max_chars: Optional[int] = 200000,
        glossary_texts: Optional[List[str]] = None,
        glossary_exclude_terms: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        """Build the adaptive full-closure context used only by ``fact_submit``.

        Round zero contains every transitive ancestor's statement, direct edges,
        and fact-local definitions, but no ancestor proof. Later rounds hydrate
        only the explicitly named closure facts, always as whole proof records.
        Traversal is a de-duplicating breadth-first walk, hence graph construction
        is ``O(V + E)`` even for deep or diamond-shaped closures. The public
        :meth:`context` API intentionally retains its v1 shape and proof modes.
        """
        with self._snapshot_lock():
            return self._verification_context_unlocked(
                fact_ids,
                max_chars=max_chars,
                candidate_fact_id=candidate_fact_id,
                expanded_proof_ids=expanded_proof_ids,
                expansion_round=expansion_round,
                expanded_proof_max_chars=expanded_proof_max_chars,
                glossary_texts=glossary_texts,
                glossary_exclude_terms=glossary_exclude_terms,
            )

    def _verification_context_unlocked(
        self,
        fact_ids: List[str],
        *,
        max_chars: Optional[int],
        candidate_fact_id: Optional[str] = None,
        expanded_proof_ids: Optional[List[str]] = None,
        expansion_round: int = 0,
        expanded_proof_max_chars: Optional[int] = 200000,
        glossary_texts: Optional[List[str]] = None,
        glossary_exclude_terms: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        if max_chars is not None and (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars < 0
        ):
            raise ValueError("max_chars must be a non-negative integer or None")
        if not isinstance(fact_ids, list) or any(
            not isinstance(fid, str) for fid in fact_ids
        ):
            raise ValueError("fact_ids must be a list of strings")
        if candidate_fact_id is not None and (
            not isinstance(candidate_fact_id, str)
            or not _CONTENT_FACT_ID_RE.fullmatch(candidate_fact_id)
        ):
            raise ValueError("candidate_fact_id must be a 16-hex fact_id or None")
        if expanded_proof_ids is None:
            expanded_proof_ids = []
        if not isinstance(expanded_proof_ids, list) or any(
            not isinstance(fid, str) for fid in expanded_proof_ids
        ):
            raise ValueError("expanded_proof_ids must be a list of strings")
        if len(expanded_proof_ids) != len(set(expanded_proof_ids)):
            raise ValueError("expanded_proof_ids contains duplicates")
        if any(not _CONTENT_FACT_ID_RE.fullmatch(fid) for fid in expanded_proof_ids):
            raise ValueError("expanded_proof_ids must contain only 16-hex fact_ids")
        if (
            isinstance(expansion_round, bool)
            or not isinstance(expansion_round, int)
            or expansion_round < 0
        ):
            raise ValueError("expansion_round must be a non-negative integer")
        if expansion_round == 0 and expanded_proof_ids:
            raise ValueError("round zero may not contain expanded proofs")
        if expansion_round > 0 and not expanded_proof_ids:
            raise ValueError("an expansion round requires at least one expanded proof")
        if expanded_proof_max_chars is not None and (
            isinstance(expanded_proof_max_chars, bool)
            or not isinstance(expanded_proof_max_chars, int)
            or expanded_proof_max_chars < 0
        ):
            raise ValueError(
                "expanded_proof_max_chars must be a non-negative integer or None"
            )
        if glossary_texts is not None and (
            not isinstance(glossary_texts, list)
            or any(not isinstance(text, str) for text in glossary_texts)
        ):
            raise ValueError("glossary_texts must be a list of strings or None")
        if glossary_exclude_terms is not None and (
            not isinstance(glossary_exclude_terms, list)
            or any(not isinstance(term, str) for term in glossary_exclude_terms)
        ):
            raise ValueError("glossary_exclude_terms must be a list of strings or None")

        roots: List[str] = []
        seen_roots: Set[str] = set()
        for fact_id in fact_ids:
            if fact_id and fact_id not in seen_roots:
                if not _CONTENT_FACT_ID_RE.fullmatch(fact_id):
                    raise ValueError(
                        "verification context requires 16-hex predecessor fact_ids"
                    )
                roots.append(fact_id)
                seen_roots.add(fact_id)

        queue: Deque[str] = deque(roots)
        discovered: Set[str] = set(roots)
        closure_order: List[str] = []
        full_records: List[Dict[str, object]] = []
        raw_by_id: Dict[str, str] = {}
        missing: List[str] = []
        revoked: List[str] = []

        while queue:
            fact_id = queue.popleft()
            closure_order.append(fact_id)
            if not _CONTENT_FACT_ID_RE.fullmatch(fact_id):
                raise ValueError(
                    "verification context closure contains a non-content fact_id: "
                    + fact_id
                )
            raw = self._get_raw_unchecked(fact_id)
            if raw is None:
                if self._revoked_path(fact_id).exists():
                    revoked.append(fact_id)
                else:
                    missing.append(fact_id)
                continue

            frontmatter = parse_frontmatter(raw)
            self._validate_fact_integrity(fact_id, raw, frontmatter)
            predecessors = frontmatter["predecessors"]
            if not isinstance(predecessors, list):
                raise ValueError(
                    f"fact_integrity_error: invalid predecessors for {fact_id}"
                )
            if any(
                not isinstance(predecessor, str)
                or not _CONTENT_FACT_ID_RE.fullmatch(predecessor)
                for predecessor in predecessors
            ):
                raise ValueError(
                    f"fact_integrity_error: non-content predecessor for {fact_id}"
                )
            local_glossary = frontmatter["glossary_introduces"]
            if not isinstance(local_glossary, dict):
                raise ValueError(
                    f"fact_integrity_error: invalid glossary for {fact_id}"
                )
            record: Dict[str, object] = {
                "fact_id": fact_id,
                "statement": statement_of(raw),
                "predecessors": predecessors,
                "glossary_introduces": local_glossary,
            }
            if not str(record["statement"]).strip():
                raise ValueError(
                    f"fact_integrity_error: closure fact {fact_id} has an empty statement"
                )
            full_records.append(record)
            raw_by_id[fact_id] = raw
            for predecessor in predecessors:
                if predecessor not in discovered:
                    discovered.add(predecessor)
                    queue.append(predecessor)

        outside_closure = [fid for fid in expanded_proof_ids if fid not in raw_by_id]
        if outside_closure:
            raise ValueError(
                "expanded proof ids are not in the verified dependency closure: "
                + ", ".join(outside_closure)
            )
        if candidate_fact_id is not None and candidate_fact_id in discovered:
            raise ValueError(
                "candidate_fact_id must not belong to its ancestor closure"
            )

        expanded_set = set(expanded_proof_ids)
        stable_expanded_ids = [fid for fid in closure_order if fid in expanded_set]
        expanded_proof_characters = 0
        omitted_expanded_proof_ids: List[str] = []
        expanded_proof_records: List[Dict[str, object]] = []
        glossary_reference_texts = list(glossary_texts or [])
        local_glossary_terms = set(glossary_exclude_terms or [])
        for record in full_records:
            fact_id = str(record["fact_id"])
            glossary_reference_texts.append(str(record["statement"]))
            local = record["glossary_introduces"]
            assert isinstance(local, dict)
            local_glossary_terms.update(str(term) for term in local)
            glossary_reference_texts.extend(str(value) for value in local.values())
        proof_characters_considered = 0
        for fact_id in stable_expanded_ids:
            raw = raw_by_id.get(fact_id)
            if raw is None:
                continue
            proof_text = _proof_of(raw)
            if not proof_text:
                raise ValueError(
                    f"fact_integrity_error: expanded fact {fact_id} has an empty proof"
                )
            proof_record = {"fact_id": fact_id, "proof": proof_text}
            proof_record_chars = len(
                json.dumps(
                    proof_record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if (
                expanded_proof_max_chars is not None
                and proof_characters_considered + proof_record_chars
                > expanded_proof_max_chars
            ):
                omitted_expanded_proof_ids.append(fact_id)
                continue
            proof_characters_considered += proof_record_chars
            expanded_proof_records.append(proof_record)
            glossary_reference_texts.append(proof_text)

        resolved_glossary = self._resolved_glossary_for_texts(
            glossary_reference_texts,
            local_glossary_terms,
            include_project_glossary=False,
        )
        all_glossary = dict(resolved_glossary)

        included_records: List[Dict[str, object]] = []
        included_glossary: Dict[str, str] = {}
        omitted_fact_ids: List[str] = []
        omitted_glossary_terms: List[str] = []
        characters_used = 0

        for index, record in enumerate(full_records):
            record_chars = len(
                json.dumps(
                    record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            if max_chars is not None and characters_used + record_chars > max_chars:
                omitted_fact_ids = [
                    str(item["fact_id"]) for item in full_records[index:]
                ]
                break
            included_records.append(record)
            characters_used += record_chars

        included_expanded_proofs: List[Dict[str, object]] = []
        if omitted_fact_ids:
            omitted_expanded_proof_ids = list(stable_expanded_ids)
            omitted_glossary_terms = list(all_glossary)
        else:
            for index, proof_record in enumerate(expanded_proof_records):
                proof_record_chars = len(
                    json.dumps(
                        proof_record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                if (
                    max_chars is not None
                    and characters_used + proof_record_chars > max_chars
                ):
                    omitted_expanded_proof_ids.extend(
                        str(item["fact_id"]) for item in expanded_proof_records[index:]
                    )
                    break
                included_expanded_proofs.append(proof_record)
                expanded_proof_characters += proof_record_chars
                characters_used += proof_record_chars

            if omitted_expanded_proof_ids:
                # Preserve closure order and uniqueness even when two independent
                # budgets reject proof records.
                omitted_set = set(omitted_expanded_proof_ids)
                omitted_expanded_proof_ids = [
                    fid for fid in stable_expanded_ids if fid in omitted_set
                ]
                omitted_glossary_terms = list(all_glossary)
            else:
                glossary_terms = list(all_glossary)
                for index, term in enumerate(glossary_terms):
                    definition = all_glossary[term]
                    definition_chars = len(
                        json.dumps(
                            {"term": term, "definition": definition},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    if (
                        max_chars is not None
                        and characters_used + definition_chars > max_chars
                    ):
                        omitted_glossary_terms = glossary_terms[index:]
                        break
                    included_glossary[term] = definition
                    characters_used += definition_chars

        truncated = bool(
            omitted_fact_ids or omitted_glossary_terms or omitted_expanded_proof_ids
        )
        scope: Dict[str, object] = {
            "candidate_fact_id": candidate_fact_id,
            "requested_fact_ids": roots,
            "predecessor_depth": None,
            "proof_mode": "adaptive",
            "include_project_glossary": False,
            "projection": VERIFICATION_CONTEXT_PROJECTION,
            "expansion_round": expansion_round,
            "closure_fact_ids": closure_order,
            "expanded_proof_ids": stable_expanded_ids,
            "glossary_terms": list(all_glossary),
        }
        context_without_digest: Dict[str, object] = {
            "schema_version": VERIFICATION_CONTEXT_SCHEMA_VERSION,
            "scope": scope,
            "facts": included_records,
            "expanded_proofs": included_expanded_proofs,
            "glossary": included_glossary,
            "complete": not (
                missing
                or revoked
                or omitted_fact_ids
                or omitted_glossary_terms
                or omitted_expanded_proof_ids
            ),
            "truncated": truncated,
            "missing_fact_ids": missing,
            "revoked_fact_ids": revoked,
            "omitted_fact_ids": omitted_fact_ids,
            "omitted_glossary_terms": omitted_glossary_terms,
            "omitted_expanded_proof_ids": omitted_expanded_proof_ids,
            "characters_used": characters_used,
            "character_budget": max_chars,
            "expanded_proof_characters": expanded_proof_characters,
            "expanded_proof_character_budget": expanded_proof_max_chars,
        }
        return {
            **context_without_digest,
            "digest": verification_context_digest(context=context_without_digest),
        }

    def predecessors(self, fact_id: str) -> List[str]:
        raw = self.get_raw(fact_id) or ""
        return parse_frontmatter(raw)["predecessors"]  # type: ignore[return-value]

    def external_refs(self, fact_id: str) -> List[Dict[str, object]]:
        """The fact's structured external bibliography (``[]`` if none / absent)."""
        raw = self.get_raw(fact_id) or ""
        return parse_frontmatter(raw)["external_refs"]  # type: ignore[return-value]

    def set_external_refs(
        self, fact_id: str, external_refs: List[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        """Replace a fact's ``external_refs`` in place — the reference auditor's
        write path. Touches only this mutable frontmatter line; the body and the
        content-addressed ``fact_id`` are unchanged (refs are not hashed). Returns
        the normalized refs written. Raises if the fact does not exist."""
        with self._mutation_lock():
            self._recover_pending_transactions_unlocked()
            return self._set_external_refs_unlocked(fact_id, external_refs)

    def _set_external_refs_unlocked(
        self, fact_id: str, external_refs: List[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        p = self._path(fact_id)
        if not p.exists():
            raise ValueError(f"unknown fact_id: {fact_id}")
        refs = clean_external_refs(external_refs)
        new_line = "external_refs: " + json.dumps(refs, ensure_ascii=False)
        lines = p.read_text(encoding="utf-8").splitlines()
        # frontmatter is between the first '---' (line 0) and the next '---'
        close = next(
            (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
        )
        if close is None:
            raise ValueError(f"malformed fact file (no frontmatter close): {fact_id}")
        idx = next(
            (i for i in range(1, close) if lines[i].startswith("external_refs:")), None
        )
        if idx is not None:
            lines[idx] = new_line
        else:
            lines.insert(close, new_line)  # facts written before the field existed
        self._atomic_write_text(p, "\n".join(lines) + "\n")
        return refs

    def descendants(self, fact_id: str) -> List[str]:
        """All facts that (transitively) depend on ``fact_id``."""
        with self._snapshot_lock():
            return self._descendants_unchecked(
                fact_id, active_ids=self._list_unchecked()
            )

    def _descendants_unchecked(
        self, fact_id: str, *, active_ids: Optional[List[str]] = None
    ) -> List[str]:
        # Build the reverse graph once from one deterministic active-file
        # snapshot.  The previous implementation re-listed and re-read every
        # active fact for every visited descendant, making a chain quadratic in
        # filesystem I/O (and holding the mutation lock for that entire time
        # when called by ``revoke``).
        reverse_edges: Dict[str, List[str]] = {}
        for active_id in self._list_unchecked() if active_ids is None else active_ids:
            raw = self._get_raw_unchecked(active_id)
            if raw is None:
                # A read-only caller can race a cross-process revoke between the
                # directory snapshot and this read.  ``revoke`` itself holds the
                # mutation lock, so its canonical snapshot cannot hit this case.
                continue
            predecessors = parse_frontmatter(raw)["predecessors"]
            for predecessor_id in predecessors:  # type: ignore[union-attr]
                reverse_edges.setdefault(predecessor_id, []).append(active_id)

        out: List[str] = []
        seen = {fact_id}
        frontier = [fact_id]
        while frontier:
            cur = frontier.pop()
            for fid in reverse_edges.get(cur, []):
                if fid in seen:
                    continue
                out.append(fid)
                seen.add(fid)
                frontier.append(fid)
        return out

    # --------------------------------------------------------- glossary check
    def undefined_symbols(
        self,
        *,
        statement: str,
        proof: str,
        intuition: str = "",
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Symbols used in the body but defined nowhere available: (this fact's
        glossary) ∪ (each predecessor's glossary) ∪ (the project glossary) ∪ (the
        repo-wide global glossary of universal notation). Used by `fact submit` to
        keep the graph readable (advisory)."""
        with self._snapshot_lock():
            return self._undefined_symbols_unlocked(
                statement=statement,
                proof=proof,
                intuition=intuition,
                predecessors=predecessors,
                glossary_introduces=glossary_introduces,
            )

    def _undefined_symbols_unlocked(
        self,
        *,
        statement: str,
        proof: str,
        intuition: str = "",
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        defined = _glossary.global_terms()  # universal notation, all projects
        defined |= set(self._read_project_glossary(strict=False))
        defined |= set(glossary_introduces or {})
        for pid in predecessors or []:
            raw = self._get_raw_unchecked(pid)
            if raw:
                defined |= set(parse_frontmatter(raw)["glossary_introduces"])  # type: ignore[arg-type]
        return _glossary.undefined_symbols(
            statement=statement, proof=proof, intuition=intuition, defined=defined
        )

    # --------------------------------------------------------------- revoke
    def _load_pending_revocation_unlocked(self) -> Optional[Dict[str, object]]:
        if not self.pending_revocation_path.exists():
            return None
        try:
            payload = json.loads(
                self.pending_revocation_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                "fact_graph_recovery_error: unreadable pending revocation"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("fact_graph_recovery_error: malformed pending revocation")
        root_id = payload.get("root_fact_id")
        fact_ids = payload.get("fact_ids")
        if (
            payload.get("schema_version") != 1
            or not isinstance(payload.get("revocation_id"), str)
            or not isinstance(payload.get("timestamp_utc"), str)
            or not isinstance(payload.get("reason"), str)
            or not isinstance(root_id, str)
            or not _SAFE_FACT_ID_RE.fullmatch(root_id)
            or not isinstance(fact_ids, list)
            or not fact_ids
            or fact_ids[0] != root_id
            or len(fact_ids) != len(set(fact_ids))
            or any(
                not isinstance(item, str) or not _SAFE_FACT_ID_RE.fullmatch(item)
                for item in fact_ids
            )
        ):
            raise ValueError("fact_graph_recovery_error: malformed pending revocation")
        return payload

    def _read_revocation_log_strict(self) -> List[Dict[str, object]]:
        if not self.revocation_log.exists():
            return []
        try:
            raw = self.revocation_log.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("revocation_log_integrity_error: unreadable log") from exc
        entries: List[Dict[str, object]] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"revocation_log_integrity_error: malformed line {line_number}"
                ) from exc
            if not isinstance(entry, dict):
                raise ValueError(
                    f"revocation_log_integrity_error: malformed line {line_number}"
                )
            entries.append(entry)
        return entries

    def _write_revocation_log_atomic(self, pending: Dict[str, object]) -> None:
        """Add every journalled fact exactly once in one atomic log replace."""
        entries = self._read_revocation_log_strict()
        logged_ids = {
            str(entry["fact_id"])
            for entry in entries
            if isinstance(entry.get("fact_id"), str)
        }
        root_id = str(pending["root_fact_id"])
        for fact_id in pending["fact_ids"]:  # type: ignore[union-attr]
            if fact_id in logged_ids:
                continue
            entries.append(
                {
                    "timestamp_utc": pending["timestamp_utc"],
                    "fact_id": fact_id,
                    "reason": pending["reason"],
                    "revoked_as_dependent_of": (
                        root_id if fact_id != root_id else None
                    ),
                    "revocation_id": pending["revocation_id"],
                }
            )
            logged_ids.add(str(fact_id))
        rendered = "".join(
            json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries
        )
        self._atomic_write_text(self.revocation_log, rendered)

    def _resume_pending_revocation_unlocked(self) -> Optional[Dict[str, object]]:
        """Idempotently finish the durable pending cascade, if one exists."""
        pending = self._load_pending_revocation_unlocked()
        if pending is None:
            return None
        fact_ids = [str(item) for item in pending["fact_ids"]]  # type: ignore[union-attr]

        # The derived glossary becomes conservative before the first move. While
        # the journal exists every public reader fails closed, so no mixed graph
        # can be consumed as a normal truth snapshot.
        rebuilt_glossary = self._active_glossary_excluding(set(fact_ids))
        self._write_project_glossary_atomic(rebuilt_glossary)
        self._mkdir_durable(self.revoked_dir)
        for fact_id in fact_ids:
            source = self._path(fact_id)
            destination = self._revoked_path(fact_id)
            source_exists = source.exists()
            destination_exists = destination.exists()
            if source_exists and destination_exists:
                raise ValueError(
                    f"fact_integrity_error: {fact_id} exists in active and revoked stores"
                )
            if source_exists:
                shutil.move(str(source), str(destination))
                self._fsync_directory(self.facts_dir)
                self._fsync_directory(self.revoked_dir)
            elif not destination_exists:
                raise ValueError(
                    f"fact_graph_recovery_error: pending fact disappeared: {fact_id}"
                )

        self._write_revocation_log_atomic(pending)
        self._unlink_durable(self.pending_revocation_path)
        return pending

    def _completed_revocation_ids(self, fact_id: str) -> List[str]:
        entries = self._read_revocation_log_strict()
        root_entries = [
            entry
            for entry in entries
            if entry.get("fact_id") == fact_id
            and entry.get("revoked_as_dependent_of") is None
        ]
        if not root_entries:
            return []
        revocation_id = root_entries[-1].get("revocation_id")
        if isinstance(revocation_id, str):
            return [
                str(entry["fact_id"])
                for entry in entries
                if entry.get("revocation_id") == revocation_id
                and isinstance(entry.get("fact_id"), str)
            ]
        return [fact_id] + [
            str(entry["fact_id"])
            for entry in entries
            if entry.get("revoked_as_dependent_of") == fact_id
            and isinstance(entry.get("fact_id"), str)
        ]

    def revoke(self, fact_id: str, reason: str) -> List[str]:
        """Cascade-revoke ``fact_id`` and everything depending on it. Moves the
        files into ``_revoked/`` and logs each. Returns the revoked ids."""
        with self._mutation_lock():
            recovered = self._recover_pending_transactions_unlocked()
            if recovered is not None and recovered["root_fact_id"] == fact_id:
                return [str(item) for item in recovered["fact_ids"]]  # type: ignore[union-attr]
            return self._revoke_unlocked(fact_id, reason)

    def _revoke_unlocked(self, fact_id: str, reason: str) -> List[str]:
        if not isinstance(reason, str):
            raise ValueError("reason must be a string")
        if not self._exists_unchecked(fact_id):
            if self._revoked_path(fact_id).exists():
                completed = self._completed_revocation_ids(fact_id)
                if completed:
                    return completed
            raise ValueError(f"unknown fact_id: {fact_id}")
        to_revoke = [fact_id] + self._descendants_unchecked(fact_id)
        pending: Dict[str, object] = {
            "schema_version": 1,
            "revocation_id": uuid.uuid4().hex,
            "timestamp_utc": utc_now(),
            "root_fact_id": fact_id,
            "reason": reason,
            "fact_ids": to_revoke,
        }
        self._atomic_write_text(
            self.pending_revocation_path,
            json.dumps(pending, ensure_ascii=False, sort_keys=True) + "\n",
        )
        resumed = self._resume_pending_revocation_unlocked()
        assert resumed is not None
        return [str(item) for item in resumed["fact_ids"]]  # type: ignore[union-attr]
