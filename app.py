"""
HTTP interface for EcoSentia.

Two endpoints carry the method. /api/plan builds a query plan and returns it
without contacting any source. /api/scan executes a plan the operator has
already seen. Splitting them is not a convenience: it makes approval a
precondition of retrieval rather than a formality after it.

Every completed scan produces an audit record containing the plan hash, the
queries actually sent, per-source outcomes, and the configuration version.
A result that cannot be traced to the query that produced it is not a finding.
"""

from __future__ import annotations

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify, send_from_directory

import domain_config as dc
from query_builder import build_plan, plan_from_dict
from retrieval import retrieve
from scoring import score, ScoringError
import bias_checker

APP_VERSION = "1.0.0"
AUDIT_SCHEMA = "ecosentia-audit-1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ecosentia")

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["JSON_SORT_KEYS"] = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plan_hash(plan_dict: dict) -> str:
    """
    Stable hash over the plan as approved.

    Computed from the canonical JSON form so that the same plan hashes
    identically across processes. This is what ties an exported result to the
    exact query the operator saw, including its warnings.
    """
    payload = json.dumps(plan_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _error(message: str, status: int, **extra):
    body = {"ok": False, "error": message, "timestamp": _now()}
    body.update(extra)
    return jsonify(body), status


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "config_version": dc.CONFIG_VERSION,
        "contact_configured": bool(os.environ.get("ECOSENTIA_CONTACT", "").strip()),
        "ncbi_key_configured": bool(os.environ.get("NCBI_API_KEY", "").strip()),
        "timestamp": _now(),
    })


@app.route("/api/config")
def config():
    """Vocabulary and lens definitions, so the interface never hardcodes them."""
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "config_version": dc.CONFIG_VERSION,
        "presets": [
            {"key": p.key, "label": p.label, "description": p.description}
            for p in dc.list_presets()
        ],
        "lenses": [
            {
                "key": l.key,
                "label": l.label,
                "question": l.question,
                "detects": l.detects,
                "requires_expert": l.requires_expert,
                "epistemic": l.epistemic,
                "default": l.key in dc.DEFAULT_LENSES,
            }
            for l in dc.list_lenses()
        ],
        "support_levels": [
            {"key": s.key, "label": s.label, "meaning": s.meaning}
            for s in dc.list_support_levels()
        ],
        "scope_statement": dc.SCOPE_STATEMENT,
    })


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@app.route("/api/plan", methods=["POST"])
def plan_endpoint():
    """
    Build a query plan. No external request is made here.

    The plan is returned complete, including the warnings and the dialect
    notes, so that what the operator approves is the same object that /api/scan
    will execute.
    """
    data = request.get_json(silent=True) or {}
    claim = (data.get("claim") or "").strip()
    if not claim:
        return _error("a claim is required", 400)
    if len(claim) > 1000:
        return _error("claim exceeds 1000 characters", 400)

    try:
        plan = build_plan(
            claim=claim,
            preset=data.get("preset") or dc.DEFAULT_PRESET,
            lenses=data.get("lenses"),
            max_records=int(data.get("max_records") or 50),
        )
    except (ValueError, KeyError) as exc:
        return _error(str(exc), 400)

    plan_dict = plan.to_dict()
    return jsonify({
        "ok": True,
        "plan": plan_dict,
        "plan_hash": _plan_hash(plan_dict),
        "timestamp": _now(),
    })


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@app.route("/api/scan", methods=["POST"])
def scan_endpoint():
    """
    Execute an approved plan.

    The plan is reconstructed from the client's payload rather than rebuilt
    from the claim, so the executed query is provably the approved one. When no
    source answers, the request fails with 503 and scoring is never reached:
    an infrastructure failure must not be returned as a weak result.
    """
    data = request.get_json(silent=True) or {}
    raw_plan = data.get("plan")
    if not isinstance(raw_plan, dict):
        return _error("an approved plan is required", 400)
    if not data.get("approved"):
        return _error("the plan must be approved before it is executed", 400)

    try:
        plan = plan_from_dict(raw_plan)
    except (ValueError, KeyError, TypeError) as exc:
        return _error(f"plan could not be read: {exc}", 400)

    plan_dict = plan.to_dict()
    plan_hash = _plan_hash(plan_dict)
    started = time.monotonic()

    try:
        retrieval = retrieve(plan)
    except Exception as exc:  # noqa: BLE001 - surfaced, never silently absorbed
        log.exception("retrieval failed")
        return _error(
            f"retrieval failed before any source was reached: "
            f"{exc.__class__.__name__}",
            503,
            plan_hash=plan_hash,
        )

    if not retrieval.usable:
        # Both sources failed. There is nothing to score, and returning zero
        # counts here would convert a network outage into a finding about the
        # claim. The per-source diagnostics go back so the operator can see why.
        return _error(
            "no source answered; this scan produced no evidence about the "
            "claim, in either direction",
            503,
            plan_hash=plan_hash,
            sources=[s.to_dict() for s in retrieval.sources],
            retrieval=retrieval.to_dict(),
        )

    try:
        scoring = score(plan, retrieval)
    except ScoringError as exc:
        return _error(str(exc), 503, plan_hash=plan_hash)

    bias = bias_checker.check(plan, retrieval, scoring)
    elapsed = round(time.monotonic() - started, 2)

    audit = {
        "schema": AUDIT_SCHEMA,
        "app_version": APP_VERSION,
        "config_version": plan.config_version,
        "plan_hash": plan_hash,
        "executed_at": _now(),
        "elapsed_s": elapsed,
        "queries_sent": {s.source: s.query_sent for s in retrieval.sources},
        "source_outcomes": [s.to_dict() for s in retrieval.sources],
        "lenses_applied": list(plan.lenses),
        "records_analysed": scoring.records_analysed,
        "composite_score": None,
        "statement": (
            "This record describes a lexical scan of indexed abstracts. It "
            "reports where a claim's vocabulary appears in published titles "
            "and abstracts. It does not evaluate whether the claim is correct."
        ),
    }

    if plan.config_version != dc.CONFIG_VERSION:
        # The approved plan predates the running vocabulary. The scan is still
        # valid because the approved queries were executed verbatim, but the
        # divergence belongs in the record.
        audit["config_drift"] = (
            f"plan built under {plan.config_version}, "
            f"executed under {dc.CONFIG_VERSION}"
        )

    log.info(
        "scan %s records=%d sources_ok=%d elapsed=%.2fs",
        plan_hash, scoring.records_analysed, len(retrieval.succeeded), elapsed,
    )

    return jsonify({
        "ok": True,
        "plan": plan_dict,
        "plan_hash": plan_hash,
        "retrieval": retrieval.to_dict(),
        "records": [r.to_dict() for r in retrieval.records],
        "scoring": scoring.to_dict(),
        "bias": bias,
        "audit": audit,
        "timestamp": _now(),
    })


@app.errorhandler(404)
def not_found(_):
    return _error("endpoint not found", 404)


@app.errorhandler(500)
def server_error(_):
    return _error("internal error", 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=False)