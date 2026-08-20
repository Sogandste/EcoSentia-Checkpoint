"""EcoSentia HTTP service.

Wires the scan chain: plan -> retrieve -> score -> bias-check -> audit. The
service adds no inference of its own. Its responsibilities are to keep the
stages in order, to distinguish retrieval failure from absent evidence in the
status code as well as the payload, and to persist every completed scan before
returning a result the operator might act on.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import audit_log
from bias_checker import BiasReport, check_records
from domain_config import LENSES, PRESETS, get_lens, requires_expert_verification
from query_builder import QueryPlan, build_plan
from schemas import MAX_CLAIM_LENGTH, MIN_CLAIM_LENGTH, normalise_source
from scoring import ScanScore, score_scan
from source_clients import SourceResult, retrieve

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_LIMIT = 40
MAX_LIMIT = 100

logging.basicConfig(
    level=os.getenv("ECOSENTIA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ecosentia")

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------

class RequestError(Exception):
    """A malformed request. Raised before any network call is made."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def _payload() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise RequestError("Request body must be a JSON object.")
    return data


def _claim(data: Dict[str, Any]) -> str:
    """Validate the claim before spending a request on it.

    Length bounds are enforced here rather than downstream because a claim that
    is too short cannot produce a specific query, and one that is too long
    produces a conjunction that no abstract satisfies. Both fail in ways that
    read as absent evidence.
    """
    claim = str(data.get("claim", "")).strip()
    if not claim:
        raise RequestError("A claim is required.", "claim")
    if len(claim) < MIN_CLAIM_LENGTH:
        raise RequestError(
            f"The claim is too short to specify a query. Minimum {MIN_CLAIM_LENGTH} "
            "characters; state the mechanism and the asserted outcome.",
            "claim",
        )
    if len(claim) > MAX_CLAIM_LENGTH:
        raise RequestError(
            f"The claim exceeds {MAX_CLAIM_LENGTH} characters. Split it into "
            "separately testable assertions and scan each one.",
            "claim",
        )
    return claim


def _preset(data: Dict[str, Any]) -> str:
    preset = str(data.get("preset", "")).strip().lower()
    if not preset:
        raise RequestError("A domain preset is required.", "preset")
    if preset not in PRESETS:
        raise RequestError(
            f"Unknown preset '{preset}'. Available: {', '.join(sorted(PRESETS))}.",
            "preset",
        )
    return preset


def _lens_keys(data: Dict[str, Any]) -> List[str]:
    """Resolve the requested lenses, rejecting unknown keys rather than ignoring them.

    Silently dropping an unrecognised key would return a result that appears to
    cover a lens the operator asked for and does not.
    """
    raw = data.get("lenses")
    if raw is None:
        return [lens.key for lens in LENSES]
    if not isinstance(raw, list) or not raw:
        raise RequestError("'lenses' must be a non-empty list of lens keys.", "lenses")

    known = {lens.key for lens in LENSES}
    unknown = [str(key) for key in raw if str(key) not in known]
    if unknown:
        raise RequestError(
            f"Unknown lens key(s): {', '.join(unknown)}. Available: "
            f"{', '.join(sorted(known))}.",
            "lenses",
        )
    return [str(key) for key in raw]


