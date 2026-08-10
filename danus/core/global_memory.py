"""global memory — project-shared, strongly typed findings.

One append-only JSONL file per kind (one file per channel, shared) + BM25.
Each entry is a *claim plus its evidence* with a ``verifiable`` tag and a
``status``. See DATA_MODEL.md §2.

Deliberately thin: append / read / search the JSONL, plus a small append-only
``status`` note. *When* to publish, verify, or promote is prose (prompts), not
code.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import bm25
from ._util import append_jsonl, read_jsonl, utc_now
from .schema import GLOBAL_KINDS, STATUSES, validate_advisor_checkpoint

_STATUS_LOG = "_status.jsonl"  # append-only status transitions
_ENTRY_ID_RE = re.compile(r"^[0-9a-f]{16}$")
GM_GET_MAX_SERIALIZED_BYTES = 16 * 1024
GM_IMMUTABLE_MAX_SERIALIZED_BYTES = 32 * 1024
GM_IMMUTABLE_MAX_PHYSICAL_LINE_BYTES = (
    2 * GM_IMMUTABLE_MAX_SERIALIZED_BYTES + 1
)


def canonical_global_memory_record(
    record: Dict[str, Any],
    *,
    max_bytes: int = GM_IMMUTABLE_MAX_SERIALIZED_BYTES,
) -> bytes:
    """Return the strict canonical bytes for one immutable JSONL record."""

    try:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("global-memory entry is not strict canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise ValueError(
            "global-memory entry exceeds exact-lookup serialized byte limit"
        )
    return encoded


def _strict_json_object(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate global-memory JSON key: {key}")
        output[key] = value
    return output


def _strict_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("global-memory JSON number must be finite")
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"global-memory JSON constant is forbidden: {value}")


def _iter_jsonl_strict(path: Path):
    """Yield JSON objects while treating every malformed durable line as fatal."""

    if not path.exists():
        return
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            line_number = 0
            while True:
                raw_line = handle.readline(GM_IMMUTABLE_MAX_PHYSICAL_LINE_BYTES + 1)
                if not raw_line:
                    break
                line_number += 1
                if len(raw_line) > GM_IMMUTABLE_MAX_PHYSICAL_LINE_BYTES:
                    raise ValueError(
                        f"global-memory JSONL line {line_number} exceeds its "
                        "physical byte limit"
                    )
                if not raw_line.endswith(b"\n"):
                    raise ValueError(
                        f"torn global-memory JSONL line {line_number}"
                    )
                if not raw_line.strip():
                    continue
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"malformed global-memory UTF-8 line {line_number}"
                    ) from exc
                try:
                    payload = json.loads(
                        line,
                        object_pairs_hook=_strict_json_object,
                        parse_float=_strict_json_float,
                        parse_constant=_reject_json_constant,
                    )
                except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                    raise ValueError(
                        f"malformed global-memory JSONL line {line_number}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"global-memory JSONL line {line_number} is not an object"
                    )
                canonical_global_memory_record(payload)
                yield payload
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GlobalMemory:
    """Rooted at the project directory; shared by all workers + the main agent."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "global_memory"

    def _path(self, kind: str) -> Path:
        return self.dir / f"{kind}.jsonl"

    # ------------------------------------------------------------------ write
    def append(
        self,
        kind: str,
        claim: str,
        evidence: str,
        author: str,
        *,
        verifiable: Optional[bool] = None,
        links: Optional[Dict[str, Any]] = None,
        glossary: Optional[Dict[str, str]] = None,
        **extra: Any,
    ) -> str:
        """Publish a finding (claim + evidence). Returns its id.

        ``verifiable`` defaults to the kind's default; objectively-checkable
        kinds require non-empty ``evidence`` (a proof/construction). ``glossary``
        (symbol -> definition) is optional but encouraged: define your symbols
        and reuse the project's terminology, so the finding stays readable and
        carries cleanly into a fact (DATA_MODEL.md §2 writing guideline).
        """
        if kind not in GLOBAL_KINDS:
            raise ValueError(f"unknown kind '{kind}'. Known: {sorted(GLOBAL_KINDS)}")
        if kind == "advisor_checkpoint":
            validate_advisor_checkpoint(claim, evidence, links)
        if verifiable is None:
            verifiable = GLOBAL_KINDS[kind]
        if verifiable and not (evidence or "").strip():
            raise ValueError(f"kind '{kind}' is verifiable and requires explicit evidence")
        ts = utc_now()
        entry_id = hashlib.sha256(
            json.dumps([kind, claim, author, ts], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        append_jsonl(
            self._path(kind),
            {
                "id": entry_id,
                "timestamp_utc": ts,
                "author": author,
                "kind": kind,
                "claim": claim,
                "evidence": evidence,
                "verifiable": verifiable,
                "status": "unverified" if verifiable else "open",
                "fact_id": None,
                "links": links or {},
                "glossary": glossary or {},
                **extra,
            },
        )
        return entry_id

    def set_status(self, entry_id: str, status: str, fact_id: Optional[str] = None) -> None:
        """Record a status transition (append-only)."""
        if status not in STATUSES:
            raise ValueError(f"invalid status '{status}'. Valid: {STATUSES}")
        append_jsonl(
            self.dir / _STATUS_LOG,
            {"timestamp_utc": utc_now(), "id": entry_id, "status": status, "fact_id": fact_id},
        )

    # ------------------------------------------------------------------- read
    def _latest_status(self) -> Dict[str, Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
        for rec in read_jsonl(self.dir / _STATUS_LOG):
            if rec.get("id"):
                latest[rec["id"]] = rec  # file is chronological; last wins
        return latest

    def read(self, kind: str) -> List[Dict[str, Any]]:
        """All entries of a kind, with the latest status folded in."""
        latest = self._latest_status()
        out = []
        for e in read_jsonl(self._path(kind)):
            st = latest.get(e.get("id"))
            if st:
                e = {**e, "status": st["status"], "fact_id": st.get("fact_id") or e.get("fact_id")}
            out.append(e)
        return out

    def get(self, entry_id: str) -> Dict[str, Any]:
        """Return exactly one bounded entry by its canonical 16-hex id.

        Exact lookup scans every kind and refuses an absent or duplicated id.
        The serialized cap keeps lazy hydration bounded even if an older or
        manually-written record is unexpectedly large.
        """
        if not isinstance(entry_id, str) or _ENTRY_ID_RE.fullmatch(entry_id) is None:
            raise ValueError("global-memory entry_id must be 16 lowercase hex characters")

        match = self.get_immutable(entry_id)

        status = self._latest_status().get(entry_id)
        if status:
            match = {
                **match,
                "status": status["status"],
                "fact_id": status.get("fact_id") or match.get("fact_id"),
            }
        canonical_global_memory_record(
            match,
            max_bytes=GM_GET_MAX_SERIALIZED_BYTES,
        )
        return match

    def get_immutable(self, entry_id: str) -> Dict[str, Any]:
        """Return one exact raw JSONL record without folding status overlays.

        Advisor checkpoint identities use this immutable projection.  A later
        append to ``_status.jsonl`` therefore cannot invalidate an already
        prepared owner receipt, while any edit to the original JSONL record is
        detected by its canonical digest.
        """

        if not isinstance(entry_id, str) or _ENTRY_ID_RE.fullmatch(entry_id) is None:
            raise ValueError("global-memory entry_id must be 16 lowercase hex characters")

        match: Optional[Dict[str, Any]] = None
        for kind in GLOBAL_KINDS:
            for entry in _iter_jsonl_strict(self._path(kind)):
                if entry.get("id") != entry_id:
                    continue
                if match is not None:
                    raise ValueError(f"duplicate global-memory entry_id: {entry_id}")
                match = entry

        if match is None:
            raise ValueError(f"unknown global-memory entry_id: {entry_id}")
        canonical_global_memory_record(match)
        return match

    def get_immutable_in_kind(self, kind: str, entry_id: str) -> Dict[str, Any]:
        """Return one exact immutable entry while scanning only one channel.

        Checkpoint identity attestation uses this scoped lookup so corruption in
        an unrelated memory kind cannot make an otherwise exact checkpoint
        unavailable.  The selected channel remains strict and duplicate ids in
        that channel fail closed.
        """

        if kind not in GLOBAL_KINDS:
            raise ValueError(f"unknown kind '{kind}'. Known: {sorted(GLOBAL_KINDS)}")
        if not isinstance(entry_id, str) or _ENTRY_ID_RE.fullmatch(entry_id) is None:
            raise ValueError("global-memory entry_id must be 16 lowercase hex characters")

        match: Optional[Dict[str, Any]] = None
        for entry in _iter_jsonl_strict(self._path(kind)):
            if entry.get("id") != entry_id:
                continue
            if match is not None:
                raise ValueError(f"duplicate global-memory entry_id: {entry_id}")
            match = entry
        if match is None:
            raise ValueError(f"unknown global-memory entry_id: {entry_id}")
        canonical_global_memory_record(match)
        return match

    def iter_immutable(self, kind: str) -> Iterator[Dict[str, Any]]:
        """Iterate one raw channel strictly without materializing it in memory."""

        if kind not in GLOBAL_KINDS:
            raise ValueError(f"unknown kind '{kind}'. Known: {sorted(GLOBAL_KINDS)}")
        for record in _iter_jsonl_strict(self._path(kind)):
            canonical_global_memory_record(record)
            yield record

    def read_immutable(self, kind: str) -> List[Dict[str, Any]]:
        """Read one raw channel strictly, without folding mutable status notes."""

        return list(self.iter_immutable(kind))

    def search(
        self, query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10
    ) -> Dict[str, Any]:
        """BM25 over the chosen kinds (default: all)."""
        latest = self._latest_status()
        out: Dict[str, Any] = {}
        for kind in (kinds or list(GLOBAL_KINDS)):
            entries = read_jsonl(self._path(kind))
            docs = [bm25.tokenize(json.dumps(e, ensure_ascii=False)) for e in entries]
            scores = bm25.bm25_scores(query, docs)
            ranked = []
            for e, s in sorted(zip(entries, scores), key=lambda p: -p[1]):
                if s <= 0:
                    break
                st = latest.get(e.get("id"))
                if st:
                    e = {**e, "status": st["status"], "fact_id": st.get("fact_id") or e.get("fact_id")}
                ranked.append({"score": s, "entry": e})
                if len(ranked) >= limit_per_kind:
                    break
            out[kind] = {"count": len(ranked), "results": ranked}
        return {"query": query, "results_by_kind": out}
