"""
Append-only, hash-chained audit log.

Each scan appends one line to a JSON Lines file. A line carries a payload and
a digest computed over that payload together with the digest of the preceding
line. Altering or removing any earlier line therefore invalidates every digest
that follows it, and the alteration is detectable without a second copy of the
log.

The chain establishes internal consistency, not authenticity. It shows that a
log has not been edited since it was written; it cannot show who wrote it. An
adversary with write access to the file could rebuild the chain from any point.
This limitation is stated here because a mechanism whose guarantee is
overstated is worse than one that is absent: the reader stops checking.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterator

AUDIT_SCHEMA = "ecosentia-audit-1"

# The first entry has no predecessor. A fixed sentinel is used rather than an
# absent field so that the digest of entry one is computed by the same
# expression as every other entry, leaving no special case to get wrong.
GENESIS_DIGEST = "0" * 64

AUDIT_PATH = os.environ.get(
    "ECOSENTIA_AUDIT_PATH", os.path.join("audit", "ecosentia_audit.jsonl")
)

# Deployment identity, written into every entry. Without it, a citation to
# "the audit log" names a file rather than a body of evidence: a reader cannot
# tell which entries constitute the study record and which are incidental
# traffic against a publicly reachable instance.
INSTANCE_ROLE = os.environ.get("ECOSENTIA_INSTANCE_ROLE", "unspecified")

# Whether the audit path is a persistent volume. Declared rather than probed,
# because a writable directory on an ephemeral filesystem is indistinguishable
# from a durable one at write time and only differs on restart.
AUDIT_PERSISTENT = os.environ.get("ECOSENTIA_AUDIT_PERSISTENT", "0") == "1"

# Gunicorn runs one worker with several threads. The chain is appended by a
# single process, but concurrent threads would read the same tail digest and
# write two entries claiming the same predecessor. The resulting break is
# silent: both scans return normally and verification fails days later.
_WRITE_LOCK = threading.Lock()


class AuditError(RuntimeError):
    """Raised when the log cannot be written or its location is unusable."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(obj: Any) -> bytes:
    """
    Serialise deterministically for hashing.

    Key order is fixed and separators are minimal, so that two structurally
    equal payloads always produce identical bytes. Without this, a digest would
    depend on dictionary insertion order and verification would fail on data
    that had not changed.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _compute_digest(payload: dict, prev_digest: str) -> str:
    return hashlib.sha256(_canonical(payload) + prev_digest.encode("ascii")).hexdigest()


def ensure_writable(path: str = AUDIT_PATH) -> None:
    """
    Confirm at start-up that the log can be written.

    Called before the application accepts requests. Deferring the check to the
    first write would turn a configuration fault into a mid-scan failure, after
    an operator had already approved a query and external indexes had been
    queried on their behalf.
    """
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".write_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
        os.remove(probe)
    except OSError as exc:
        raise AuditError(f"audit path is not writable: {path} ({exc})") from exc


def iter_entries(path: str = AUDIT_PATH) -> Iterator[dict]:
    """
    Yield entries in write order, including malformed ones.

    A line that cannot be parsed is yielded as a placeholder rather than
    skipped. Silently discarding it would let a corrupted log verify as a
    shorter intact one, which is precisely the outcome the chain exists to
    prevent.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                yield {"_malformed": True, "_line": line_number}
                continue
            if not isinstance(entry, dict):
                yield {"_malformed": True, "_line": line_number}
                continue
            entry["_line"] = line_number
            yield entry


def _tail(path: str) -> tuple[int, str]:
    """Return the sequence number and digest of the last entry written."""
    seq, digest = 0, GENESIS_DIGEST
    for entry in iter_entries(path):
        if entry.get("_malformed"):
            # Chaining onto an unreadable tail would bury the corruption under
            # valid entries and make the log's history unrecoverable.
            raise AuditError(
                f"audit log is malformed at line {entry['_line']}; "
                "refusing to append until the file is inspected"
            )
        seq = int(entry.get("seq", seq))
        digest = str(entry.get("digest", digest))
    return seq, digest