def _limit(data: Dict[str, Any]) -> int:
    try:
        limit = int(data.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise RequestError("'limit' must be an integer.", "limit")
    return max(5, min(limit, MAX_LIMIT))


# --------------------------------------------------------------------------
# Response assembly
# --------------------------------------------------------------------------

def _hard_errors(result: SourceResult) -> Dict[str, str]:
    """Errors that removed a source from the result, excluding advisory notes.

    Notes recording a downgraded but successful query are not failures and must
    not be counted as such, or a working scan would report itself as degraded.
    """
    return {
        name: message
        for name, message in result.errors.items()
        if not name.endswith("_note")
    }


def _interpretation(score: ScanScore, result: SourceResult, bias: BiasReport) -> Dict[str, Any]:
    """The boundary between what was computed and what it licenses.

    Kept as a separate object from the numeric output so that no client can
    render a support level without the statement of what it does and does not
    mean. Reviewers of an earlier build read a high level as a verification;
    this field exists because that reading was available.
    """
    verification_lenses = [
        item.label for item in score.lens_scores
        if item.expert_verification_required and item.score > 0
    ]
    contested = [
        item.label for item in score.lens_scores
        if item.positive_matches and item.negative_matches
    ]

    required: List[str] = []
    if verification_lenses:
        required.append(
            "Domain-expert verification of the primary studies for: "
            + ", ".join(verification_lenses)
            + ". Lexical signal cannot establish these lenses."
        )
    if contested:
        required.append(
            "Reading, not counting, for contested lenses: " + ", ".join(contested)
            + ". Supporting and contradicting vocabulary both occur."
        )
    if result.relaxed:
        required.append(
            "Re-specification of the claim. The strict query returned too few "
            "records and a broader query was executed in its place."
        )
    if bias.degraded_records:
        required.append(
            f"Manual reading of {bias.degraded_records} record(s) whose abstracts "
            "lacked punctuation; risk-pattern detection ran at reduced sensitivity."
        )
    if score.records_scored and not score.direct_hits:
        required.append(
            "Assessment of whether partial matches address the claim at all. No "
            "record matched a majority of its terms."
        )

    return {
        "what_this_is": (
            "A measurement of lexical overlap between the claim and the abstracts "
            "of retrieved records."
        ),
        "what_this_is_not": (
            "An assessment of whether the claim is true, whether the studies are "
            "sound, or whether the mechanism transfers to application."
        ),
        "human_judgement_required": required,
        "limiting_factor": score.limiting_factor,
    }


def _build_response(
    *,
    claim: str,
    preset: str,
    source: str,
    plan: QueryPlan,
    result: SourceResult,
    score: ScanScore,
    bias: BiasReport,
    entry_id: str,
    elapsed_ms: int,
) -> Dict[str, Any]:
    hard = _hard_errors(result)
    return {
        "request": {
            "claim": claim,
            "preset": preset,
            "preset_label": PRESETS[preset].label,
            "source": source,
            "lenses": [item.key for item in score.lens_scores],
        },
        "query": {
            "canonical": plan.canonical,
            "executed": result.query_executed,
            "relaxed": result.relaxed,
            "anchors": plan.anchors,
            "terms": plan.all_terms,
            "excluded": plan.negatives,
        },
        "retrieval": {
            "sources_used": result.sources_used,
            "sources_failed": sorted(hard),
            "partial_failure": bool(hard),
            "errors": result.errors,
            "counts": result.raw_counts,
            "records_scored": score.records_scored,
        },
        "machine_output": {
            "support_level": score.support_level,
            "aggregate_score": score.aggregate_score,
            "downgraded": score.downgraded,
            "downgrade_reason": score.downgrade_reason,
            "direct_hits": score.direct_hits,
            "partial_hits": score.partial_hits,
            "term_coverage": score.term_coverage,
            "unmatched_terms": score.unmatched_terms,
            "lenses": [asdict(item) for item in score.lens_scores],
        },
        "translation_risk": {
            "summary": bias.summary,
            "records_examined": bias.records_examined,
            "degraded_records": bias.degraded_records,
            "counts_by_severity": bias.counts_by_severity,
            "findings": [asdict(item) for item in bias.findings],
        },
        "interpretation": _interpretation(score, result, bias),
        "records": [
            {
                "title": record.title,
                "year": record.year,
                "source": record.source,
                "identifier": record.identifier,
                "url": record.url,
                "abstract": record.abstract[:600],
                "abstract_truncated": len(record.abstract) > 600,
            }
            for record in result.records[:25]
        ],
        "audit": {"entry_id": entry_id},
        "elapsed_ms": elapsed_ms,
    }


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------

@app.get("/")
def index() -> Any:
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename: str) -> Any:
    return send_from_directory(STATIC_DIR, filename)


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------

@app.get("/api/config")
def config() -> Any:
    """Presets and lenses, so the client never hardcodes domain vocabulary.

    A client copy of these definitions would drift from the server's, and the
    interface would then describe a scan different from the one executed.
    """
    return jsonify({
        "presets": [
            {"key": key, "label": preset.label, "description": preset.description}
            for key, preset in sorted(PRESETS.items())
        ],
        "lenses": [
            {
                "key": lens.key,
                "label": lens.label,
                "question": lens.question,
                "expert_verification_required": requires_expert_verification(lens.key),
            }
            for lens in LENSES
        ],
        "sources": ["pubmed", "openalex", "both"],
        "claim_length": {"min": MIN_CLAIM_LENGTH, "max": MAX_CLAIM_LENGTH},
    })


@app.get("/api/health")
def health() -> Any:
    chain = audit_log.verify_chain()
    return jsonify({
        "status": "ok" if chain["valid"] else "degraded",
        "audit_chain": chain,
        "schema_version": audit_log.AuditEntry.__dataclass_fields__["schema_version"].default,
    })


# --------------------------------------------------------------------------
# Query preview
# --------------------------------------------------------------------------

@app.post("/api/plan")
def plan_only() -> Any:
    """Return the query without executing it.

    Exposed separately so that an operator can inspect and reject a
    mis-specified query before it consumes a request and before its empty result
    is available to be misread as absent evidence.
    """
    data = _payload()
    plan = build_plan(_claim(data), _preset(data))
    return jsonify({
        "canonical": plan.canonical,
        "anchors": plan.anchors,
        "terms": plan.all_terms,
        "excluded": plan.negatives,
        "facets": plan.facets,
        "warnings": plan.warnings,
    })


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

