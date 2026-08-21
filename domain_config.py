"""
Central domain configuration: presets, lens vocabularies, and checklists.

All vocabulary used by the screening pipeline is declared here rather than
constructed at run time, so that a reader can inspect exactly which terms
produced a given result. A lexical instrument whose word lists are not
published cannot be criticised, and an instrument that cannot be criticised
cannot be trusted.

The lens vocabularies were assembled by the project panel and revised after
expert review in August 2026. They have not been validated against an
independently rater-labelled corpus. Agreement between a lens signal and
expert judgement is therefore unquantified, and lens output is a prompt for
attention, not a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONFIG_VERSION = "domain-config-2026-08"


# ---------------------------------------------------------------------------
# Support levels
# ---------------------------------------------------------------------------
# Labels describe what was found in the retrieved text, not whether the claim
# is true. "Strong" means the literature repeatedly places these terms
# together; a widely repeated error would score the same way.

@dataclass(frozen=True)
class SupportLevel:
    key: str
    label: str
    meaning: str


SUPPORT_LEVELS: tuple[SupportLevel, ...] = (
    SupportLevel(
        "none",
        "No lexical support",
        "No retrieved record places the claim terms together. This is an "
        "absence of evidence in the indexed abstracts searched, not evidence "
        "of absence.",
    ),
    SupportLevel(
        "weak",
        "Weak lexical support",
        "Few records combine the claim terms, or they appear only alongside "
        "hedging language. Consistent with an emerging topic and with an "
        "unsupported claim alike.",
    ),
    SupportLevel(
        "moderate",
        "Moderate lexical support",
        "Several independent records combine the claim terms. Co-occurrence "
        "does not establish that the reported relation holds.",
    ),
    SupportLevel(
        "strong",
        "Strong lexical support",
        "The claim terms co-occur across many records. Frequency reflects how "
        "often the literature discusses the combination, not its validity.",
    ),
)

SUPPORT_BY_KEY = {level.key: level for level in SUPPORT_LEVELS}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
# Anchors define the subject area and are required in every query. Positive
# terms indicate the mechanism or outcome under discussion. Negative terms
# mark contexts that share vocabulary but not subject; they are reported to
# the operator rather than filtered out, because an automatic exclusion the
# operator never sees will eventually discard a relevant record unnoticed.

@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    anchors: tuple[str, ...]
    positive_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]


PRESETS: dict[str, Preset] = {
    "fog": Preset(
        key="fog",
        label="Atmospheric water capture",
        description=(
            "Bio-inspired surfaces and structures for fog, dew, and humidity "
            "harvesting."
        ),
        anchors=(
            "fog harvesting",
            "fog collection",
            "atmospheric water harvesting",
            "dew collection",
            "water condensation",
            "moisture capture",
        ),
        positive_terms=(
            "wettability gradient",
            "hydrophilic",
            "hydrophobic",
            "superhydrophobic",
            "Janus membrane",
            "droplet coalescence",
            "directional transport",
            "capillary",
            "nucleation",
            "collection efficiency",
            "water yield",
            "biomimetic",
            "bioinspired",
            "beetle",
            "cactus",
            "spider silk",
            "Nepenthes",
        ),
        negative_terms=(
            "atmospheric optics",
            "visibility forecasting",
            "aerosol dispersion model",
            "cloud seeding",
            "fog computing",
            "edge computing",
        ),
    ),
    "ev": Preset(
        key="ev",
        label="Extracellular vesicles",
        description=(
            "Extracellular vesicles and exosomes as carriers, biomarkers, and "
            "engineered delivery systems."
        ),
        anchors=(
            "extracellular vesicle",
            "extracellular vesicles",
            "exosome",
            "exosomes",
            "microvesicle",
            "ectosome",
            "small EV",
        ),
        positive_terms=(
            "cargo loading",
            "drug delivery",
            "targeting ligand",
            "surface engineering",
            "biodistribution",
            "cellular uptake",
            "membrane fusion",
            "tetraspanin",
            "CD9",
            "CD63",
            "CD81",
            "miRNA transfer",
            "isolation protocol",
            "differential ultracentrifugation",
            "size exclusion chromatography",
            "MISEV",
            "biomarker",
            "immunomodulation",
        ),
        negative_terms=(
            "electric vehicle",
            "battery pack",
            "charging infrastructure",
            "extravehicular activity",
            "expected value",
        ),
    ),
}

DEFAULT_PRESET = "fog"


# ---------------------------------------------------------------------------
# Lenses
# ---------------------------------------------------------------------------
# A lens is a question asked of the retrieved text. Lexical lenses look for
# vocabulary that would appear if the question had been addressed; they detect
# discussion of an issue, never its resolution. Lenses marked as requiring
# expert verification cannot be settled lexically at all, and their signal is
# presented as an unanswered question rather than a score.

@dataclass(frozen=True)
class Lens:
    key: str
    label: str
    question: str
    rationale: str
    positive: tuple[str, ...]
    negative: tuple[str, ...] = ()
    requires_expert: bool = False
    epistemic: bool = False
    detects: str = ""


LENSES: dict[str, Lens] = {
    "mechanism": Lens(
        key="mechanism",
        label="Mechanism",
        question="Is a physical mechanism named, rather than an analogy?",
        rationale=(
            "Bio-inspired claims often transfer a biological outcome without "
            "the mechanism that produced it. Naming the mechanism is the "
            "minimum condition for the claim to be testable."
        ),
        positive=(
            "mechanism", "driving force", "governing equation", "mass transfer",
            "surface energy", "contact angle hysteresis", "Laplace pressure",
            "diffusion", "receptor mediated", "kinetics", "thermodynamic",
        ),
        negative=("inspired by nature", "nature's design", "mimics the way"),
        detects="Whether a mechanism is discussed. Not whether it is correct.",
    ),
    "scale": Lens(
        key="scale",
        label="Scale transfer",
        question="Is the change of scale from organism to device addressed?",
        rationale=(
            "Effects that dominate at the scale of a biological structure are "
            "frequently negligible at device scale. Claims that omit this are "
            "the most common failure mode in bio-inspired engineering."
        ),
        positive=(
            "scale up", "scaling law", "dimensionless", "Reynolds number",
            "characteristic length", "pilot scale", "device level",
            "areal", "per unit area", "throughput",
        ),
        detects="Whether scale is discussed. Not whether the transfer is valid.",
    ),
    "evidence_quality": Lens(
        key="evidence_quality",
        label="Evidence quality",
        question="What kind of study design supports the claim?",
        rationale=(
            "A claim supported by controlled replicated measurement differs in "
            "kind from one supported by a single demonstration, even when both "
            "appear in peer-reviewed abstracts."
        ),
        positive=(
            "randomised", "randomized", "controlled", "replicate",
            "independent replication", "blinded", "sample size",
            "statistically significant", "confidence interval",
            "systematic review", "meta-analysis", "validation cohort",
        ),
        negative=("proof of concept", "preliminary", "pilot study", "case report"),
        epistemic=True,
        detects="Design vocabulary. Study quality requires reading the paper.",
    ),
    "uncertainty": Lens(
        key="uncertainty",
        label="Reported uncertainty",
        question="Are limitations and uncertainty stated?",
        rationale=(
            "Absence of stated uncertainty is not confidence. A literature "
            "that reports no limitations is more often incompletely reported "
            "than settled."
        ),
        positive=(
            "limitation", "uncertainty", "standard deviation", "error bar",
            "variability", "confounding", "remains unclear", "further work",
            "not fully understood", "caution",
        ),
        epistemic=True,
        detects="Hedging and limitation language, including in unrelated claims.",
    ),
    "durability": Lens(
        key="durability",
        label="Durability under operating conditions",
        question="Does the reported performance persist beyond initial testing?",
        rationale=(
            "Functional surfaces and engineered carriers frequently lose "
            "performance through fouling, degradation, or storage. A result "
            "measured once at time zero constrains nothing about deployment."
        ),
        positive=(
            "long term", "durability", "stability", "cycling", "cycles",
            "degradation", "fouling", "abrasion", "shelf life", "storage",
            "ageing", "aging", "recovery", "reusability", "after months",
        ),
        detects="Whether persistence is examined. Not whether it is adequate.",
    ),
    "sustainability": Lens(
        key="sustainability",
        label="Environmental burden",
        question="Is the environmental cost of the proposed route considered?",
        rationale=(
            "A solution addressing an environmental problem can carry an "
            "unexamined environmental cost in synthesis, solvents, or rare "
            "inputs. Naming the domain as sustainable does not establish it."
        ),
        positive=(
            "life cycle assessment", "life-cycle", "embodied energy",
            "carbon footprint", "solvent", "toxicity", "recyclable",
            "biodegradable", "scarce", "critical raw material",
            "energy consumption", "waste stream",
        ),
        detects="Whether burden is discussed. Not whether it is acceptable.",
    ),
    "ethics": Lens(
        key="ethics",
        label="Ethical and governance considerations",
        question="Are provenance, consent, and misuse potential addressed?",
        rationale=(
            "Work on biological material carries obligations about sourcing, "
            "consent, and dual use that no lexical measure can discharge. The "
            "lens exists to keep the question visible, not to answer it."
        ),
        positive=(
            "informed consent", "ethics approval", "institutional review",
            "biosafety", "dual use", "Nagoya Protocol", "benefit sharing",
            "regulatory", "GMP", "data protection", "animal welfare",
        ),
        requires_expert=True,
        detects="Presence of governance vocabulary only. Never sufficient.",
    ),
    "reproducibility": Lens(
        key="reproducibility",
        label="Independent reproduction",
        question="Has the result been reproduced outside the originating group?",
        rationale=(
            "Repetition within one group and reproduction by an independent "
            "one carry different evidential weight. Retrieval counts records, "
            "so it cannot distinguish them without this being asked."
        ),
        positive=(
            "independently reproduced", "replication study",
            "multi-centre", "multicenter", "inter-laboratory",
            "round robin", "reported protocol", "data availability",
            "open data", "standardised protocol", "MISEV",
        ),
        requires_expert=True,
        epistemic=True,
        detects=(
            "Reproduction vocabulary. Author-group independence cannot be "
            "determined from abstract text."
        ),
    ),
}

DEFAULT_LENSES: tuple[str, ...] = (
    "mechanism", "scale", "evidence_quality", "uncertainty", "durability",
)


# ---------------------------------------------------------------------------
# Expert checklist
# ---------------------------------------------------------------------------
# Items no lexical instrument can settle. They are shown after every scan,
# whatever the result, so that a high score does not read as a clearance. The
# checklist is the boundary of the tool, stated in the interface rather than
# only in the paper.

@dataclass(frozen=True)
class ChecklistItem:
    key: str
    prompt: str
    why: str
    lens: str = ""


EXPERT_CHECKLIST: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        "read_sources",
        "Read the retrieved abstracts and confirm each supports the claim.",
        "Retrieval matches words. A record can contain every claim term while "
        "reporting the opposite finding.",
    ),
    ChecklistItem(
        "independence",
        "Check whether the supporting records share authors or a single dataset.",
        "Counting records treats one group's output as many independent "
        "observations.",
        lens="reproducibility",
    ),
    ChecklistItem(
        "contrary",
        "Search deliberately for records that contradict the claim.",
        "The query was built from the claim, so it favours agreement. "
        "Disconfirming work must be sought on purpose.",
    ),
    ChecklistItem(
        "operating_conditions",
        "Confirm the reported conditions match the intended application.",
        "Performance under laboratory conditions constrains deployment weakly.",
        lens="durability",
    ),
    ChecklistItem(
        "governance",
        "Confirm material provenance, approvals, and misuse potential.",
        "No lexical signal discharges an ethical or regulatory obligation.",
        lens="ethics",
    ),
    ChecklistItem(
        "coverage",
        "Consider literature outside English-language indexed abstracts.",
        "Both indexes are English-dominant and abstract-limited. Absent "
        "evidence may be unindexed rather than nonexistent.",
    ),
)


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def list_presets() -> list[dict]:
    return [
        {"key": p.key, "label": p.label, "description": p.description}
        for p in PRESETS.values()
    ]


def get_preset(key: str) -> Preset:
    preset = PRESETS.get((key or "").strip().lower())
    if preset is None:
        raise KeyError(f"unknown preset: {key!r}")
    return preset


def get_anchors(preset: str) -> tuple[str, ...]:
    return get_preset(preset).anchors


def get_positive_terms(preset: str) -> tuple[str, ...]:
    return get_preset(preset).positive_terms


def get_negative_terms(preset: str) -> tuple[str, ...]:
    return get_preset(preset).negative_terms


def list_lenses() -> list[dict]:
    return [
        {
            "key": lens.key,
            "label": lens.label,
            "question": lens.question,
            "rationale": lens.rationale,
            "detects": lens.detects,
            "requires_expert": lens.requires_expert,
            "epistemic": lens.epistemic,
            "term_count": len(lens.positive),
        }
        for lens in LENSES.values()
    ]


def get_lens(key: str) -> Lens:
    lens = LENSES.get((key or "").strip().lower())
    if lens is None:
        raise KeyError(f"unknown lens: {key!r}")
    return lens


def resolve_lenses(keys: list[str] | None) -> tuple[str, ...]:
    """
    Validate a requested lens selection, falling back to the default set.

    An unknown key raises rather than being dropped: a scan that silently
    ignored a requested lens would report a narrower analysis than the
    operator believes they asked for.
    """
    if not keys:
        return DEFAULT_LENSES
    for key in keys:
        get_lens(key)
    return tuple(dict.fromkeys(k.strip().lower() for k in keys))


def requires_expert_verification(lens_key: str) -> bool:
    """
    Whether a lens result must be marked as awaiting expert judgement.

    Used by the scoring and interface layers to suppress a numeric score for
    lenses that lexical evidence cannot settle. Reporting a number there would
    invite the reader to treat an unanswered question as an answered one.
    """
    return get_lens(lens_key).requires_expert


def get_checklist(lens_keys: tuple[str, ...] | None = None) -> list[dict]:
    """
    Return the checklist, marking items tied to a selected lens.

    Every item is always returned. Filtering the checklist by lens selection
    would let an operator narrow the scan and, without noticing, narrow the
    obligations reported back to them.
    """
    selected = set(lens_keys or ())
    return [
        {
            "key": item.key,
            "prompt": item.prompt,
            "why": item.why,
            "lens": item.lens,
            "lens_selected": bool(item.lens and item.lens in selected),
        }
        for item in EXPERT_CHECKLIST
    ]


def config_fingerprint() -> dict:
    """
    Summarise the active vocabulary for the provenance record.

    Term counts are recorded with every scan so that a result can be tied to
    the vocabulary that produced it. Without this, revising a word list would
    silently make earlier results non-comparable.
    """
    return {
        "config_version": CONFIG_VERSION,
        "presets": {
            key: {
                "anchors": len(p.anchors),
                "positive": len(p.positive_terms),
                "negative": len(p.negative_terms),
            }
            for key, p in PRESETS.items()
        },
        "lenses": {key: len(lens.positive) for key, lens in LENSES.items()},
        "checklist_items": len(EXPERT_CHECKLIST),
    }