def record(kind: str, body: dict, path: str = AUDIT_PATH) -> dict:
    """
    Append one entry and return it.

    `kind` distinguishes categories of event, most importantly machine scans
    from recorded expert judgements. The distinction is kept inside the hashed
    payload so that no one can reclassify a machine result as a human one
    without breaking the chain.
    """
    payload = {
        "schema": AUDIT_SCHEMA,
        "kind": kind,
        "timestamp": _utc_now_iso(),
        "instance": {
            "role": INSTANCE_ROLE,
            "audit_persistent": AUDIT_PERSISTENT,
        },
        "body": body,
    }

    with _WRITE_LOCK:
        prev_seq, prev_digest = _tail(path)
        entry = {
            "seq": prev_seq + 1,
            "prev": prev_digest,
            "digest": _compute_digest(payload, prev_digest),
            "payload": payload,
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            # The digest of the next entry depends on this one having survived.
            # Leaving it in a buffer would allow an abrupt shutdown to break
            # the chain rather than merely truncate it.
            os.fsync(handle.fileno())

    return entry


def verify_chain(path: str = AUDIT_PATH) -> dict:
    """
    Recompute every digest and report the first inconsistency.

    Verification stops describing entries after the first break, because once
    a predecessor is in doubt the digests that follow it carry no information.
    Reporting them as "also invalid" would exaggerate the extent of the fault.
    """
    entries: list[dict] = []
    expected_prev = GENESIS_DIGEST
    expected_seq = 1
    first_break: dict | None = None

    for entry in iter_entries(path):
        if entry.get("_malformed"):
            first_break = {"line": entry["_line"], "reason": "unparsable_line"}
            break

        line = entry.get("_line")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            first_break = {"line": line, "reason": "missing_payload"}
            break
        if entry.get("prev") != expected_prev:
            first_break = {
                "line": line,
                "seq": entry.get("seq"),
                "reason": "predecessor_mismatch",
            }
            break
        if entry.get("seq") != expected_seq:
            first_break = {
                "line": line,
                "seq": entry.get("seq"),
                "reason": "sequence_gap",
            }
            break
        recomputed = _compute_digest(payload, expected_prev)
        if recomputed != entry.get("digest"):
            first_break = {
                "line": line,
                "seq": entry.get("seq"),
                "reason": "digest_mismatch",
            }
            break

        entries.append(entry)
        expected_prev = str(entry["digest"])
        expected_seq += 1

    return {
        "ok": first_break is None,
        "verified_entries": len(entries),
        "head_digest": entries[-1]["digest"] if entries else None,
        "first_break": first_break,
        "audit_persistent": AUDIT_PERSISTENT,
        "instance_role": INSTANCE_ROLE,
        "checked_at": _utc_now_iso(),
    }


def read_entries(limit: int = 25, path: str = AUDIT_PATH) -> list[dict]:
    """
    Return the most recent entries, newest first, for display.

    The full payload is returned rather than a summary. A log presented through
    a filter the reader cannot inspect is not an auditable record.
    """
    collected = [e for e in iter_entries(path) if not e.get("_malformed")]
    return list(reversed(collected[-max(1, limit):]))


def export_bundle(path: str = AUDIT_PATH) -> dict:
    """
    Produce a citable manifest over the current chain.

    The manifest does not replace the log. It lets a reader confirm that a log
    they have received is the one the study describes: an archived file whose
    head digest does not match the published manifest is a different file,
    whatever its filename says.

    Counts are reported per entry kind and per instance role so that machine
    scans, expert reviews, and incidental public traffic remain
    distinguishable in the aggregate, as they are per entry.
    """
    verification = verify_chain(path)

    kinds: dict[str, int] = {}
    roles: dict[str, int] = {}
    timestamps: list[str] = []
    head_digest: str | None = None

    for entry in iter_entries(path):
        if entry.get("_malformed"):
            break
        payload = entry.get("payload") or {}
        kind = str(payload.get("kind", "unknown"))
        role = str((payload.get("instance") or {}).get("role", "unspecified"))
        kinds[kind] = kinds.get(kind, 0) + 1
        roles[role] = roles.get(role, 0) + 1
        if payload.get("timestamp"):
            timestamps.append(str(payload["timestamp"]))
        head_digest = str(entry.get("digest"))

    return {
        "schema": AUDIT_SCHEMA,
        "generated": _utc_now_iso(),
        "instance_role": INSTANCE_ROLE,
        "audit_persistent": AUDIT_PERSISTENT,
        "entry_count": sum(kinds.values()),
        "entries_by_kind": kinds,
        "entries_by_instance_role": roles,
        "first_entry": timestamps[0] if timestamps else None,
        "last_entry": timestamps[-1] if timestamps else None,
        "head_digest": head_digest,
        "verification": verification,
        "note": (
            "The chain demonstrates that this log has not been edited since it "
            "was written. It does not attest authorship. Archive this manifest "
            "with the log; a copy whose head digest differs is a different log."
        ),
    }