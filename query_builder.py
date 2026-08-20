"""Claim decomposition and auditable query construction.

The refined query is a first-class output, not an internal detail: it is
returned to the operator, editable, and logged with the scan. A support level
whose query cannot be inspected is not reproducible and therefore not usable
as evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from domain_config import get_anchors, get_exclusions, get_preset

FACET_ORDER: Tuple[str, ...] = (
    "biological_model",
    "target_function",
    "application_context",
    "mechanism_keywords",
)

FACET_LABELS: Dict[str, str] = {
    "biological_model": "Biological model",
    "target_function": "Target function",
    "application_context": "Application context",
    "mechanism_keywords": "Mechanism keywords",
}

# Terms carrying no retrieval value. Kept deliberately short: an aggressive
# stopword list silently deletes domain words such as "surface" or "state".
STOPWORDS: frozenset = frozenset("""
a an the and or but if then than that this these those there here
is are was were be been being am do does did doing have has had having
of in on at by for with without from to into onto over under between among
as about against during before after above below through
it its they them their we our you your he she his her
can could may might must shall should will would
not no nor only just also very more most much many few less least
such same other another each every both either neither
i ii iii iv new novel using used use based study studies paper article
show shows shown demonstrate demonstrates present presents report reports
however therefore thus hence which who whom whose what when where why how
""".split())

MIN_TOKEN_LENGTH = 3
MAX_CLAIM_KEYWORDS = 8
MIN_RECORDS_BEFORE_RELAXATION = 5

_FIELD_SPLIT = re.compile(r"[,;\n\u060C\u061B]+")          # comma, semicolon, newline, Arabic comma/semicolon
_QUOTED_PHRASE = re.compile(r'"([^"]{2,60})"')
_CLAUSE_SPLIT = re.compile(r"[.;:!?()\[\]]+|\s+\b(?:and|or|but|while|whereas|because)\b\s+", re.IGNORECASE)
_NON_WORD = re.compile(r"[^\w\s\-/]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class QueryPlan:
    """Everything needed to execute and to audit a retrieval.

    Both query forms are carried together so that the relaxation decision is
    made by the retrieval layer at run time and recorded, rather than being
    taken silently at construction time.
    """

    claim: str
    preset: str
    facets: Dict[str, List[str]] = field(default_factory=dict)
    claim_keywords: List[str] = field(default_factory=list)
    anchors: List[str] = field(default_factory=list)
    exclusions: List[str] = field(default_factory=list)
    strict_query: str = ""
    relaxed_query: str = ""
    rationale: str = ""

    @property
    def all_terms(self) -> List[str]:
        """Every positive term in the plan, deduplicated, in declaration order."""
        collected: List[str] = []
        for facet in FACET_ORDER:
            collected.extend(self.facets.get(facet, []))
        collected.extend(self.claim_keywords)
        collected.extend(self.anchors)
        return _dedupe_preserving_order(collected)

    @property
    def has_explicit_facets(self) -> bool:
        return any(self.facets.get(facet) for facet in FACET_ORDER)


# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------

def _normalise_term(term: str) -> str:
    cleaned = _NON_WORD.sub(" ", (term or "").strip().lower())
    return _WHITESPACE.sub(" ", cleaned).strip()


def _dedupe_preserving_order(terms: Sequence[str]) -> List[str]:
    """Deduplicate case-insensitively while preserving first-seen order.

    Order is preserved so that two runs of the same claim produce byte-identical
    queries, which is what makes a logged scan reproducible.
    """
    seen: set = set()
    result: List[str] = []
    for term in terms:
        normalised = _normalise_term(term)
        if normalised and normalised not in seen:
            seen.add(normalised)
            result.append(normalised)
    return result


def split_field(value: str) -> List[str]:
    """Split one operator-supplied facet field into terms.

    Quoted spans are extracted first and kept verbatim, so that a multi-word
    phrase the operator marked as a unit is never broken across the delimiter.
    """
    text = (value or "").strip()
    if not text:
        return []
    phrases = [match.group(1) for match in _QUOTED_PHRASE.finditer(text)]
    remainder = _QUOTED_PHRASE.sub(" ", text)
    parts = [part for part in _FIELD_SPLIT.split(remainder) if part.strip()]
    return _dedupe_preserving_order(phrases + parts)


def extract_claim_keywords(claim: str, limit: int = MAX_CLAIM_KEYWORDS) -> List[str]:
    """Derive retrieval terms from free text when facets are not supplied.

    Quoted phrases are honoured, then short noun-like clause fragments, then
    individual content tokens. The result is truncated so that a long claim
    cannot generate a query too narrow to retrieve anything.
    """
    text = (claim or "").strip()
    if not text:
        return []

    candidates: List[str] = [match.group(1) for match in _QUOTED_PHRASE.finditer(text)]
    remainder = _QUOTED_PHRASE.sub(" ", text)

    for clause in _CLAUSE_SPLIT.split(remainder):
        tokens = [
            token for token in _normalise_term(clause).split()
            if len(token) >= MIN_TOKEN_LENGTH and token not in STOPWORDS
        ]
        if 2 <= len(tokens) <= 3:
            candidates.append(" ".join(tokens))
        candidates.extend(tokens)

    return _dedupe_preserving_order(candidates)[:limit]


# --------------------------------------------------------------------------
# Plan construction
# --------------------------------------------------------------------------

def build_plan(
    claim: str,
    preset: str,
    biological_model: str = "",
    target_function: str = "",
    application_context: str = "",
    mechanism_keywords: str = "",
    exclude_terms: str = "",
    override_query: str = "",
) -> QueryPlan:
    """Build the retrieval plan for one claim.

    `override_query` short-circuits construction: when the operator has edited
    the query, that exact string is executed. Silently re-deriving it would
    discard a deliberate expert correction, which is the most valuable input the
    system receives.
    """
    preset_spec = get_preset(preset)

    facets: Dict[str, List[str]] = {
        "biological_model": split_field(biological_model),
        "target_function": split_field(target_function),
        "application_context": split_field(application_context),
        "mechanism_keywords": split_field(mechanism_keywords),
    }

    anchors = _dedupe_preserving_order(get_anchors(preset_spec.key))
    exclusions = _dedupe_preserving_order(
        list(get_exclusions(preset_spec.key)) + split_field(exclude_terms)
    )

    populated = [facet for facet in FACET_ORDER if facets[facet]]
    claim_keywords = [] if populated else extract_claim_keywords(claim)

    if override_query.strip():
        manual = override_query.strip()
        return QueryPlan(
            claim=claim.strip(),
            preset=preset_spec.key,
            facets=facets,
            claim_keywords=claim_keywords,
            anchors=anchors,
            exclusions=exclusions,
            strict_query=manual,
            relaxed_query=manual,
            rationale="Operator-supplied query executed verbatim; no facet expansion applied.",
        )

    plan = QueryPlan(
        claim=claim.strip(),
        preset=preset_spec.key,
        facets=facets,
        claim_keywords=claim_keywords,
        anchors=anchors,
        exclusions=exclusions,
    )

    strict = _render_boolean(_strict_groups(plan), plan.exclusions)
    relaxed = _render_boolean(_relaxed_groups(plan), plan.exclusions)

    return QueryPlan(
        claim=plan.claim,
        preset=plan.preset,
        facets=plan.facets,
        claim_keywords=plan.claim_keywords,
        anchors=plan.anchors,
        exclusions=plan.exclusions,
        strict_query=strict,
        relaxed_query=relaxed,
        rationale=_build_rationale(plan, populated),
    )


def _strict_groups(plan: QueryPlan) -> List[List[str]]:
    """Conjunctive groups: anchors AND each populated facet."""
    groups: List[List[str]] = []
    if plan.anchors:
        groups.append(plan.anchors)
    for facet in FACET_ORDER:
        terms = plan.facets.get(facet, [])
        if terms:
            groups.append(terms)
    if plan.claim_keywords:
        groups.append(plan.claim_keywords)
    return groups


def _relaxed_groups(plan: QueryPlan) -> List[List[str]]:
    """Two conjunctive groups: anchors AND the union of all remaining terms.

    Used when the strict conjunction retrieves too little to score. Reported as
    a relaxation rather than substituted silently, because the two queries do
    not answer the same question.
    """
    remainder: List[str] = []
    for facet in FACET_ORDER:
        remainder.extend(plan.facets.get(facet, []))
    remainder.extend(plan.claim_keywords)
    remainder = _dedupe_preserving_order(remainder)

    groups: List[List[str]] = []
    if plan.anchors:
        groups.append(plan.anchors)
    if remainder:
        groups.append(remainder)
    return groups or [plan.anchors or remainder]


def _render_boolean(groups: List[List[str]], exclusions: List[str]) -> str:
    """Render conjunctive groups into a source-neutral boolean expression.

    This canonical form is what the operator sees, edits and audits. The
    source-specific renderers below translate it; they do not redefine it.
    """
    rendered = [
        "(" + " OR ".join(_quote(term) for term in group) + ")"
        for group in groups if group
    ]
    if not rendered:
        return ""
    expression = " AND ".join(rendered)
    if exclusions:
        expression += " NOT (" + " OR ".join(_quote(term) for term in exclusions) + ")"
    return expression


def _quote(term: str) -> str:
    return f'"{term}"' if " " in term else term


def _build_rationale(plan: QueryPlan, populated: List[str]) -> str:
    lines = [
        f"Preset anchors ({len(plan.anchors)}) constrain retrieval to the "
        f"{get_preset(plan.preset).label} domain."
    ]
    if populated:
        named = ", ".join(FACET_LABELS[facet] for facet in populated)
        lines.append(f"Operator-supplied facets combined conjunctively: {named}.")
    else:
        lines.append(
            f"No facets supplied; {len(plan.claim_keywords)} keywords derived from the "
            "claim text. Supplying facets explicitly will narrow retrieval."
        )
    lines.append(
        f"{len(plan.exclusions)} exclusion terms applied both in the query and as a "
        "post-retrieval filter, so every source is filtered identically."
    )
    lines.append(
        "If the strict query returns fewer than "
        f"{MIN_RECORDS_BEFORE_RELAXATION} records, a relaxed query is executed and "
        "flagged in the result."
    )
    return " ".join(lines)


# --------------------------------------------------------------------------
# Source-specific rendering
# --------------------------------------------------------------------------

def to_pubmed_query(expression: str) -> str:
    """Tag every bare term with [tiab] for title/abstract matching.

    Untagged terms are expanded by PubMed's automatic term mapping across MeSH
    and all fields, which broadens retrieval unpredictably and makes the logged
    query a poor record of what was actually searched.
    """
    if not expression:
        return ""
    parts = re.split(r'("[^"]*"|\(|\)|\bAND\b|\bOR\b|\bNOT\b|\s+)', expression)
    out: List[str] = []
    for part in parts:
        if not part or part.isspace():
            out.append(part or "")
            continue
        if part in {"(", ")", "AND", "OR", "NOT"}:
            out.append(part)
        else:
            out.append(f"{part}[tiab]")
    return _WHITESPACE.sub(" ", "".join(out)).strip()


def to_openalex_query(expression: str) -> str:
    """OpenAlex accepts the canonical expression directly in its search parameter."""
    return expression.strip()


def to_openalex_plain_query(groups_source: QueryPlan) -> str:
    """Degraded fallback used only if OpenAlex rejects the boolean expression.

    Anchors alone, disjunctively. Recall is broader and precision lower; the
    exclusion filter still applies client-side, and the substitution is
    recorded in the source error map.
    """
    terms = groups_source.anchors or groups_source.all_terms
    return " OR ".join(_quote(term) for term in terms[:12])


def describe_plan(plan: QueryPlan) -> Dict[str, object]:
    """Serialisable form matching schemas.RefineResponse."""
    return {
        "refined_query": plan.strict_query,
        "facets": {FACET_LABELS[k]: v for k, v in plan.facets.items() if v},
        "anchors_used": plan.anchors,
        "excluded_terms": plan.exclusions,
        "rationale": plan.rationale,
    }