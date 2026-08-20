"""Lexical support scoring and aggregation to support levels.

The scorer measures lexical co-occurrence between a claim and retrieved
abstracts. That is all it measures. It does not read, infer or verify, and the
support level it produces is a statement about the literature's vocabulary, not
about the truth of the claim.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from domain_config import (
    LENSES,
    get_lens,
    get_negative_terms,
    get_positive_terms,
    requires_expert_verification,
)
from query_builder import QueryPlan
from schemas import Record
from source_clients import SourceResult

# Direct hit: a record matching the anchors and a majority of claim terms.
DIRECT_HIT_COVERAGE = 0.60
PARTIAL_HIT_COVERAGE = 0.30

# Saturation constant for the logarithmic transform. Chosen so that the fifth
# matching record adds roughly a fifth of the first record's contribution.
SATURATION_K = 4.0

# Support level thresholds on the aggregate score, in [0, 1].
SUPPORT_THRESHOLDS: Tuple[Tuple[float, str], ...] = (
    (0.70, "strong"),
    (0.45, "moderate"),
    (0.20, "weak"),
    (0.00, "none"),
)

MIN_RECORDS_FOR_STRONG = 5
MIN_RECORDS_FOR_MODERATE = 3

_WORD = re.compile(r"[\w\-]+", re.UNICODE)


@dataclass
class LensScore:
    """Per-lens evidence summary."""

    key: str
    label: str
    score: float = 0.0
    positive_matches: int = 0
    negative_matches: int = 0
    matched_terms: List[str] = field(default_factory=list)
    contradicting_terms: List[str] = field(default_factory=list)
    supporting_records: List[str] = field(default_factory=list)
    expert_verification_required: bool = False
    note: str = ""


@dataclass
class ScanScore:
    """Aggregate outcome of scoring one retrieval against one claim."""

    support_level: str = "none"
    aggregate_score: float = 0.0
    direct_hits: int = 0
    partial_hits: int = 0
    records_scored: int = 0
    term_coverage: float = 0.0
    unmatched_terms: List[str] = field(default_factory=list)
    lens_scores: List[LensScore] = field(default_factory=list)
    limiting_factor: str = ""
    downgraded: bool = False
    downgrade_reason: str = ""


# --------------------------------------------------------------------------
# Tokenisation
# --------------------------------------------------------------------------

def tokenise(text: str) -> List[str]:
    """Lowercase word tokens, hyphens retained.

    Hyphens are kept because compound domain terms such as 'self-healing' and
    'anti-fouling' lose their meaning when split, and splitting them would
    inflate match counts by counting each fragment separately.
    """
    return _WORD.findall((text or "").lower())


def _record_text(record: Record) -> str:
    return f"{record.title} {record.abstract}"


def _term_present(term: str, tokens: Sequence[str], text: str) -> bool:
    """Whether a single- or multi-word term appears in a record.

    Single words are matched against the token set to avoid substring false
    positives ('ion' inside 'version'). Multi-word terms are matched as
    substrings on the joined text, since token-level matching cannot express
    adjacency without a positional index.
    """
    if " " in term:
        return term in text
    return term in tokens


# --------------------------------------------------------------------------
# Per-record coverage
# --------------------------------------------------------------------------

def term_coverage(record: Record, terms: Sequence[str]) -> Tuple[float, List[str]]:
    """Fraction of claim terms present in one record, with the matched subset."""
    if not terms:
        return 0.0, []
    text = _record_text(record).lower()
    tokens = set(tokenise(text))
    matched = [term for term in terms if _term_present(term, tokens, text)]
    return len(matched) / len(terms), matched


def classify_records(records: Sequence[Record], terms: Sequence[str]) -> Tuple[int, int, List[str], Dict[str, float]]:
    """Partition records into direct and partial hits by term coverage.

    Coverage is used rather than a binary relevance judgement because the system
    has no mechanism for judging relevance. Coverage is a measurable proxy and
    its threshold is declared, so a reader can disagree with it explicitly.
    """
    direct = 0
    partial = 0
    matched_union: List[str] = []
    seen: set = set()
    per_record: Dict[str, float] = {}

    for record in records:
        coverage, matched = term_coverage(record, terms)
        per_record[f"{record.source}:{record.identifier}"] = round(coverage, 4)
        if coverage >= DIRECT_HIT_COVERAGE:
            direct += 1
        elif coverage >= PARTIAL_HIT_COVERAGE:
            partial += 1
        for term in matched:
            if term not in seen:
                seen.add(term)
                matched_union.append(term)

    return direct, partial, matched_union, per_record


# --------------------------------------------------------------------------
# Saturation
# --------------------------------------------------------------------------

def saturate(count: int, k: float = SATURATION_K) -> float:
    """Map a match count into [0, 1) with diminishing returns.

    Linear counting would let a single well-indexed topic dominate the
    aggregate: fifty records on one mechanism would outweigh a sparse but
    genuine literature. Logarithmic saturation bounds each component so that
    breadth across lenses matters more than depth within one.
    """
    if count <= 0:
        return 0.0
    return math.log1p(count) / math.log1p(count + k)


# --------------------------------------------------------------------------
# Lens scoring
# --------------------------------------------------------------------------

def score_lens(lens_key: str, records: Sequence[Record]) -> LensScore:
    """Score one lens over the retrieved records.

    Negative vocabulary is counted separately and subtracted, never used to
    cancel a match silently. A lens with both strong positive and strong
    negative signal is a contested lens; reporting only the difference would
    hide the contestation.
    """
    lens = get_lens(lens_key)
    positives = [term.lower() for term in get_positive_terms(lens_key)]
    negatives = [term.lower() for term in get_negative_terms(lens_key)]

    result = LensScore(
        key=lens.key,
        label=lens.label,
        expert_verification_required=requires_expert_verification(lens_key),
    )
    if not records:
        result.note = "No scorable records retrieved; this lens was not evaluated."
        return result

    matched: List[str] = []
    contradicting: List[str] = []
    supporting: List[str] = []

    for record in records:
        text = _record_text(record).lower()
        tokens = set(tokenise(text))

        hits = [term for term in positives if _term_present(term, tokens, text)]
        misses = [term for term in negatives if _term_present(term, tokens, text)]

        if hits:
            result.positive_matches += len(hits)
            supporting.append(f"{record.source}:{record.identifier}")
            matched.extend(term for term in hits if term not in matched)
        if misses:
            result.negative_matches += len(misses)
            contradicting.extend(term for term in misses if term not in contradicting)

    positive_component = saturate(result.positive_matches)
    negative_component = saturate(result.negative_matches)

    result.score = round(max(0.0, positive_component - 0.5 * negative_component), 4)
    result.matched_terms = matched[:12]
    result.contradicting_terms = contradicting[:12]
    result.supporting_records = supporting[:12]

    if result.negative_matches and result.positive_matches:
        result.note = (
            "Both supporting and contradicting vocabulary present. "
            "The literature on this lens is contested and requires reading, not counting."
        )
    elif result.expert_verification_required and result.score > 0:
        result.note = (
            "Lexical signal only. This lens cannot be established without "
            "domain-expert verification of the underlying studies."
        )

    return result


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _support_level(score: float) -> str:
    for threshold, level in SUPPORT_THRESHOLDS:
        if score >= threshold:
            return level
    return "none"


def _downgrade_for_thin_evidence(level: str, record_count: int) -> Tuple[str, str]:
    """Cap the support level when too few records underlie it.

    A high aggregate over two records is an artefact of small numbers, not
    evidence of consensus. The cap is applied after threshold mapping and
    reported, so the raw score and the reported level remain separately legible.
    """
    if level == "strong" and record_count < MIN_RECORDS_FOR_STRONG:
        return "moderate", (
            f"Capped at moderate: {record_count} scorable records is below the "
            f"minimum of {MIN_RECORDS_FOR_STRONG} required for a strong level."
        )
    if level in ("strong", "moderate") and record_count < MIN_RECORDS_FOR_MODERATE:
        return "weak", (
            f"Capped at weak: {record_count} scorable records is below the "
            f"minimum of {MIN_RECORDS_FOR_MODERATE} required for a moderate level."
        )
    return level, ""


def _limiting_factor(result: SourceResult, score: ScanScore) -> str:
    """The single most consequential constraint on this result.

    Surfaced so that a low support level is attributable. 'No evidence found'
    and 'one index was unreachable' are indistinguishable in the number alone.
    """
    hard_errors = [name for name in result.errors if not name.endswith("_note")]
    if hard_errors:
        return f"Retrieval incomplete: {', '.join(hard_errors)} unavailable."
    if result.relaxed:
        return (
            "Strict query returned too few records; a relaxed query was executed. "
            "This result answers a broader question than the one posed."
        )
    if score.records_scored == 0:
        return "No records with usable abstracts were retrieved."
    dropped = result.raw_counts.get("deduplicated", 0) - result.raw_counts.get("scorable", 0)
    if dropped > score.records_scored:
        return f"{dropped} retrieved records lacked usable abstracts and were excluded."
    if score.direct_hits == 0:
        return "No record matched a majority of claim terms; all signal is partial."
    return ""


def score_scan(
    plan: QueryPlan,
    result: SourceResult,
    lens_keys: Optional[Sequence[str]] = None,
) -> ScanScore:
    """Score a completed retrieval against the plan that produced it.

    Claim vocabulary is taken from the plan rather than re-derived from the claim
    text. Re-deriving it would allow the retrieved text and the scored text to
    diverge, so that the score would describe a query that was never executed.
    """
    records = result.records
    terms = plan.all_terms
    selected = list(lens_keys) if lens_keys else [lens.key for lens in LENSES]

    score = ScanScore(records_scored=len(records))

    direct, partial, matched, _ = classify_records(records, terms)
    score.direct_hits = direct
    score.partial_hits = partial
    score.term_coverage = round(len(matched) / len(terms), 4) if terms else 0.0
    score.unmatched_terms = [term for term in terms if term not in matched]

    score.lens_scores = [score_lens(key, records) for key in selected]

    # The aggregate weights retrieval strength and lens breadth equally. Either
    # alone is misleading: many records matching no lens vocabulary indicate a
    # mis-specified query, and strong lens signal over two records indicates a
    # small sample.
    retrieval_component = 0.7 * saturate(direct) + 0.3 * saturate(partial)
    lens_values = [item.score for item in score.lens_scores]
    lens_component = sum(lens_values) / len(lens_values) if lens_values else 0.0

    score.aggregate_score = round(
        0.5 * retrieval_component + 0.5 * lens_component * max(score.term_coverage, 0.2),
        4,
    )

    level = _support_level(score.aggregate_score)
    capped, reason = _downgrade_for_thin_evidence(level, score.records_scored)
    score.support_level = capped
    score.downgraded = bool(reason)
    score.downgrade_reason = reason
    score.limiting_factor = _limiting_factor(result, score)

    return score