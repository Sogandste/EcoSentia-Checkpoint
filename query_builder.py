"""
Construction of an inspectable query plan from an operator's claim.

The plan is produced, shown, and approved before any external request is made.
This ordering is deliberate: a tool that queries first and explains afterwards
gives the operator a result to react to rather than a method to accept, and an
operator who has not seen the query cannot judge what its absence of hits means.

No claim term is invented. Every term in a query is either taken verbatim from
the operator's text or drawn from the published preset vocabulary, so that the
mapping from claim to query can be checked by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

import domain_config as dc

PLAN_SCHEMA = "ecosentia-plan-1"

# Function words carry no discriminative power in an abstract index but would
# match nearly every record, diluting the query. The list is intentionally
# short: aggressive stopword removal has been observed to strip meaningful
# terms such as "against" or "without" that reverse a claim's direction.
STOPWORDS: frozenset[str] = frozenset("""
a an the and or but if then than that this these those of in on at to for from
with without into onto over under by is are was were be been being has have had
it its as such can could may might will would should must do does did not no
""".split())

# Terms shorter than this match too broadly in abstract text to be useful.
MIN_TERM_LENGTH = 3

# Above this, a query becomes so specific that an empty result reports the
# narrowness of the query rather than the state of the literature.
MAX_CLAIM_TERMS = 8

_QUOTED = re.compile(r'"([^"]{2,120})"')
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")


# ---------------------------------------------------------------------------
# Dialect disclosure
# ---------------------------------------------------------------------------
# The same boolean expression is not the same query in both indexes. Recording
# the differences with the plan prevents a later reader from treating two
# source counts as two measurements of one quantity.

DIALECT_NOTES: dict[str, tuple[str, ...]] = {
    "pubmed": (
        "Terms are restricted to title and abstract with [tiab], which "
        "suppresses PubMed's automatic term mapping and MeSH explosion.",
        "Quoted phrases match as phrases without stemming; a singular form "
        "will not match its plural unless both are supplied.",
    ),
    "openalex": (
        "title_and_abstract.search applies OpenAlex tokenisation and stemming, "
        "so term matching is broader than the PubMed equivalent.",
        "Commas act as filter separators in the OpenAlex query syntax and are "
        "removed from search terms; a phrase containing a comma is matched "
        "without it.",
        "Coverage extends beyond biomedicine, so the same query returns a "
        "different disciplinary mix.",
    ),
}

CROSS_SOURCE_NOTE = (
    "PubMed and OpenAlex interpret this query differently. Record counts from "
    "the two sources are not directly comparable and are reported separately "
    "for that reason."
)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueryPlan:
    claim: str
    preset: str
    preset_label: str
    anchors: tuple[str, ...]
    claim_terms: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    matched_vocabulary: tuple[str, ...]
    dropped_terms: tuple[str, ...]
    negative_matches: tuple[str, ...]
    lenses: tuple[str, ...]
    queries: dict[str, str]
    dialect_notes: dict[str, tuple[str, ...]]
    cross_source_note: str
    warnings: tuple[str, ...]
    max_records: int
    config_version: str
    schema: str = PLAN_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Term extraction
# ---------------------------------------------------------------------------

def _extract_quoted(claim: str) -> tuple[list[str], str]:
    """
    Pull explicit phrases out of the claim before tokenisation.

    Quoting is the operator's only means of insisting that words stay together.
    Tokenising first would silently discard that instruction.
    """
    phrases = [m.group(1).strip() for m in _QUOTED.finditer(claim)]
    remainder = _QUOTED.sub(" ", claim)
    return phrases, remainder


def _extract_terms(text: str) -> tuple[list[str], list[str]]:
    """
    Return retained terms and terms deliberately dropped.

    Dropped terms are returned rather than discarded so the operator can see
    what was removed. A term silently omitted from a query is the difference
    between "not found" and "not searched for", and the operator is the only
    person able to tell which mattered.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        lowered = token.lower()
        if lowered in kept or lowered in dropped:
            continue
        if lowered in STOPWORDS or len(lowered) < MIN_TERM_LENGTH:
            dropped.append(lowered)
        else:
            kept.append(lowered)
    return kept, dropped


def _match_vocabulary(claim_lower: str, terms: tuple[str, ...]) -> list[str]:
    """Return preset vocabulary entries that appear in the claim as written."""
    return [term for term in terms if term.lower() in claim_lower]


# ---------------------------------------------------------------------------
# Query rendering
# ---------------------------------------------------------------------------

def _render_pubmed(anchors: list[str], terms: list[str]) -> str:
    """
    Anchors as a required OR group, claim terms conjoined.

    The anchor group keeps retrieval inside the subject area, so that a generic
    claim term does not pull in records from an unrelated field that happens to
    share the vocabulary.
    """
    anchor_clause = " OR ".join(f'"{a}"[tiab]' for a in anchors)
    parts = [f"({anchor_clause})"]
    for term in terms:
        quoted = f'"{term}"[tiab]' if " " in term else f"{term}[tiab]"
        parts.append(quoted)
    return " AND ".join(parts)


