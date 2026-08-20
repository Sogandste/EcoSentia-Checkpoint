"""Data contracts shared by the service, retrieval, scoring, and client layers.

Every field that crosses a process boundary is declared here. Typed contracts
make incompatible field changes fail explicitly instead of silently producing
incomplete results.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


SUPPORT_LEVELS: tuple[str, ...] = (
    "none",
    "limited",
    "indirect",
    "moderate",
    "direct",
)

MIN_CLAIM_LENGTH = 20
MAX_CLAIM_LENGTH = 2000

VALID_SOURCES: tuple[str, ...] = (
    "pubmed",
    "openalex",
    "both",
)


def normalise_source(value: str) -> str:
    """Return the canonical source key used by the retrieval layer."""

    source = (value or "both").strip().lower()

    aliases = {
        "all": "both",
        "combined": "both",
        "ncbi": "pubmed",
        "pub-med": "pubmed",
        "pub_med": "pubmed",
        "open-alex": "openalex",
        "open_alex": "openalex",
    }

    source = aliases.get(source, source)
    return source if source in VALID_SOURCES else "both"


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

    @field_validator("source")
    @classmethod
    def validate_record_source(cls, value: str) -> str:
        source = (value or "").strip().lower()

        aliases = {
            "ncbi": "pubmed",
            "pub-med": "pubmed",
            "pub_med": "pubmed",
            "open-alex": "openalex",
            "open_alex": "openalex",
        }

        return aliases.get(source, source)

    @field_validator("identifier", "title", "journal", "doi", "url")
    @classmethod
    def strip_record_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("abstract")
    @classmethod
    def normalise_abstract(cls, value: str) -> str:
        return " ".join((value or "").split())


class BiasFinding(BaseModel):
    """One deterministic translation-risk pattern detected in record text."""

    bias: str
    explanation: str
    trigger: str
    severity: str = "moderate"

    @field_validator("bias", "explanation", "trigger", "severity")
    @classmethod
    def strip_bias_text(cls, value: str) -> str:
        return value.strip()


class ScanRequest(BaseModel):
    """One evidence-scan request shared across service endpoints."""

    claim: str = Field(
        min_length=MIN_CLAIM_LENGTH,
        max_length=MAX_CLAIM_LENGTH,
    )
    lens: str = ""
    preset: str = "fog"
    source: str = "both"
    max_results: int = 15
    query_text: Optional[str] = None
    biological_model: str = ""
    target_function: str = ""
    application_context: str = ""
    mechanism_keywords: str = ""
    exclude_terms: str = ""
    session_id: str = ""
    project: str = ""

    @field_validator("claim", mode="before")
    @classmethod
    def normalise_claim(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator(
        "lens",
        "preset",
        "biological_model",
        "target_function",
        "application_context",
        "mechanism_keywords",
        "exclude_terms",
        "session_id",
        "project",
        mode="before",
    )
    @classmethod
    def normalise_optional_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("query_text", mode="before")
    @classmethod
    def normalise_query_text(cls, value: Any) -> Optional[str]:
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @field_validator("max_results")
    @classmethod
    def clamp_max_results(cls, value: int) -> int:
        return max(5, min(50, value))

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return normalise_source(value)


class RefineResponse(BaseModel):
    """Structured query-refinement response."""

    refined_query: str
    facets: Dict[str, List[str]]
    anchors_used: List[str]
    excluded_terms: List[str]
    rationale: str


class LensResult(BaseModel):
    """Outcome of one lens applied to one claim.

    When ``scored`` is false, support fields may remain null because the lens
    requires qualitative or expert judgement rather than lexical scoring.
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
    def support_level_must_be_known(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is not None and value not in SUPPORT_LEVELS:
            raise ValueError(f"unknown support level: {value}")
        return value


class MatrixResponse(BaseModel):
    """Collection of lens results produced for one claim."""

    claim: str
    preset: str
    query_text: str
    lens_matrix: Dict[str, LensResult]
    generated_at: str


class PromptBundle(BaseModel):
    """Evidence-conditioned prompts for downstream manual use."""

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

    @field_validator("support_level")
    @classmethod
    def prompt_support_level_must_be_known(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is not None and value not in SUPPORT_LEVELS:
            raise ValueError(f"unknown support level: {value}")
        return value


class FeedbackRequest(BaseModel):
    """Expert or operator feedback submitted for a completed scan."""

    human_agree: bool
    notes: str = ""
    reviewer: str = ""

    @field_validator("notes", "reviewer", mode="before")
    @classmethod
    def strip_feedback_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class FeedbackAck(BaseModel):
    """Acknowledgement returned after feedback persistence."""

    scan_id: str
    recorded: bool
    message: str


class LensInfo(BaseModel):
    """Public metadata describing one evaluation lens."""

    key: str
    label: str
    scored: bool
    requires_expert_verification: bool
    question: str
    checklist: List[str]
    legacy: bool


class LensCatalog(BaseModel):
    """Public catalogue of lenses, presets, and support levels."""

    lenses: List[LensInfo]
    presets: List[str]
    support_levels: List[str]


class SourceHealth(BaseModel):
    """Connectivity state for one bibliographic source."""

    name: str
    reachable: bool
    detail: str = ""


class HealthResponse(BaseModel):
    """Service health and source-connectivity response."""

    status: str
    version: str
    lens_count: int
    sources: List[SourceHealth]


class AuditSummary(BaseModel):
    """Aggregate audit and reviewer-agreement statistics."""

    total_scans: int
    total_feedback: int
    agreement_rate: Optional[float]
    per_lens: Dict[str, Dict[str, Any]]
    note: str
