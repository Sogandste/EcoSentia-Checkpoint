"""Data contracts shared by the service layer, the scoring layer and the client.

Every field that crosses a process boundary is declared here. Modules must not
exchange bare dictionaries: a typed contract is what makes a field rename fail
loudly at import time instead of silently producing an empty panel.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

SUPPORT_LEVELS: tuple = ("none", "limited", "indirect", "moderate", "direct")


class Record(BaseModel):
    """One deduplicated bibliographic record after normalisation."""

    source: str
    identifier: str
    title: str
    abstract: str = ""
    journal: str = ""
    year: Optional[int] = None
    doi: str = ""
    url: str = ""
    score: float = 0.0
    matched_terms: List[str] = Field(default_factory=list)


class BiasFinding(BaseModel):
    """One deterministic translation-risk pattern detected in the claim text."""

    bias: str
    explanation: str
    trigger: str
    severity: str = "moderate"


class ScanRequest(BaseModel):
    """Single request object used by every /evidence/* endpoint.

    A single request shape keeps the four endpoints interchangeable from the
    client's point of view; unused fields are ignored rather than rejected.
    """

    claim: str
    lens: str = ""
    preset: str = "fog"
    source: str = "combined"
    max_results: int = 15
    query_text: Optional[str] = None
    biological_model: str = ""
    target_function: str = ""
    application_context: str = ""
    mechanism_keywords: str = ""
    exclude_terms: str = ""
    session_id: str = ""
    project: str = ""

    @field_validator("claim")
    @classmethod
    def claim_must_not_be_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("claim must not be empty")
        return value.strip()

    @field_validator("max_results")
    @classmethod
    def clamp_max_results(cls, value: int) -> int:
        return max(5, min(50, value))

    @field_validator("source")
    @classmethod
    def normalise_source(cls, value: str) -> str:
        allowed = {"pubmed", "crossref", "combined"}
        lowered = (value or "combined").strip().lower()
        return lowered if lowered in allowed else "combined"


class RefineResponse(BaseModel):
    refined_query: str
    facets: Dict[str, List[str]]
    anchors_used: List[str]
    excluded_terms: List[str]
    rationale: str


class LensResult(BaseModel):
    """Outcome of one lens applied to one claim.

    `scored` is False for judgement-only lenses. When it is False, both
    `support_level` and `support_score` are None by contract, and any consumer
    that renders a support bar must branch on `scored` before reading them.
    """

    scan_id: str
    lens: str
    lens_label: str
    scored: bool
    requires_expert_verification: bool
    question: str
    checklist: List[str] = Field(default_factory=list)
    support_level: Optional[str] = None
    support_score: Optional[float] = None
    combined_count: int = 0
    direct_hits: int = 0
    query_text: str = ""
    summary: str = ""
    top_records: List[Record] = Field(default_factory=list)
    detected_biases: List[BiasFinding] = Field(default_factory=list)
    sources_used: List[str] = Field(default_factory=list)
    source_errors: Dict[str, str] = Field(default_factory=dict)
    latency_ms: int = 0
    error: Optional[str] = None

    @field_validator("support_level")
    @classmethod
    def support_level_must_be_known(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in SUPPORT_LEVELS:
            raise ValueError(f"unknown support level: {value}")
        return value


class MatrixResponse(BaseModel):
    claim: str
    preset: str
    query_text: str
    lens_matrix: Dict[str, LensResult]
    generated_at: str


class PromptBundle(BaseModel):
    """Evidence-conditioned prompts for downstream manual use.

    The prompts embed the retrieved evidence state so that the operator's
    downstream reasoning is anchored to what was actually found, rather than to
    an unstated impression of the literature.
    """

    scan_id: str
    lens: str
    lens_label: str
    support_level: Optional[str]
    evidence_note: str
    master_prompt: str
    counter_prompt: str
    uncertainty_prompt: str
    redesign_prompt: str
    look_for: List[str] = Field(default_factory=list)
    detected_biases: List[BiasFinding] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    human_agree: bool
    notes: str = ""
    reviewer: str = ""


class FeedbackAck(BaseModel):
    scan_id: str
    recorded: bool
    message: str


class LensInfo(BaseModel):
    key: str
    label: str
    scored: bool
    requires_expert_verification: bool
    question: str
    checklist: List[str]
    legacy: bool


class LensCatalog(BaseModel):
    lenses: List[LensInfo]
    presets: List[str]
    support_levels: List[str]


class SourceHealth(BaseModel):
    name: str
    reachable: bool
    detail: str = ""


class HealthResponse(BaseModel):
    status: str
    version: str
    lens_count: int
    sources: List[SourceHealth]


class AuditSummary(BaseModel):
    total_scans: int
    total_feedback: int
    agreement_rate: Optional[float]
    per_lens: Dict[str, Dict[str, Any]]
    note: str