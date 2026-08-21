"""
Lens evaluation over retrieved records.

A lens counts the records in which its vocabulary appears. This measures how
often a topic is discussed in the retrieved abstracts. It does not measure
whether the claim is true, whether the discussion reached a conclusion, or
whether that conclusion was favourable.

Two constraints are enforced here rather than left to the interface, because a
constraint that lives only in a template is lost the first time the template is
reused:

  1. Lenses marked as requiring expert verification receive no support level
     and no count-derived label. They return an unanswered question. Attaching
     a number to a question that lexical evidence cannot settle invites the
     reader to treat it as settled.

  2. No composite score is produced. The lenses share no scale and none has
     been calibrated against an independently rater-labelled corpus, so their
     sum would carry an appearance of precision that the inputs do not support.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, asdict

import domain_config as dc
from query_builder import QueryPlan
from retrieval import Record, RetrievalResult

SCORING_SCHEMA = "ecosentia-scoring-1"

# Below this many analysable records, differences between lens counts reflect
# which records happened to be retrieved rather than any property of the
# literature. Support is capped at "weak" and the reason is reported.
MIN_CORPUS_FOR_STRENGTH = 5

# A lens reaching "strong" on records that all share an author is describing
# one group's output, not a body of independent work. The cap is applied
# automatically because the operator cannot see author overlap in a count.
MIN_DISTINCT_AUTHORS_FOR_STRONG = 2


class ScoringError(RuntimeError):
    """Raised when scoring is attempted on a retrieval that cannot support it."""


# ---------------------------------------------------------------------------
# Term matching
# ---------------------------------------------------------------------------
# Matching is literal, case-insensitive, and bounded by non-alphanumeric
# characters, with one concession: a trailing plural is accepted. There is no
# stemming. Stemming would make matches unpredictable for a reader checking a
# result by hand, and hand-checking is the only validation this instrument
# currently has.

_PLURAL = r"(?:e?s)?"


def _compile_term(term: str) -> re.Pattern:
    """
    Build a bounded pattern for one vocabulary entry.

    Internal spaces also match hyphens, so that "life cycle" matches
    "life-cycle". The two spellings are equally common in this literature and
    treating them as different terms would split one signal into two weak ones.
    """
    parts = [re.escape(part) for part in term.strip().split()]
    body = r"[\s\-]+".join(parts)
    return re.compile(
        rf"(?<![A-Za-z0-9]){body}{_PLURAL}(?![A-Za-z0-9])", re.IGNORECASE
    )


_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _pattern(term: str) -> re.Pattern:
    if term not in _PATTERN_CACHE:
        _PATTERN_CACHE[term] = _compile_term(term)
    return _PATTERN_CACHE[term]


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if _pattern(term).search(text)]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class LensResult:
    key: str
    label: str
    question: str
    detects: str
    status: str                      # "scored" | "requires_expert" | "not_assessable"
    support: str | None              # support level key, or None when unscored
    support_label: str | None
    support_meaning: str | None
    records_matched: int
    records_analysed: int
    matched_terms: list[str] = field(default_factory=list)
    negative_terms_present: list[str] = field(default_factory=list)
    distinct_first_authors: int = 0
    caps_applied: list[str] = field(default_factory=list)
    epistemic: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoringResult:
    lenses: list[LensResult]
    records_analysed: int
    records_title_only: int
    records_in_both_sources: int
    corpus_note: str
    composite_score: None = None
    composite_note: str = (
        "No composite score is produced. The lenses answer different questions "
        "on different scales and none has been calibrated against an "
        "independently labelled corpus. Combining them would present an "
        "arithmetic result as a measurement."
    )
    schema: str = SCORING_SCHEMA

    @property
    def unanswered(self) -> list[LensResult]:
        return [lens for lens in self.lenses if lens.status != "scored"]

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "lenses": [lens.to_dict() for lens in self.lenses],
            "records_analysed": self.records_analysed,
            "records_title_only": self.records_title_only,
            "records_in_both_sources": self.records_in_both_sources,
            "corpus_note": self.corpus_note,
            "composite_score": self.composite_score,
            "composite_note": self.composite_note,
            "unanswered_questions": [
                {"lens": lens.label, "question": lens.question, "note": lens.note}
                for lens in self.unanswered
            ],
        }


# ---------------------------------------------------------------------------
# Support assignment
# ---------------------------------------------------------------------------

def _assign_support(
    matched: int, analysed: int, distinct_authors: int, negatives: int
) -> tuple[str, list[str]]:
    """
    Map a record count to a support level, applying documented caps.

    Caps are returned alongside the level so that a downgrade is visible. A
    level that had been reduced without explanation would be indistinguishable
    from a genuinely weaker signal, and the operator would draw the wrong
    conclusion about which one they were looking at.
    """
    caps: list[str] = []

    if matched == 0:
        return "none", caps
    if matched <= 2:
        level = "weak"
    elif matched <= 5:
        level = "moderate"
    else:
        level = "strong"

    if level == "strong" and analysed < MIN_CORPUS_FOR_STRENGTH:
        level = "weak"
        caps.append(
            f"Capped: only {analysed} analysable record(s), too few to "
            "distinguish a strong signal from the retrieval sample."
        )
    elif level == "moderate" and analysed < MIN_CORPUS_FOR_STRENGTH:
        level = "weak"
        caps.append(
            f"Capped: only {analysed} analysable record(s) in the corpus."
        )

    if level == "strong" and distinct_authors < MIN_DISTINCT_AUTHORS_FOR_STRONG:
        level = "moderate"
        caps.append(
            "Capped: matching records share a first author, so they represent "
            "one group's output rather than independent work."
        )

    if level in ("strong", "moderate") and negatives >= matched:
        level = "weak"
        caps.append(
            "Capped: qualifying or contrary vocabulary appears at least as "
            "often as the lens vocabulary itself."
        )

    return level, caps


def _first_author_key(record: Record) -> str:
    """
    A coarse identity for the leading author.

    Name matching conflates common surnames and misses name changes. It is used
    only to withhold the strongest label, never to exclude a record, so its
    errors reduce a claim's apparent support rather than removing evidence.
    """
    if not record.authors:
        return f"__unknown__{record.identifier}"
    return record.authors[0].strip().lower()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _evaluate_lens(lens: dc.Lens, records: list[Record]) -> LensResult:
    matching: list[Record] = []
    all_terms: Counter = Counter()
    negative_hits: set[str] = set()

    for record in records:
        text = record.text
        hits = _matched_terms(text, lens.positive)
        if hits:
            matching.append(record)
            all_terms.update(hits)
        negative_hits.update(_matched_terms(text, lens.negative))

    distinct_authors = len({_first_author_key(r) for r in matching})

    result = LensResult(
        key=lens.key,
        label=lens.label,
        question=lens.question,
        detects=lens.detects,
        status="scored",
        support=None,
        support_label=None,
        support_meaning=None,
        records_matched=len(matching),
        records_analysed=len(records),
        matched_terms=[term for term, _ in all_terms.most_common()],
        negative_terms_present=sorted(negative_hits),
        distinct_first_authors=distinct_authors,
        epistemic=lens.epistemic,
    )

    if lens.requires_expert:
        # The question is recorded and left open. Supplying a count here would
        # be read as a partial answer, and a partial answer to a question that
        # abstract text cannot address is worse than none.
        result.status = "requires_expert"
        result.note = (
            f"{lens.detects} This question cannot be settled from abstract "
            "text and is recorded as open pending expert judgement."
        )
        return result

    level, caps = _assign_support(
        matched=len(matching),
        analysed=len(records),
        distinct_authors=distinct_authors,
        negatives=len(negative_hits),
    )
    support = dc.SUPPORT_BY_KEY[level]
    result.support = support.key
    result.support_label = support.label
    result.support_meaning = support.meaning
    result.caps_applied = caps
    result.note = lens.detects
    return result


def score(plan: QueryPlan, retrieval: RetrievalResult) -> ScoringResult:
    """
    Evaluate the selected lenses over the retrieved records.

    Raises ScoringError when no source answered. A retrieval in which every
    source failed contains no information about the claim, and scoring it would
    convert an infrastructure failure into a finding of weak support. The
    caller is expected to report the failure instead.
    """
    if not retrieval.usable:
        raise ScoringError(
            "no source answered; this retrieval cannot be scored in either "
            "direction"
        )

    records = retrieval.records
    title_only = [r for r in records if not r.has_abstract]
    both_sources = sum(1 for r in records if len(r.seen_in) > 1)

    lenses = [
        _evaluate_lens(dc.get_lens(key), records) for key in plan.lenses
    ]

    parts = [
        f"{len(records)} record(s) analysed after merging "
        f"{retrieval.duplicates_merged} duplicate(s)."
    ]
    if both_sources:
        parts.append(
            f"{both_sources} appeared in both indexes and are counted once."
        )
    if title_only:
        parts.append(
            f"{len(title_only)} carry no abstract, so lens vocabulary could "
            "only be sought in their titles; their absence from a lens is "
            "uninformative."
        )
    if len(records) < MIN_CORPUS_FOR_STRENGTH:
        parts.append(
            "The corpus is too small for support levels above weak; caps have "
            "been applied and are listed per lens."
        )
    parts.append(
        "Counts describe the retrieved abstracts, not the literature as a whole."
    )

    return ScoringResult(
        lenses=lenses,
        records_analysed=len(records),
        records_title_only=len(title_only),
        records_in_both_sources=both_sources,
        corpus_note=" ".join(parts),
    )