@app.post("/api/scan")
def scan() -> Any:
    """Execute the full chain and persist the result.

    Stage order is fixed: the plan determines the query, the query determines
    the records, the records determine the score. Scoring against anything other
    than the records the logged query returned would make the audit entry a
    record of a scan that did not happen.
    """
    started = time.monotonic()
    data = _payload()

    claim = _claim(data)
    preset = _preset(data)
    source = normalise_source(str(data.get("source", "both")))
    lens_keys = _lens_keys(data)
    limit = _limit(data)

    plan = build_plan(claim, preset)
    result = retrieve(plan, source=source, limit=limit)

    hard = _hard_errors(result)
    requested = {"pubmed", "openalex"} if source == "both" else {source}
    if not result.sources_used and hard:
        # Every requested index failed. Returning 200 with a support level of
        # 'none' would present an infrastructure fault as a scientific finding,
        # which is the one outcome this service must never produce.
        return jsonify({
            "error": "retrieval_failed",
            "message": (
                "No source could be reached, so no statement about the literature "
                "can be made. This is not a finding of absent evidence."
            ),
            "sources_attempted": sorted(requested),
            "errors": result.errors,
            "query": {"canonical": plan.canonical},
        }), 502

    score = score_scan(plan, result, lens_keys)
    bias = check_records(result.records)

    notes: List[str] = list(plan.warnings)
    if hard:
        notes.append(
            "Partial retrieval: " + ", ".join(sorted(hard))
            + " unavailable. The support level rests on the remaining source(s)."
        )
    if result.relaxed:
        notes.append("Relaxed query executed after the strict query returned too few records.")

    entry = audit_log.record_scan(
        claim=claim,
        preset=preset,
        source=source,
        query_executed=result.query_executed,
        relaxed=result.relaxed,
        sources_used=result.sources_used,
        source_errors=result.errors,
        raw_counts=result.raw_counts,
        support_level=score.support_level,
        aggregate_score=score.aggregate_score,
        downgraded=score.downgraded,
        downgrade_reason=score.downgrade_reason,
        limiting_factor=score.limiting_factor,
        lens_summary={item.key: item.score for item in score.lens_scores},
        bias_counts=bias.counts_by_severity,
        notes=notes,
    )

    response = _build_response(
        claim=claim,
        preset=preset,
        source=source,
        plan=plan,
        result=result,
        score=score,
        bias=bias,
        entry_id=entry.entry_id,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    response["notes"] = notes

    logger.info(
        "scan entry=%s preset=%s sources=%s relaxed=%s scored=%d level=%s",
        entry.entry_id, preset, ",".join(result.sources_used) or "-",
        result.relaxed, score.records_scored, score.support_level,
    )
    return jsonify(response)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

@app.get("/api/audit/recent")
def audit_recent() -> Any:
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    return jsonify({"entries": audit_log.recent(limit)})


@app.get("/api/audit/verify")
def audit_verify() -> Any:
    """Independent chain verification, exposed rather than internal.

    A trail whose integrity can only be asserted by the system that wrote it
    provides no assurance. This endpoint recomputes every digest on request.
    """
    return jsonify(audit_log.verify_chain())


@app.post("/api/audit/review")
def audit_review() -> Any:
    """Attach an expert verdict as a new chained entry.

    The reviewed entry is not modified. The verdict is a human conclusion and is
    stored as such, adjacent to the machine output rather than replacing it.
    """
    data = _payload()

    entry_id = str(data.get("entry_id", "")).strip()
    reviewer = str(data.get("reviewer", "")).strip()
    verdict = str(data.get("verdict", "")).strip().lower()
    comment = str(data.get("comment", "")).strip()

    if not entry_id:
        raise RequestError("'entry_id' is required.", "entry_id")
    if not reviewer:
        raise RequestError("A reviewer identifier is required for attribution.", "reviewer")
    if verdict not in {"confirmed", "rejected", "inconclusive"}:
        raise RequestError(
            "'verdict' must be one of: confirmed, rejected, inconclusive.", "verdict"
        )
    if verdict != "confirmed" and len(comment) < 20:
        raise RequestError(
            "A rejection or inconclusive verdict requires a stated reason of at "
            "least 20 characters.",
            "comment",
        )

    entry = audit_log.attach_expert_review(
        entry_id, reviewer=reviewer, verdict=verdict, comment=comment
    )
    if entry is None:
        return jsonify({
            "error": "entry_not_found",
            "message": f"No audit entry with id '{entry_id}'.",
        }), 404

    return jsonify({
        "entry_id": entry.entry_id,
        "reviews_entry": entry_id,
        "verdict": verdict,
        "timestamp": entry.timestamp,
    }), 201


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------

@app.errorhandler(RequestError)
def handle_request_error(error: RequestError) -> Tuple[Any, int]:
    return jsonify({"error": "invalid_request", "message": error.message,
                    "field": error.field}), 400


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException) -> Tuple[Any, int]:
    return jsonify({"error": "http_error", "message": error.description}), error.code or 500


@app.errorhandler(Exception)
def handle_unexpected(error: Exception) -> Tuple[Any, int]:
    """Report an internal fault as a fault.

    The traceback is logged and withheld from the client, but the response never
    substitutes an empty result for it. An empty result is a claim about the
    literature; a 500 is a claim about the service.
    """
    logger.exception("unhandled error: %s", error)
    return jsonify({
        "error": "internal_error",
        "message": (
            "The scan did not complete. No conclusion about the literature "
            "should be drawn from this response."
        ),
    }), 500


if __name__ == "__main__":
    # PORT is injected by the platform. Binding to 127.0.0.1 or to a fixed
    # port would make the container unreachable behind the platform proxy.
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)