"""Detection of translation-risk patterns in retrieved abstracts.

Findings are lexical flags, not judgements. Each carries the exact span that
triggered it and the segmentation quality under which it was found, so that a
reviewer can dismiss a false positive quickly and can see when detection ran
degraded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from schemas import Record

# Sentence-boundary detection requires punctuation. OpenAlex abstracts are
# reconstructed from an inverted index and arrive without it, so patterns
# anchored on boundaries would fail silently on that source. Segmentation
# quality is therefore measured per record and reported with every finding.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u0600-\u06FF])")
_PUNCT_DENSITY_FLOOR = 0.004     # terminal marks per character
_WINDOW_TOKENS = 28
_WINDOW_STRIDE = 20
_MAX_FINDINGS_PER_PATTERN = 4

SEVERITY_ORDER: Dict[str, int] = {"high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class BiasPattern:
    """One translation-risk pattern.

    `guidance` states what a reviewer should check. A flag without an action is
    noise that trains operators to ignore the panel.
    """

    key: str
    label: str
    severity: str
    expression: re.Pattern
    guidance: str


@dataclass
class BiasFinding:
    pattern_key: str
    label: str
    severity: str
    guidance: str
    excerpt: str
    record_id: str
    segmentation: str


@dataclass
class BiasReport:
    findings: List[BiasFinding] = field(default_factory=list)
    counts_by_severity: Dict[str, int] = field(default_factory=dict)
    records_examined: int = 0
    degraded_records: int = 0
    summary: str = ""


def _pattern(key: str, label: str, severity: str, regex: str, guidance: str) -> BiasPattern:
    return BiasPattern(key, label, severity, re.compile(regex, re.IGNORECASE), guidance)


PATTERNS: Tuple[BiasPattern, ...] = (
    _pattern(
        "scale_leap", "Unqualified scale extrapolation", "high",
        r"\b(?:scal(?:e|es|ing|able|ability)|industrial(?:ly)?|mass[- ]produc\w*|"
        r"large[- ]scale|commercial(?:ly|isation|ization)?)\b",
        "Confirm the reported scale. Laboratory performance rarely transfers to "
        "production without loss; check whether any study exceeds bench scale.",
    ),
    _pattern(
        "in_vitro_only", "Findings limited to model systems", "high",
        r"\b(?:in\s+vitro|in\s+silico|ex\s+vivo|model\s+system|proof[- ]of[- ]concept|"
        r"prototype|simulat\w+|computational(?:ly)?)\b",
        "Model-system evidence does not establish real-world behaviour. Check "
        "whether validation outside the model exists.",
    ),
    _pattern(
        "ideal_conditions", "Controlled-condition dependency", "medium",
        r"\b(?:under\s+(?:ideal|optimal|controlled|laboratory)\s+conditions|"
        r"optimi[sz]ed\s+conditions|ambient\s+conditions|standard\s+conditions)\b",
        "Identify the operating window. Performance outside it is unreported, "
        "not equivalent.",
    ),
    _pattern(
        "single_organism", "Single-source biological evidence", "medium",
        r"\b(?:a\s+single\s+species|one\s+species|single\s+(?:strain|isolate|specimen)|"
        r"this\s+species\s+alone)\b",
        "One organism is not a general mechanism. Check for replication across taxa.",
    ),
    _pattern(
        "causal_overreach", "Causal language from correlational design", "high",
        r"\b(?:proves?|proven|demonstrates?\s+conclusively|confirms?\s+that|"
        r"establishes?\s+that|clearly\s+shows?)\b",
        "Verify the study design supports a causal claim. Observational designs "
        "cannot, regardless of the verb used.",
    ),
    _pattern(
        "hedged_finding", "Speculative framing", "low",
        r"\b(?:may|might|could|potential(?:ly)?|suggests?|appears?\s+to|"
        r"promising|preliminar\w+)\b",
        "Hedged findings are appropriate in primary literature but weaken any "
        "claim built on them. Weight accordingly.",
    ),
    _pattern(
        "no_lifecycle", "Environmental cost unaddressed", "medium",
        r"\b(?:energy[- ]intensive|high\s+temperature|solvent|catalyst|rare[- ]earth|"
        r"toxic|hazardous|precious\s+metal)\b",
        "A biologically inspired mechanism may still require inputs that negate "
        "its environmental advantage. Check for lifecycle data.",
    ),
    _pattern(
        "durability_gap", "Durability unreported", "medium",
        r"\b(?:degrad\w+|fatigue|wear|corrosion|ageing|aging|cycl\w+\s+stability|"
        r"long[- ]term\s+(?:performance|stability))\b",
        "Confirm the tested duration. A mechanism that fails after short service "
        "does not support a durability claim.",
    ),
)


def _segmentation_quality(text: str) -> str:
    """Whether sentence boundaries in this record are trustworthy.

    Returns 'sentence' when terminal punctuation density is sufficient, and
    'window' otherwise. Reported with each finding so that a reviewer knows the
    excerpt may not correspond to a grammatical sentence.
    """
    if not text:
        return "window"
    terminals = sum(text.count(mark) for mark in ".!?")
    return "sentence" if terminals / max(len(text), 1) >= _PUNCT_DENSITY_FLOOR else "window"


def segment(text: str) -> Tuple[List[str], str]:
    """Split a record into inspectable units, degrading when punctuation is absent.

    Fixed overlapping token windows are used as the fallback. Overlap prevents a
    pattern spanning a window edge from being missed, which would be a silent
    false negative and the most damaging failure mode available here.
    """
    quality = _segmentation_quality(text)
    if quality == "sentence":
        parts = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
        return parts or [text.strip()], quality

    tokens = text.split()
    if not tokens:
        return [], quality
    windows = [
        " ".join(tokens[start:start + _WINDOW_TOKENS])
        for start in range(0, max(len(tokens) - _WINDOW_TOKENS, 0) + 1, _WINDOW_STRIDE)
    ]
    return windows or [text.strip()], quality


def _excerpt(unit: str, match: re.Match, radius: int = 90) -> str:
    start = max(0, match.start() - radius)
    end = min(len(unit), match.end() + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(unit) else ""
    return f"{prefix}{unit[start:end].strip()}{suffix}"


def check_records(records: Sequence[Record]) -> BiasReport:
    """Scan retrieved records for translation-risk patterns.

    Findings are capped per pattern. An uncapped report of the same pattern
    across forty records is unreadable, and an unread panel provides no
    protection at all.
    """
    report = BiasReport(records_examined=len(records))
    per_pattern: Dict[str, int] = {pattern.key: 0 for pattern in PATTERNS}

    for record in records:
        text = f"{record.title}. {record.abstract}".strip()
        units, quality = segment(text)
        if quality == "window":
            report.degraded_records += 1

        record_id = f"{record.source}:{record.identifier}"
        for unit in units:
            for pattern in PATTERNS:
                if per_pattern[pattern.key] >= _MAX_FINDINGS_PER_PATTERN:
                    continue
                match = pattern.expression.search(unit)
                if match:
                    per_pattern[pattern.key] += 1
                    report.findings.append(
                        BiasFinding(
                            pattern_key=pattern.key,
                            label=pattern.label,
                            severity=pattern.severity,
                            guidance=pattern.guidance,
                            excerpt=_excerpt(unit, match),
                            record_id=record_id,
                            segmentation=quality,
                        )
                    )

    report.findings.sort(key=lambda item: -SEVERITY_ORDER.get(item.severity, 0))
    for finding in report.findings:
        report.counts_by_severity[finding.severity] = (
            report.counts_by_severity.get(finding.severity, 0) + 1
        )
    report.summary = _summarise(report)
    return report


def _summarise(report: BiasReport) -> str:
    if not report.records_examined:
        return "No records examined; no translation-risk assessment was performed."

    if not report.findings:
        base = (
            "No translation-risk patterns matched. This indicates the absence of "
            "specific vocabulary, not the absence of risk."
        )
    else:
        counts = ", ".join(
            f"{count} {severity}" for severity, count in sorted(
                report.counts_by_severity.items(),
                key=lambda item: -SEVERITY_ORDER.get(item[0], 0),
            )
        )
        base = (
            f"{len(report.findings)} pattern matches across "
            f"{report.records_examined} records ({counts}). Each is a prompt to "
            "read the source, not a defect in it."
        )

    if report.degraded_records:
        base += (
            f" Sentence segmentation was unavailable for {report.degraded_records} "
            "record(s) lacking punctuation; excerpts from these are token windows "
            "and detection sensitivity is lower."
        )
    return base