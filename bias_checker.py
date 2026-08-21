"""
Structural checks over a completed scan.

The checks examine the shape of the retrieved evidence: who produced it, when,
where it was published, and how it was gathered. These are properties a support
level cannot express. A lens can report strong lexical support over records
that all originate from one laboratory in one year, and nothing in the number
would reveal it.

Three severities are used, and the third is the reason this module exists in
its present form:

  flag            an observable property of this scan that weakens inference
  note            context the operator should hold while reading the result
  not_assessable  a known threat this tool cannot examine at all

Reporting only the first two would let a clean result read as a clearance. The
unassessable category keeps the instrument's blind spots on the same screen as
its findings, at the same level of prominence.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict

import domain_config as dc
from query_builder import QueryPlan
from retrieval import RetrievalResult
from scoring import ScoringResult

BIAS_SCHEMA = "ecosentia-bias-1"

# Above this share from a single first author, the corpus describes a group
# rather than a field.
AUTHOR_CONCENTRATION = 0.50

# Above this share from one venue, the result reflects an editorial scope.
VENUE_CONCENTRATION = 0.50

# Above this share within a three-year window, the corpus may capture a topic's
# current fashion rather than its accumulated evidence.
RECENCY_CONCENTRATION = 0.70

SMALL_CORPUS = 5


@dataclass
class Finding:
    key: str
    severity: str
    title: str
    detail: str
    action: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Checks over the retrieved corpus
# ---------------------------------------------------------------------------

def _check_corpus_size(retrieval: RetrievalResult) -> Finding | None:
    count = len(retrieval.records)
    if count == 0:
        return Finding(
            key="empty_corpus",
            severity="flag",
            title="No records retrieved",
            detail=(
                "Every queried source answered and none matched this query. "
                "This constrains the claim only within the scope searched: "
                "English-language indexed abstracts, using the terms shown in "
                "the query plan."
            ),
            action=(
                "Before treating this as absence of evidence, rerun with "
                "broader terms and check whether the claim's vocabulary "
                "matches how the field words it."
            ),
        )
    if count < SMALL_CORPUS:
        return Finding(
            key="small_corpus",
            severity="flag",
            title=f"Only {count} record(s) retrieved",
            detail=(
                "Support levels computed over this few records reflect which "
                "records happened to be indexed and retrieved, not the balance "
                "of the literature."
            ),
            action="Treat all lens output as provisional and read every record.",
        )
    return None


def _check_author_concentration(retrieval: RetrievalResult) -> Finding | None:
    authors = [r.authors[0].strip().lower() for r in retrieval.records if r.authors]
    if len(authors) < 3:
        return None
    top_author, top_count = Counter(authors).most_common(1)[0]
    share = top_count / len(authors)
    if share < AUTHOR_CONCENTRATION:
        return None
    return Finding(
        key="author_concentration",
        severity="flag",
        title="Evidence concentrated in one group",
        detail=(
            f"{top_count} of {len(authors)} records with author data share the "
            f"first author '{top_author}' ({share:.0%}). Record counts treat "
            "these as independent observations; they are not."
        ),
        action=(
            "Check whether the supporting records report distinct experiments "
            "or restate one dataset."
        ),
    )


def _check_venue_concentration(retrieval: RetrievalResult) -> Finding | None:
    venues = [r.venue.strip().lower() for r in retrieval.records if r.venue.strip()]
    if len(venues) < 4:
        return None
    top_venue, top_count = Counter(venues).most_common(1)[0]
    share = top_count / len(venues)
    if share < VENUE_CONCENTRATION:
        return None
    return Finding(
        key="venue_concentration",
        severity="note",
        title="Evidence concentrated in one venue",
        detail=(
            f"{top_count} of {len(venues)} records appeared in the same venue "
            f"({share:.0%}). A single editorial scope shapes which results and "
            "which framings are published."
        ),
        action="Look for treatments of this claim outside that venue.",
    )


def _check_temporal_skew(retrieval: RetrievalResult) -> Finding | None:
    years = [r.year for r in retrieval.records if r.year]
    if len(years) < 4:
        return None
    newest = max(years)
    recent = sum(1 for y in years if y >= newest - 2)
    share = recent / len(years)
    if share < RECENCY_CONCENTRATION:
        return None
    return Finding(
        key="temporal_skew",
        severity="note",
        title="Evidence concentrated in recent years",
        detail=(
            f"{recent} of {len(years)} dated records fall within "
            f"{newest - 2}–{newest} ({share:.0%}). Recent work is less likely "
            "to have been independently reproduced or corrected."
        ),
        action=(
            "Check whether early results in this area have since been "
            "revised or failed to replicate."
        ),
    )


def _check_source_dependence(retrieval: RetrievalResult) -> Finding | None:
    if retrieval.partial:
        names = ", ".join(s.source for s in retrieval.failed)
        return Finding(
            key="partial_coverage",
            severity="flag",
            title="A source did not respond",
            detail=(
                f"{names} failed during this scan. Counts are a floor over the "
                "sources that answered, and a lens showing no support may be "
                "reporting a missing index rather than a missing literature."
            ),
            action="Rerun before drawing any conclusion from an absence.",
        )
    if any(s.truncated for s in retrieval.sources):
        totals = ", ".join(
            f"{s.source}: {s.total_available}"
            for s in retrieval.sources
            if s.truncated and s.total_available
        )
        return Finding(
            key="truncated_results",
            severity="note",
            title="Results truncated at the record limit",
            detail=(
                f"More records matched than were retrieved ({totals}). Records "
                "were taken in the source's relevance order, which is itself an "
                "unexamined ranking."
            ),
            action=(
                "Raise the record limit if the balance of evidence matters to "
                "the decision."
            ),
        )
    return None


def _check_title_only(scoring: ScoringResult) -> Finding | None:
    if not scoring.records_title_only:
        return None
    return Finding(
        key="title_only_records",
        severity="note",
        title=f"{scoring.records_title_only} record(s) without abstracts",
        detail=(
            "Lens vocabulary could only be sought in the titles of these "
            "records. Their absence from a lens carries no information."
        ),
        action="Retrieve these records directly if they appear relevant.",
    )


# ---------------------------------------------------------------------------
# Checks over the query and the scan design
# ---------------------------------------------------------------------------

def _check_confirmation_orientation(plan: QueryPlan) -> Finding:
    """
    Always reported.

    The query is built from the claim, so it selects for records phrased like
    the claim. This is not a defect to be fixed; it is the operating principle
    of the tool, and it must therefore be stated on every scan rather than
    flagged on some.
    """
    return Finding(
        key="confirmation_orientation",
        severity="flag",
        title="The query was built from the claim",
        detail=(
            "Retrieval selects records whose wording resembles the claim, so "
            "the corpus is oriented towards agreement. A study reporting that "
            "this mechanism fails may use vocabulary the query never matched."
        ),
        action=(
            "Run a second scan with the claim's opposite phrased in the field's "
            "own terms, and compare what each returns."
        ),
    )


def _check_negative_vocabulary(plan: QueryPlan) -> Finding | None:
    if not plan.negative_matches:
        return None
    return Finding(
        key="off_domain_vocabulary",
        severity="flag",
        title="Claim contains vocabulary from another subject area",
        detail=(
            "The claim uses terms associated with a different field "
            f"({', '.join(plan.negative_matches)}). Records from that field "
            "may have been retrieved and counted as support."
        ),
        action="Inspect the record list for material outside the intended area.",
    )


def _check_dropped_terms(plan: QueryPlan) -> Finding | None:
    substantive = [t for t in plan.dropped_terms if len(t) >= dc_min_length()]
    if not substantive:
        return None
    return Finding(
        key="terms_not_searched",
        severity="note",
        title="Some claim wording was not searched for",
        detail=(
            "These words were removed before querying: "
            f"{', '.join(substantive)}. If any carried the claim's meaning, "
            "the query tested something narrower than intended."
        ),
        action="Requote essential wording in the claim and rebuild the plan.",
    )


def dc_min_length() -> int:
    # Imported lazily to keep the query construction threshold defined in one
    # place; duplicating the constant would let the two drift apart unnoticed.
    from query_builder import MIN_TERM_LENGTH
    return MIN_TERM_LENGTH


def _check_caps(scoring: ScoringResult) -> Finding | None:
    capped = [lens for lens in scoring.lenses if lens.caps_applied]
    if not capped:
        return None
    lines = "; ".join(
        f"{lens.label}: {' '.join(lens.caps_applied)}" for lens in capped
    )
    return Finding(
        key="support_capped",
        severity="note",
        title="Support levels reduced by structural limits",
        detail=lines,
        action=(
            "The displayed level is lower than the raw count would give. Read "
            "the reason before treating the reduction as a weak literature."
        ),
    )


def _check_open_questions(scoring: ScoringResult) -> Finding | None:
    open_lenses = [lens for lens in scoring.lenses if lens.status == "requires_expert"]
    if not open_lenses:
        return None
    names = ", ".join(lens.label for lens in open_lenses)
    return Finding(
        key="expert_questions_open",
        severity="flag",
        title="Questions left open for expert judgement",
        detail=(
            f"These lenses returned no level because abstract text cannot "
            f"settle them: {names}. They are unanswered, not satisfied."
        ),
        action="Record a judgement against each before citing this scan.",
    )


# ---------------------------------------------------------------------------
# Declared blind spots
# ---------------------------------------------------------------------------
# Fixed entries, returned on every scan. They name threats the tool cannot
# examine. Omitting them when nothing else is flagged would present the absence
# of detectable problems as the absence of problems.

_BLIND_SPOTS: tuple[Finding, ...] = (
    Finding(
        key="language_coverage",
        severity="not_assessable",
        title="Non-English literature",
        detail=(
            "Both indexes are English-dominant and the vocabulary is English. "
            "Work published in other languages was not searched and its volume "
            "cannot be estimated from this scan."
        ),
        action="Consult regional indexes where the subject area warrants it.",
    ),
    Finding(
        key="full_text",
        severity="not_assessable",
        title="Content beyond the abstract",
        detail=(
            "Methods, results, limitations, and conflict-of-interest statements "
            "sit in full text. A study whose abstract omits its limitations is "
            "indistinguishable here from one that has none."
        ),
        action="Read the full text of any record the conclusion depends on.",
    ),
    Finding(
        key="direction_of_finding",
        severity="not_assessable",
        title="Whether a record supports or contradicts the claim",
        detail=(
            "Term co-occurrence is direction-blind. A record reporting that a "
            "mechanism does not work contains the same vocabulary as one "
            "reporting that it does."
        ),
        action="Confirm the direction of each supporting record by reading it.",
    ),
    Finding(
        key="publication_bias",
        severity="not_assessable",
        title="Unpublished and null results",
        detail=(
            "Negative findings are published less often and indexed less "
            "consistently. Nothing in a retrieval over published abstracts can "
            "reveal what was never published."
        ),
        action="Check trial registries and preprint servers where applicable.",
    ),
    Finding(
        key="lens_validation",
        severity="not_assessable",
        title="Agreement between lens output and expert judgement",
        detail=(
            "The lens vocabularies have not been validated against an "
            "independently rater-labelled corpus. Their agreement with expert "
            "assessment is unmeasured, so lens output has no known error rate."
        ),
        action="Treat every lens result as a prompt to look, not as a finding.",
    ),
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def check(
    plan: QueryPlan, retrieval: RetrievalResult, scoring: ScoringResult
) -> dict:
    """
    Run all structural checks and return findings with a summary.

    The summary states explicitly that an absence of flags is not a clearance.
    Without that sentence, a scan with no findings is the output most likely to
    be over-read, precisely because it looks like a result.
    """
    findings: list[Finding] = [_check_confirmation_orientation(plan)]

    for candidate in (
        _check_negative_vocabulary(plan),
        _check_dropped_terms(plan),
        _check_corpus_size(retrieval),
        _check_source_dependence(retrieval),
        _check_author_concentration(retrieval),
        _check_venue_concentration(retrieval),
        _check_temporal_skew(retrieval),
        _check_title_only(scoring),
        _check_caps(scoring),
        _check_open_questions(scoring),
    ):
        if candidate is not None:
            findings.append(candidate)

    findings.extend(_BLIND_SPOTS)

    counts = Counter(f.severity for f in findings)
    return {
        "schema": BIAS_SCHEMA,
        "findings": [f.to_dict() for f in findings],
        "counts": {
            "flag": counts.get("flag", 0),
            "note": counts.get("note", 0),
            "not_assessable": counts.get("not_assessable", 0),
        },
        "summary": (
            f"{counts.get('flag', 0)} structural flag(s) and "
            f"{counts.get('note', 0)} note(s) were raised, alongside "
            f"{counts.get('not_assessable', 0)} threat(s) this tool cannot "
            "examine. An absence of flags is not a clearance: the checks cover "
            "the shape of the retrieved evidence, not its content or its "
            "correctness."
        ),
        "checklist": dc.get_checklist(plan.lenses),
    }