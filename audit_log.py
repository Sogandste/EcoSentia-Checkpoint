"""Append-only audit trail with chained hashes.

Each entry commits to its predecessor, so any retrospective edit invalidates
every subsequent digest. The trail records what was asked, what was executed and
what was returned — a support level that cannot be reproduced from its own log
is not evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

GENESIS_HASH = "0" * 64
LOG_PATH = os.getenv("ECOSENTIA_AUDIT_LOG", "audit_trail.jsonl")
MAX_EXCERPT_ENTRIES = 200

_lock = threading.Lock()


@dataclass
class AuditEntry:
    """One immutable record of a completed scan."""

    entry_id: str
    timestamp: str
    claim: str
    preset: str
    source: str
    query_executed: str
    relaxed: bool
    sources_used: List[str]
    source_errors: Dict[str, str]
    raw_counts: Dict[str, int]
    support_level: str
    aggregate_score: float
    downgraded: bool
    downgrade_reason: str
    limiting_factor: str
    lens_summary: Dict[str, float]
    bias_counts: Dict[str, int]
    expert_review: Optional[Dict[str, Any]] = None
    previous_hash: str = GENESIS_HASH
    entry_hash: str = ""
    schema_version: str = "2026.08"
    notes: List[str] = field(default_factory=list)


def _canonical(payload: Dict[str, Any]) -> str:
    """Deterministic serialisation for hashing.

    Sorted keys and fixed separators are required: dictionary iteration order
    must not change the digest, or verification fails on unmodified data.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(entry: AuditEntry) -> str:
    payload = {k: v for k, v in asdict(entry).items() if k != "entry_hash"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _read_all() -> List[Dict[str, Any]]:
    if not os.path.exists(LOG_PATH):
        return []
    entries: List[Dict[str, Any]] = []
    with open(LOG_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # A malformed line is preserved in place rather than skipped
                # silently: chain verification must report it as a break.
                entries.append({"entry_hash": "", "previous_hash": "", "_malformed": line[:200]})
    return entries


def last_hash() -> str:
    entries = _read_all()
    return entries[-1].get("entry_hash", GENESIS_HASH) if entries else GENESIS_HASH


def record_scan(
    *,
    claim: str,
    preset: str,
    source: str,
    query_executed: str,
    relaxed: bool,
    sources_used: List[str],
    source_errors: Dict[str, str],
    raw_counts: Dict[str, int],
    support_level: str,
    aggregate_score: float,
    downgraded: bool,
    downgrade_reason: str,
    limiting_factor: str,
    lens_summary: Dict[str, float],
    bias_counts: Dict[str, int],
    notes: Optional[List[str]] = None,
) -> AuditEntry:
    """Append one scan to the trail and return the committed entry.

    Writing is serialised and flushed to disk before returning, so the entry_id
    handed to the operator always corresponds to a persisted record.
    """
    with _lock:
        entry = AuditEntry(
            entry_id=uuid.uuid4().hex[:16],
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            claim=claim.strip(),
            preset=preset,
            source=source,
            query_executed=query_executed,
            relaxed=relaxed,
            sources_used=sorted(sources_used),
            source_errors=dict(source_errors),
            raw_counts=dict(raw_counts),
            support_level=support_level,
            aggregate_score=aggregate_score,
            downgraded=downgraded,
            downgrade_reason=downgrade_reason,
            limiting_factor=limiting_factor,
            lens_summary=dict(lens_summary),
            bias_counts=dict(bias_counts),
            previous_hash=last_hash(),
            notes=list(notes or []),
        )
        entry.entry_hash = compute_hash(entry)

        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(_canonical(asdict(entry)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    return entry


def attach_expert_review(
    entry_id: str,
    *,
    reviewer: str,
    verdict: str,
    comment: str,
) -> Optional[AuditEntry]:
    """Append a review as a new entry referencing the original.

    The original is never modified. Amending it in place would break the chain
    and, more importantly, would erase the distinction between what the system
    reported and what a human subsequently concluded — the one distinction this
    trail exists to preserve.
    """
    originals = [item for item in _read_all() if item.get("entry_id") == entry_id]
    if not originals:
        return None
    original = originals[0]

    with _lock:
        entry = AuditEntry(
            entry_id=uuid.uuid4().hex[:16],
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            claim=original.get("claim", ""),
            preset=original.get("preset", ""),
            source=original.get("source", ""),
            query_executed=original.get("query_executed", ""),
            relaxed=bool(original.get("relaxed", False)),
            sources_used=list(original.get("sources_used", [])),
            source_errors={},
            raw_counts={},
            support_level=original.get("support_level", ""),
            aggregate_score=float(original.get("aggregate_score", 0.0)),
            downgraded=bool(original.get("downgraded", False)),
            downgrade_reason=original.get("downgrade_reason", ""),
            limiting_factor=original.get("limiting_factor", ""),
            lens_summary={},
            bias_counts={},
            expert_review={
                "reviews_entry": entry_id,
                "reviewer": reviewer.strip(),
                "verdict": verdict.strip(),
                "comment": comment.strip(),
            },
            previous_hash=last_hash(),
            notes=[f"Expert review of entry {entry_id}."],
        )
        entry.entry_hash = compute_hash(entry)

        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(_canonical(asdict(entry)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    return entry


def verify_chain() -> Dict[str, Any]:
    """Recompute every digest and locate the first break.

    Reports the position of a break rather than a boolean, because a trail that
    is intact up to a known point retains evidential value beyond it being
    merely 'invalid'.
    """
    entries = _read_all()
    if not entries:
        return {"valid": True, "entries": 0, "first_break": None, "detail": "Trail is empty."}

    expected_previous = GENESIS_HASH
    for index, raw in enumerate(entries):
        if "_malformed" in raw:
            return {
                "valid": False, "entries": len(entries), "first_break": index,
                "detail": f"Entry {index} is not valid JSON.",
            }
        if raw.get("previous_hash") != expected_previous:
            return {
                "valid": False, "entries": len(entries), "first_break": index,
                "detail": f"Entry {index} does not reference its predecessor's digest.",
            }

        stored = raw.get("entry_hash", "")
        payload = {k: v for k, v in raw.items() if k != "entry_hash"}
        if hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest() != stored:
            return {
                "valid": False, "entries": len(entries), "first_break": index,
                "detail": f"Entry {index} has been modified since it was written.",
            }
        expected_previous = stored

    return {
        "valid": True, "entries": len(entries), "first_break": None,
        "detail": f"All {len(entries)} entries verified.",
    }


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    """Most recent entries, newest first, for the audit view."""
    return list(reversed(_read_all()[-max(1, min(limit, MAX_EXCERPT_ENTRIES)):]))