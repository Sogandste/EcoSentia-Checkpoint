"""Central domain configuration: lens taxonomy, term sets, scoring constants.

This module is the single source of truth for what the system knows about its
domain. Every other module reads from here and hardcodes nothing. Adding a lens
requires editing this file only; the API, the scorer and the interface pick it
up without modification.

Epistemic layer added after the expert-panel review of August 2026: the
`scored` and `requires_expert_verification` flags plus the per-lens checklist.
These are what allow the system to refuse to score what should not be scored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CONFIG_VERSION = "2026.08.1"

# --------------------------------------------------------------------------
# Support levels
# --------------------------------------------------------------------------
# Ordered from weakest to strongest. Kept identical to schemas.SUPPORT_LEVELS;
# validate_config() enforces that the two never drift apart.

SUPPORT_LEVELS: Tuple[str, ...] = ("none", "limited", "indirect", "moderate", "direct")

SUPPORT_LEVEL_DESCRIPTIONS: Dict[str, str] = {
    "none": "No retrieved record contains vocabulary consistent with the claim under this lens.",
    "limited": "Isolated records touch the claim, but none addresses it directly.",
    "indirect": "Related work exists in adjacent systems; transfer to this claim is untested.",
    "moderate": "Several records address the claim directly, with partial agreement.",
    "direct": "Multiple records address the claim directly and consistently.",
}

# --------------------------------------------------------------------------
# Scoring constants
# --------------------------------------------------------------------------
# Fixed, declared, not learned. Determinism is a requirement: the same claim and
# the same retrieved set must yield the same support level on every run,
# independent of call order or process lifetime.

WEIGHT_ANCHOR = 0.40      # preset anchor coverage
WEIGHT_POSITIVE = 0.45    # lens-positive term density
WEIGHT_NEGATIVE = 0.25    # lens-negative term density, subtracted

DIRECT_HIT_THRESHOLD = 0.50   # record score at or above which a record counts as a direct hit
TOP_K_RECORDS = 5             # records averaged into the aggregate score
HIT_COMPONENT_WEIGHT = 0.60   # weight of the direct-hit component in the aggregate
MEAN_COMPONENT_WEIGHT = 0.40  # weight of the top-k mean component in the aggregate

ANCHOR_COVERAGE_CAP = 4       # anchor matches beyond this add nothing
POSITIVE_SATURATION = 5       # lens-positive matches beyond this add nothing
NEGATIVE_SATURATION = 4       # lens-negative matches beyond this subtract nothing further


@dataclass(frozen=True)
class LensSpec:
    """Declarative specification of one analytical lens.

    Frozen because configuration must not be mutated at runtime: a lens whose
    thresholds change mid-session would make the audit log uninterpretable.
    """

    key: str
    label: str
    question: str
    checklist: Tuple[str, ...]
    scored: bool = True
    requires_expert_verification: bool = False
    positive_terms: Tuple[str, ...] = ()
    negative_terms: Tuple[str, ...] = ()
    saturation_target: int = 6
    thresholds: Tuple[float, float, float, float] = (0.15, 0.32, 0.50, 0.70)
    metric_label: str = "Evidence Support Level"
    reading_note: str = ""


# --------------------------------------------------------------------------
# LEGACY LENSES
# --------------------------------------------------------------------------
# The five lenses that predate the August 2026 epistemic layer. Their keys,
# labels, questions and term sets are preserved unchanged so that scans logged
# before the revision remain comparable with scans logged after it.
#
# EDIT HERE if the original deployment used different keys. Nothing outside
# this block and the LEGACY_LENSES tuple below needs to change.

LEGACY_LENSES: Tuple[str, ...] = (
    "mechanism",
    "materials",
    "ecology",
    "scalability",
    "application",
)

LENSES: Dict[str, LensSpec] = {
    "mechanism": LensSpec(
        key="mechanism",
        label="Mechanism",
        question=(
            "Is the physical or biological mechanism behind the proposed function "
            "actually identified in the literature, rather than assumed?"
        ),
        checklist=(
            "Is the governing mechanism named, or only the outcome described?",
            "Has the mechanism been measured directly, or inferred from correlation?",
            "Do independent groups report the same mechanism?",
            "Are the boundary conditions under which the mechanism holds stated?",
        ),
        positive_terms=(
            "mechanism", "mechanistic", "structure-function", "governing",
            "underlying principle", "physical basis", "driving force",
            "capillary", "wetting", "adhesion", "surface energy", "gradient",
            "transport", "kinetics", "thermodynamic", "mass transfer",
            "nucleation", "membrane trafficking", "biogenesis", "signalling pathway",
        ),
        negative_terms=(
            "mechanism remains unknown", "poorly understood", "unclear mechanism",
            "black box", "phenomenological", "empirical correlation",
            "not yet elucidated", "remains to be determined",
        ),
        saturation_target=8,
        thresholds=(0.15, 0.32, 0.50, 0.70),
    ),
    "materials": LensSpec(
        key="materials",
        label="Materials & Fabrication",
        question=(
            "Do materials and fabrication routes exist that can realise the proposed "
            "structure at the required tolerance?"
        ),
        checklist=(
            "Is the fabrication route specified, or only the target geometry?",
            "Are the reported feature sizes achievable outside a cleanroom?",
            "Is material cost or availability addressed anywhere?",
            "Has the structure been fabricated by anyone other than the originating group?",
        ),
        positive_terms=(
            "fabrication", "synthesis", "coating", "surface modification",
            "nanostructure", "microstructure", "lithography", "electrospinning",
            "self-assembly", "template", "substrate", "composite", "polymer",
            "deposition", "etching", "patterning", "functionalization",
        ),
        negative_terms=(
            "difficult to fabricate", "not reproducible", "low yield",
            "specialised equipment", "single crystal only", "cleanroom required",
        ),
        saturation_target=8,
        thresholds=(0.15, 0.32, 0.50, 0.70),
    ),
    "ecology": LensSpec(
        key="ecology",
        label="Ecological Context",
        question=(
            "Is the biological model characterised in its own environmental context, "
            "or only under laboratory conditions?"
        ),
        checklist=(
            "Were the organism's traits measured in situ or only in captivity?",
            "Is the natural operating envelope (humidity, temperature, light) reported?",
            "Is the trait known to vary between populations or seasons?",
            "Does the source organism face the same constraints as the target application?",
        ),
        positive_terms=(
            "field conditions", "natural habitat", "in situ", "ecological",
            "environmental variability", "microclimate", "seasonal", "arid",
            "desert", "native range", "wild population", "field measurement",
            "natural environment", "ambient conditions",
        ),
        negative_terms=(
            "laboratory conditions only", "controlled chamber", "captive",
            "simulated environment", "idealised conditions",
        ),
        saturation_target=5,
        thresholds=(0.12, 0.26, 0.42, 0.62),
    ),
    "scalability": LensSpec(
        key="scalability",
        label="Scalability",
        question=(
            "Is there evidence that the proposed structure or process survives the "
            "move from laboratory area to deployment area?"
        ),
        checklist=(
            "What is the largest area or volume actually demonstrated?",
            "Does performance per unit area hold as area increases?",
            "Is a continuous or batch manufacturing route identified?",
            "Is cost per unit area reported at any scale?",
        ),
        positive_terms=(
            "scale-up", "scalable", "large-area", "roll-to-roll", "continuous process",
            "pilot scale", "industrial", "throughput", "mass production",
            "square metre", "cost per", "manufacturable", "batch production",
        ),
        negative_terms=(
            "laboratory scale", "proof of concept", "small area", "not scalable",
            "limited to", "bench scale", "millimetre scale",
        ),
        saturation_target=4,
        thresholds=(0.12, 0.26, 0.42, 0.62),
    ),
    "application": LensSpec(
        key="application",
        label="Application Readiness",
        question=(
            "Has the concept been tested in, or against, a realistic operational setting?"
        ),
        checklist=(
            "Is there a field trial, or only laboratory performance?",
            "Is performance reported against an existing conventional baseline?",
            "Are failure modes under operational conditions described?",
            "Is the reported duration of testing stated?",
        ),
        positive_terms=(
            "field trial", "prototype", "deployment", "operational", "pilot study",
            "real-world", "installed", "demonstrator", "end user", "case study",
            "benchmark", "compared with conventional", "practical application",
        ),
        negative_terms=(
            "future work", "has yet to be tested", "no field data",
            "remains theoretical", "simulation only", "in principle",
        ),
        saturation_target=6,
        thresholds=(0.14, 0.30, 0.46, 0.66),
    ),

    # ----------------------------------------------------------------------
    # LENSES ADDED IN THE AUGUST 2026 EPISTEMIC REVISION
    # ----------------------------------------------------------------------

    "evidence_quality": LensSpec(
        key="evidence_quality",
        label="Evidence Quality",
        question=(
            "What is the methodological standard of the studies that support this "
            "claim, independent of whether they agree with it?"
        ),
        checklist=(
            "Are controls, replicates and sample sizes reported?",
            "Is the work pre-registered, or is the analysis exploratory?",
            "Has any result been reproduced by an independent laboratory?",
            "Does the field have a reporting guideline, and is it followed?",
            "Are negative or null results present in the retrieved set at all?",
        ),
        requires_expert_verification=True,
        positive_terms=(
            "randomized", "randomised", "controlled trial", "replicate", "replication",
            "blinded", "pre-registered", "systematic review", "meta-analysis",
            "sample size", "statistical power", "reproducibility", "reproducible",
            "independent validation", "inter-laboratory", "reporting guideline",
            "standard operating procedure", "orthogonal method", "misev",
            "confidence interval", "effect size",
        ),
        negative_terms=(
            "anecdotal", "single case", "no control", "preliminary",
            "underpowered", "not replicated", "pilot data", "unvalidated",
            "convenience sample", "post hoc",
        ),
        saturation_target=6,
        thresholds=(0.14, 0.30, 0.48, 0.68),
        reading_note=(
            "A high level here means the retrieved literature is methodologically "
            "strong, not that it supports the claim. Read it together with the "
            "lens that matches your actual question."
        ),
    ),
    "uncertainty": LensSpec(
        key="uncertainty",
        label="Uncertainty",
        question=(
            "How much unresolved disagreement, hedging or acknowledged ignorance "
            "surrounds this claim in the literature?"
        ),
        checklist=(
            "Do retrieved papers contradict one another on the central point?",
            "Is disagreement acknowledged in the texts, or only visible across them?",
            "Are the sources of variability named?",
            "Would an expert in the field describe this as settled?",
        ),
        requires_expert_verification=True,
        positive_terms=(
            "uncertain", "uncertainty", "unclear", "controversial", "conflicting",
            "contradictory", "debated", "disputed", "inconsistent", "heterogeneity",
            "confounding", "cannot exclude", "remains unknown", "poorly constrained",
            "further research is needed", "speculative", "hypothesised",
            "limitation", "caution should be exercised", "no consensus",
        ),
        negative_terms=(
            "well established", "consensus", "confirmed", "robust",
            "consistently observed", "independently reproduced", "validated",
            "unambiguous",
        ),
        saturation_target=5,
        thresholds=(0.12, 0.26, 0.44, 0.64),
        metric_label="Uncertainty Signal",
        reading_note=(
            "This scale is inverted relative to the other lenses. A high value "
            "means the literature signals substantial unresolved uncertainty. "
            "It is a caution, never an endorsement."
        ),
    ),
    "sustainability": LensSpec(
        key="sustainability",
        label="Environmental Sustainability",
        question=(
            "What is the environmental cost of producing, operating and disposing "
            "of the proposed system?"
        ),
        checklist=(
            "Has any life cycle assessment been performed?",
            "Are persistent or hazardous substances required by the fabrication route?",
            "Is end-of-life recovery or degradation addressed?",
            "Does the environmental benefit claimed exceed the production burden?",
            "Are critical or geographically constrained raw materials involved?",
        ),
        requires_expert_verification=True,
        positive_terms=(
            "life cycle assessment", "lca", "recyclable", "recycling",
            "biodegradable", "biodegradation", "carbon footprint", "embodied energy",
            "circular economy", "renewable feedstock", "end-of-life",
            "water footprint", "green chemistry", "solvent-free", "non-toxic",
            "environmental impact",
        ),
        negative_terms=(
            "fluorinated", "pfas", "perfluor", "rare earth", "critical raw material",
            "solvent intensive", "hazardous", "non-recyclable", "persistent pollutant",
            "heavy metal", "toxic",
        ),
        saturation_target=4,
        thresholds=(0.10, 0.22, 0.38, 0.58),
        reading_note=(
            "The base rate of life cycle vocabulary in this literature is low, so "
            "thresholds here are deliberately lower than for the technical lenses. "
            "A low level means the question has not been studied, not that the "
            "environmental cost is small."
        ),
    ),
    "stability": LensSpec(
        key="stability",
        label="Stability & Durability",
        question=(
            "Does the reported performance persist over operational time, cycles "
            "and storage, or is it measured only when freshly prepared?"
        ),
        checklist=(
            "Over what duration or how many cycles was performance measured?",
            "Is the degradation mode identified, or only the loss quantified?",
            "Were samples aged, weathered, or stored before testing?",
            "Is a criterion for end of useful life defined?",
        ),
        positive_terms=(
            "long-term", "durability", "durable", "cyclic", "cycling", "fatigue",
            "aging", "ageing", "weathering", "uv stability", "thermal stability",
            "retained performance", "after cycles", "shelf life", "storage stability",
            "freeze-thaw", "lyophilization", "lyophilisation", "robustness over time",
            "accelerated aging", "endurance",
        ),
        negative_terms=(
            "degradation observed", "performance loss", "delamination", "fouling",
            "loss of function", "short-lived", "unstable", "rapid decline",
            "freshly prepared only",
        ),
        saturation_target=5,
        thresholds=(0.13, 0.28, 0.45, 0.65),
    ),
    "ethics": LensSpec(
        key="ethics",
        label="Ethical Considerations",
        question=(
            "Whose interests are affected by this design, and who was not consulted?"
        ),
        checklist=(
            "Does the source organism or its habitat bear a collection cost?",
            "Is traditional or local knowledge involved, and is it attributed?",
            "Who gains access to the resulting technology, and who is excluded?",
            "Does deployment redistribute a shared resource such as water?",
            "Are dual-use or misuse pathways plausible?",
            "Which stakeholders should review this before it proceeds?",
        ),
        scored=False,
        requires_expert_verification=True,
        saturation_target=0,
        reading_note=(
            "This lens is deliberately not scored. No retrieval is performed and "
            "no support level is emitted. Presenting a keyword statistic here "
            "would dress a value judgement as a measured property of the "
            "literature. The checklist is the output."
        ),
    ),
}

# Derived sets. Computed once from LENSES so that a new lens cannot be
# accidentally omitted from one of them.
NON_SCORED_LENSES: frozenset = frozenset(k for k, v in LENSES.items() if not v.scored)
EXPERT_VERIFICATION_LENSES: frozenset = frozenset(
    k for k, v in LENSES.items() if v.requires_expert_verification
)
INVERTED_SEMANTIC_LENSES: Dict[str, str] = {
    k: v.metric_label for k, v in LENSES.items()
    if v.metric_label != "Evidence Support Level"
}
DEFAULT_LENS = "mechanism"


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------
# Anchors carry the domain specificity that the lens term sets deliberately
# omit, so that one lens taxonomy serves multiple research domains.
#
# Exclusions are not optional. Both preset keywords collide with high-volume
# unrelated literature: "fog" with distributed computing, "EV" with extracellular vesicles. Without the negative clause the retrieved set is contaminated, and
# that contamination is reported as apparent support.

@dataclass(frozen=True)
class PresetSpec:
    key: str
    label: str
    description: str
    anchors: Tuple[str, ...]
    exclusions: Tuple[str, ...]


PRESETS: Dict[str, PresetSpec] = {
    "fog": PresetSpec(
        key="fog",
        label="Fog & Atmospheric Water Harvesting",
        description=(
            "Surfaces and structures that capture liquid water from fog, dew or "
            "humid air, including biological models such as desert beetles, "
            "cactus spines and Namib grasses."
        ),
        anchors=(
            "fog harvesting", "fog collection", "water harvesting",
            "atmospheric water", "dew collection", "condensation",
            "droplet nucleation", "wettability", "hydrophobic", "hydrophilic",
            "superhydrophobic", "biomimetic surface", "water capture",
        ),
        exclusions=(
            "fog computing", "edge computing", "fog node", "fog lamp",
            "brain fog", "fog signal",
        ),
    ),
    "ev": PresetSpec(
        key="ev",
        label="Extracellular Vesicles",
        description=(
            "Extracellular vesicles, including exosomes and microvesicles, their "
            "isolation, characterisation, cargo and functional attribution."
        ),
        anchors=(
            "extracellular vesicle", "extracellular vesicles", "exosome",
            "microvesicle", "ectosome", "small ev", "vesicle isolation",
            "vesicle cargo", "vesicular transport", "misev",
            "nanoparticle tracking analysis", "tetraspanin",
        ),
        exclusions=(
            "electric vehicle", "electric vehicles", "expected value",
            "ebola virus", "vehicle-to-grid",
        ),
    ),
}

DEFAULT_PRESET = "fog"


# --------------------------------------------------------------------------
# Accessors
# --------------------------------------------------------------------------
# All lookups go through these functions. Direct dictionary access from other
# modules is what allows an unknown key to propagate silently; every accessor
# here either normalises the key or fails with a named error.

def normalise_lens_key(lens: str) -> str:
    return (lens or "").strip().lower().replace(" ", "_").replace("-", "_")


def normalise_preset_key(preset: str) -> str:
    return (preset or "").strip().lower()


def get_lens(lens: str) -> LensSpec:
    """Return the spec for a lens, raising on an unknown key.

    Raises rather than falling back to a default: a silent fallback would score
    a claim under a lens the operator did not choose, and the audit log would
    record the wrong lens name against the result.
    """
    key = normalise_lens_key(lens)
    if key not in LENSES:
        raise KeyError(f"unknown lens: {lens!r}. Known lenses: {sorted(LENSES)}")
    return LENSES[key]


def get_preset(preset: str) -> PresetSpec:
    key = normalise_preset_key(preset)
    if key not in PRESETS:
        raise KeyError(f"unknown preset: {preset!r}. Known presets: {sorted(PRESETS)}")
    return PRESETS[key]


def all_lens_keys(scored_only: bool = False) -> List[str]:
    """Lens keys in declaration order, legacy lenses first."""
    keys = list(LENSES)
    return [k for k in keys if LENSES[k].scored] if scored_only else keys


def all_preset_keys() -> List[str]:
    return list(PRESETS)


def get_anchors(preset: str) -> List[str]:
    return list(get_preset(preset).anchors)


def get_exclusions(preset: str) -> List[str]:
    return list(get_preset(preset).exclusions)


def get_positive_terms(lens: str) -> List[str]:
    return list(get_lens(lens).positive_terms)


def get_negative_terms(lens: str) -> List[str]:
    return list(get_lens(lens).negative_terms)


def get_thresholds(lens: str) -> Tuple[float, float, float, float]:
    return get_lens(lens).thresholds


def get_saturation_target(lens: str) -> int:
    return get_lens(lens).saturation_target


def is_lens_scored(lens: str) -> bool:
    """Whether retrieval and scoring should run for this lens at all."""
    return get_lens(lens).scored


def requires_expert_verification(lens: str) -> bool:
    """Whether output from this lens must be reviewed before use.

    Implemented as a single attribute read on the spec. An earlier revision
    used a parallel if/elif chain, which drifted out of step with the lens
    dictionary the first time a lens was added and returned False for a lens
    that did require verification.
    """
    return get_lens(lens).requires_expert_verification


def get_metric_label(lens: str) -> str:
    """Correct noun for this lens's score bar.

    Overridden for lenses whose scale is inverted, so that a high uncertainty
    value is never rendered under the word "Support".
    """
    return get_lens(lens).metric_label


def get_reading_note(lens: str) -> str:
    return get_lens(lens).reading_note


def get_question(lens: str) -> str:
    return get_lens(lens).question


def get_checklist(lens: str) -> List[str]:
    return list(get_lens(lens).checklist)


def is_legacy_lens(lens: str) -> bool:
    return normalise_lens_key(lens) in LEGACY_LENSES


def score_to_support_level(score: float, lens: str) -> str:
    """Map an aggregate score in [0, 1] to an ordinal support level."""
    t0, t1, t2, t3 = get_thresholds(lens)
    if score < t0:
        return "none"
    if score < t1:
        return "limited"
    if score < t2:
        return "indirect"
    if score < t3:
        return "moderate"
    return "direct"


def lens_catalog() -> List[Dict[str, object]]:
    """Serialisable lens list for the /lenses endpoint.

    The interface builds its lens selector from this, so a lens added here
    appears in the interface with no client-side change and no risk of an
    out-of-date hardcoded list dropping an existing lens.
    """
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "scored": spec.scored,
            "requires_expert_verification": spec.requires_expert_verification,
            "question": spec.question,
            "checklist": list(spec.checklist),
            "legacy": spec.key in LEGACY_LENSES,
        }
        for spec in LENSES.values()
    ]


# --------------------------------------------------------------------------
# Configuration validation
# --------------------------------------------------------------------------

def validate_config() -> None:
    """Fail at import time on an internally inconsistent configuration.

    A misordered threshold tuple or a scored lens with no positive terms
    produces plausible-looking output rather than an error, which is the worst
    possible failure mode for an evidence tool. These checks run once, on
    import, so such a configuration can never reach a user.
    """
    for key, spec in LENSES.items():
        if key != spec.key:
            raise ValueError(f"lens dictionary key {key!r} does not match spec key {spec.key!r}")

        if not spec.question.strip():
            raise ValueError(f"lens {key!r} has no guiding question")

        if not spec.checklist:
            raise ValueError(f"lens {key!r} has no checklist")

        if spec.scored:
            if not spec.positive_terms:
                raise ValueError(f"scored lens {key!r} has no positive terms")
            if spec.saturation_target < 1:
                raise ValueError(f"scored lens {key!r} has a saturation target below 1")
            if list(spec.thresholds) != sorted(spec.thresholds):
                raise ValueError(f"lens {key!r} has non-ascending thresholds")
            if not all(0.0 < t < 1.0 for t in spec.thresholds):
                raise ValueError(f"lens {key!r} has thresholds outside (0, 1)")

        overlap = set(spec.positive_terms) & set(spec.negative_terms)
        if overlap:
            raise ValueError(f"lens {key!r} has terms in both polarities: {sorted(overlap)}")

    for legacy_key in LEGACY_LENSES:
        if legacy_key not in LENSES:
            raise ValueError(
                f"legacy lens {legacy_key!r} is declared but missing from LENSES. "
                "Legacy lenses must never be removed; scans logged before the "
                "August 2026 revision reference them."
            )

    for key, spec in PRESETS.items():
        if key != spec.key:
            raise ValueError(f"preset dictionary key {key!r} does not match spec key {spec.key!r}")
        if not spec.anchors:
            raise ValueError(f"preset {key!r} has no anchor terms")
        if not spec.exclusions:
            raise ValueError(
                f"preset {key!r} has no exclusion terms. Both supported presets use "
                "an abbreviation that collides with unrelated literature; omitting "
                "the negative clause contaminates retrieval."
            )

    if DEFAULT_LENS not in LENSES:
        raise ValueError(f"DEFAULT_LENS {DEFAULT_LENS!r} is not a declared lens")
    if DEFAULT_PRESET not in PRESETS:
        raise ValueError(f"DEFAULT_PRESET {DEFAULT_PRESET!r} is not a declared preset")

    weight_sum = WEIGHT_ANCHOR + WEIGHT_POSITIVE
    if not 0.0 < weight_sum <= 1.0:
        raise ValueError(f"positive weights must sum into (0, 1]; got {weight_sum}")
    if abs(HIT_COMPONENT_WEIGHT + MEAN_COMPONENT_WEIGHT - 1.0) > 1e-9:
        raise ValueError("aggregate component weights must sum to 1.0")


validate_config()