def _render_openalex(anchors: list[str], terms: list[str]) -> str:
    """
    The same logical structure in OpenAlex filter syntax.

    Commas are stripped rather than escaped: in this syntax a comma separates
    filters, and a term containing one would silently truncate the query into a
    different and broader search.
    """
    def clean(value: str) -> str:
        return value.replace(",", " ").replace("|", " ").strip()

    anchor_clause = " OR ".join(f'"{clean(a)}"' for a in anchors)
    parts = [f"({anchor_clause})"]
    for term in terms:
        cleaned = clean(term)
        parts.append(f'"{cleaned}"' if " " in cleaned else cleaned)
    return " AND ".join(parts)


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------

def build_plan(
    claim: str,
    preset: str = dc.DEFAULT_PRESET,
    lenses: list[str] | None = None,
    max_records: int = 50,
) -> QueryPlan:
    """
    Translate a claim into a query plan for operator approval.

    Raises ValueError when no usable term survives extraction. Returning an
    anchor-only query in that case would retrieve the whole subject area and
    present it as support for a claim that was never searched for.
    """
    claim = (claim or "").strip()
    if len(claim) < 8:
        raise ValueError("claim is too short to form a query")

    preset_obj = dc.get_preset(preset)
    selected_lenses = dc.resolve_lenses(lenses)

    quoted, remainder = _extract_quoted(claim)
    tokens, dropped = _extract_terms(remainder)
    claim_lower = claim.lower()

    warnings: list[str] = []

    # Vocabulary matches are ranked ahead of free tokens: they are curated,
    # published, and multi-word, so they discriminate better in abstract text.
    matched_vocab = _match_vocabulary(claim_lower, preset_obj.positive_terms)
    ordered = list(dict.fromkeys(quoted + matched_vocab + tokens))

    # Anchors already constrain the subject area, so repeating an anchor as a
    # claim term would narrow retrieval without adding information.
    anchor_lower = {a.lower() for a in preset_obj.anchors}
    ordered = [t for t in ordered if t.lower() not in anchor_lower]

    if not ordered:
        raise ValueError(
            "no usable search term remains after removing function words and "
            "preset anchors; rephrase the claim with the specific mechanism, "
            "material, or outcome"
        )

    if len(ordered) > MAX_CLAIM_TERMS:
        warnings.append(
            f"The claim yielded {len(ordered)} terms; the query uses the first "
            f"{MAX_CLAIM_TERMS}. Remaining terms were not searched for."
        )
        ordered = ordered[:MAX_CLAIM_TERMS]

    if len(ordered) == 1:
        warnings.append(
            "Only one claim term was extracted. Retrieval will be broad and "
            "may return records related to the subject area rather than to "
            "this specific claim."
        )

    if not matched_vocab and not quoted:
        warnings.append(
            "No preset vocabulary term appears in the claim. The query rests "
            "entirely on free text from the claim, which matches abstract "
            "wording less reliably than curated terms."
        )

    negative_matches = _match_vocabulary(claim_lower, preset_obj.negative_terms)
    if negative_matches:
        warnings.append(
            "The claim contains vocabulary associated with a different subject "
            f"area ({', '.join(negative_matches)}). Retrieved records may "
            "belong to that area. These terms are reported, not excluded; "
            "judging their relevance is the operator's decision."
        )

    anchors = list(preset_obj.anchors)
    return QueryPlan(
        claim=claim,
        preset=preset_obj.key,
        preset_label=preset_obj.label,
        anchors=tuple(anchors),
        claim_terms=tuple(ordered),
        quoted_phrases=tuple(quoted),
        matched_vocabulary=tuple(matched_vocab),
        dropped_terms=tuple(dropped),
        negative_matches=tuple(negative_matches),
        lenses=selected_lenses,
        queries={
            "pubmed": _render_pubmed(anchors, ordered),
            "openalex": _render_openalex(anchors, ordered),
        },
        dialect_notes=DIALECT_NOTES,
        cross_source_note=CROSS_SOURCE_NOTE,
        warnings=tuple(warnings),
        max_records=max(1, min(int(max_records), 200)),
        config_version=dc.CONFIG_VERSION,
    )


def plan_from_dict(data: dict) -> QueryPlan:
    """
    Rebuild an approved plan for execution.

    The scan endpoint reconstructs the plan the operator saw rather than
    rebuilding it from the claim. If vocabulary changed between approval and
    execution, a rebuilt plan would differ from the one that was approved and
    the audit entry would describe a query that was never shown to anyone.
    """
    if data.get("schema") != PLAN_SCHEMA:
        raise ValueError("unrecognised plan schema")
    return QueryPlan(
        claim=data["claim"],
        preset=data["preset"],
        preset_label=data["preset_label"],
        anchors=tuple(data["anchors"]),
        claim_terms=tuple(data["claim_terms"]),
        quoted_phrases=tuple(data.get("quoted_phrases", ())),
        matched_vocabulary=tuple(data.get("matched_vocabulary", ())),
        dropped_terms=tuple(data.get("dropped_terms", ())),
        negative_matches=tuple(data.get("negative_matches", ())),
        lenses=tuple(data["lenses"]),
        queries=dict(data["queries"]),
        dialect_notes={k: tuple(v) for k, v in data.get("dialect_notes", {}).items()},
        cross_source_note=data.get("cross_source_note", CROSS_SOURCE_NOTE),
        warnings=tuple(data.get("warnings", ())),
        max_records=int(data.get("max_records", 50)),
        config_version=data.get("config_version", dc.CONFIG_VERSION),
    )