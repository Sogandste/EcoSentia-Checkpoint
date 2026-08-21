"""
Retrieval from PubMed and OpenAlex.

The central requirement of this module is that a source failure never appears
as an absence of evidence. Both outcomes produce zero records, but they license
opposite conclusions: an empty result is information about the literature, a
timeout is information about the network. Every source therefore reports an
explicit status, and the caller is expected to refuse to score a scan in which
no source succeeded.

Requests are paced and identified. Both services grant higher throughput to
identified clients, and an unidentified tool that exhausts a shared quota
degrades access for every user of that endpoint.
"""

from __future__ import annotations

import os
import re
import time
import threading
from dataclasses import dataclass, field, asdict
from xml.etree import ElementTree

import requests

from query_builder import QueryPlan

RETRIEVAL_SCHEMA = "ecosentia-retrieval-1"

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX_BASE = "https://api.openalex.org/works"

TIMEOUT_S = float(os.environ.get("ECOSENTIA_TIMEOUT", "20"))
MAX_RECORDS_CAP = int(os.environ.get("ECOSENTIA_MAX_RECORDS", "100"))
CONTACT = os.environ.get("ECOSENTIA_CONTACT", "").strip()
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()

USER_AGENT = f"EcoSentia/1.0 ({CONTACT or 'contact unset'})"

# NCBI permits three requests per second unidentified and ten with a key. The
# limit applies per key across all clients using it, so pacing is enforced here
# rather than relying on the service to reject excess traffic.
_NCBI_MIN_INTERVAL = 0.11 if NCBI_API_KEY else 0.34
_NCBI_LOCK = threading.Lock()
_NCBI_LAST = [0.0]

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Record:
    source: str
    identifier: str
    title: str
    abstract: str
    year: int | None
    doi: str
    url: str
    venue: str
    authors: list[str] = field(default_factory=list)
    seen_in: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Concatenated title and abstract, the only text the lenses examine."""
        return f"{self.title}\n{self.abstract}".strip()

    @property
    def has_abstract(self) -> bool:
        return len(self.abstract.strip()) >= 40

    def to_dict(self) -> dict:
        data = asdict(self)
        data["has_abstract"] = self.has_abstract
        return data


@dataclass
class SourceResult:
    """
    Outcome of querying one source.

    `status` is the field that keeps a failure from being read as a finding:
      ok       the source answered and returned records
      empty    the source answered and the query matched nothing
      error    the source did not answer usefully; the result is unknown
      skipped  the source was not queried
    """
    source: str
    status: str
    query_sent: str
    records: list[Record] = field(default_factory=list)
    total_available: int | None = None
    truncated: bool = False
    error: str = ""
    http_status: int | None = None
    attempts: int = 0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "status": self.status,
            "query_sent": self.query_sent,
            "record_count": len(self.records),
            "total_available": self.total_available,
            "truncated": self.truncated,
            "error": self.error,
            "http_status": self.http_status,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 2),
        }


@dataclass
class RetrievalResult:
    sources: list[SourceResult]
    records: list[Record]
    duplicates_merged: int
    schema: str = RETRIEVAL_SCHEMA

    @property
    def succeeded(self) -> list[SourceResult]:
        return [s for s in self.sources if s.status in ("ok", "empty")]

    @property
    def failed(self) -> list[SourceResult]:
        return [s for s in self.sources if s.status == "error"]

    @property
    def usable(self) -> bool:
        """
        Whether any source answered.

        When this is false the scan must not be scored. Zero records from zero
        working sources says nothing about the claim, and presenting it as a
        low score would be a false negative generated entirely by infrastructure.
        """
        return bool(self.succeeded)

    @property
    def partial(self) -> bool:
        """True when some sources answered and others did not."""
        return bool(self.succeeded) and bool(self.failed)

    @property
    def records_without_abstract(self) -> int:
        return sum(1 for r in self.records if not r.has_abstract)

    def coverage_note(self) -> str:
        """
        A plain statement of what this retrieval can and cannot support.

        Composed here rather than in the interface so that the same wording
        enters the audit record and the operator's screen.
        """
        if not self.usable:
            return (
                "No source answered. This scan produced no evidence about the "
                "claim, in either direction."
            )
        parts: list[str] = []
        if self.partial:
            names = ", ".join(s.source for s in self.failed)
            parts.append(
                f"{names} did not respond. Coverage is incomplete and counts "
                "understate what the indexed literature contains."
            )
        if any(s.truncated for s in self.sources):
            parts.append(
                "Results were truncated at the record limit, so counts are a "
                "floor rather than a total."
            )
        missing = self.records_without_abstract
        if missing:
            parts.append(
                f"{missing} record(s) carry no abstract; lens analysis of those "
                "records rests on the title alone."
            )
        parts.append(
            "Both indexes are English-dominant and cover abstracts, not full "
            "text. Evidence outside that scope was not searched."
        )
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "sources": [s.to_dict() for s in self.sources],
            "record_count": len(self.records),
            "duplicates_merged": self.duplicates_merged,
            "records_without_abstract": self.records_without_abstract,
            "usable": self.usable,
            "partial": self.partial,
            "coverage_note": self.coverage_note(),
        }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _request(
    url: str, params: dict, source: str
) -> tuple[requests.Response | None, str, int]:
    """
    Perform a GET with bounded retries.

    Only transient conditions are retried. A malformed query returns 400, and
    repeating it would waste the caller's quota to obtain the same rejection.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=TIMEOUT_S
            )
        except requests.Timeout:
            last_error = f"request timed out after {TIMEOUT_S:.0f}s"
        except requests.RequestException as exc:
            last_error = f"network error: {exc.__class__.__name__}"
        else:
            if response.status_code == 200:
                return response, "", attempt
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRY_STATUSES:
                return response, last_error, attempt
        if attempt < MAX_ATTEMPTS:
            time.sleep(0.6 * (2 ** (attempt - 1)))
    return None, f"{source}: {last_error}", MAX_ATTEMPTS


def _pace_ncbi() -> None:
    with _NCBI_LOCK:
        gap = time.monotonic() - _NCBI_LAST[0]
        if gap < _NCBI_MIN_INTERVAL:
            time.sleep(_NCBI_MIN_INTERVAL - gap)
        _NCBI_LAST[0] = time.monotonic()


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

def _pubmed_params(extra: dict) -> dict:
    params = {"db": "pubmed", "tool": "EcoSentia"}
    if CONTACT:
        params["email"] = CONTACT
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    params.update(extra)
    return params


def _text_of(node) -> str:
    """
    Flatten an element including inline markup.

    Abstracts contain italics and superscripts as child elements. Reading only
    the element's own text would truncate a sentence at the first italicised
    species name, which is common in this literature.
    """
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip() if node is not None else ""


def _parse_pubmed_article(article) -> Record | None:
    citation = article.find("MedlineCitation")
    if citation is None:
        return None
    pmid = _text_of(citation.find("PMID"))
    if not pmid:
        return None

    art = citation.find("Article")
    title = _text_of(art.find("ArticleTitle")) if art is not None else ""

    # Structured abstracts split into labelled sections. Labels are retained
    # because "Limitations" or "Conclusions" headings themselves carry signal
    # that several lenses look for.
    segments: list[str] = []
    if art is not None:
        for part in art.findall("./Abstract/AbstractText"):
            label = (part.get("Label") or "").strip()
            body = _text_of(part)
            if body:
                segments.append(f"{label}: {body}" if label else body)
    abstract = " ".join(segments)

    year = None
    for path in ("./Journal/JournalIssue/PubDate/Year", "./Journal/JournalIssue/PubDate/MedlineDate"):
        node = art.find(path) if art is not None else None
        if node is not None:
            match = re.search(r"(19|20)\d{2}", _text_of(node))
            if match:
                year = int(match.group(0))
                break

    doi = ""
    for node in article.findall(".//ArticleId"):
        if (node.get("IdType") or "").lower() == "doi":
            doi = _text_of(node).lower()
            break

    authors: list[str] = []
    if art is not None:
        for author in art.findall("./AuthorList/Author")[:6]:
            last = _text_of(author.find("LastName"))
            initials = _text_of(author.find("Initials"))
            if last:
                authors.append(f"{last} {initials}".strip())

    venue = _text_of(art.find("./Journal/Title")) if art is not None else ""

    return Record(
        source="pubmed",
        identifier=f"PMID:{pmid}",
        title=title,
        abstract=abstract,
        year=year,
        doi=doi,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        venue=venue,
        authors=authors,
        seen_in=["pubmed"],
    )


def fetch_pubmed(query: str, limit: int) -> SourceResult:
    started = time.monotonic()
    result = SourceResult(source="pubmed", status="error", query_sent=query)

    _pace_ncbi()
    search, error, attempts = _request(
        f"{PUBMED_BASE}/esearch.fcgi",
        _pubmed_params({
            "term": query, "retmax": str(limit),
            "retmode": "json", "sort": "relevance",
        }),
        "pubmed",
    )
    result.attempts = attempts
    if search is None or error:
        result.error = error or "no response from PubMed"
        result.http_status = search.status_code if search is not None else None
        result.elapsed_s = time.monotonic() - started
        return result
    result.http_status = search.status_code

    try:
        payload = search.json()["esearchresult"]
        ids = list(payload.get("idlist", []))
        result.total_available = int(payload.get("count", len(ids)))
    except (ValueError, KeyError, TypeError) as exc:
        result.error = f"pubmed: unreadable search response ({exc.__class__.__name__})"
        result.elapsed_s = time.monotonic() - started
        return result

    if not ids:
        # A confirmed empty result. Distinct from the error paths above, and
        # the distinction is what allows the caller to say "nothing was found"
        # rather than "nothing is known".
        result.status = "empty"
        result.elapsed_s = time.monotonic() - started
        return result

    _pace_ncbi()
    fetch, error, attempts = _request(
        f"{PUBMED_BASE}/efetch.fcgi",
        _pubmed_params({"id": ",".join(ids), "retmode": "xml"}),
        "pubmed",
    )
    result.attempts += attempts
    if fetch is None or error:
        # Identifiers were found but their content could not be retrieved.
        # Reporting this as success with zero records would understate the
        # literature; reporting it as empty would misstate it.
        result.error = (error or "pubmed: could not fetch records") + \
            f" (search matched {result.total_available} record(s))"
        result.elapsed_s = time.monotonic() - started
        return result

    try:
        root = ElementTree.fromstring(fetch.content)
    except ElementTree.ParseError as exc:
        result.error = f"pubmed: malformed XML ({exc})"
        result.elapsed_s = time.monotonic() - started
        return result

    for article in root.findall(".//PubmedArticle"):
        record = _parse_pubmed_article(article)
        if record is not None:
            result.records.append(record)

    result.status = "ok" if result.records else "empty"
    result.truncated = bool(
        result.total_available and result.total_available > len(result.records)
    )
    result.elapsed_s = time.monotonic() - started
    return result


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted: dict | None) -> str:
    """
    Rebuild abstract text from OpenAlex's inverted index.

    OpenAlex distributes abstracts as token-to-position maps rather than prose.
    Without reconstruction every OpenAlex record would appear to have no
    abstract, and the lenses would silently analyse titles alone.
    """
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for token, indices in inverted.items():
        if isinstance(indices, list):
            positions.extend((int(i), token) for i in indices)
    if not positions:
        return ""
    positions.sort()
    return " ".join(token for _, token in positions)


def fetch_openalex(query: str, limit: int) -> SourceResult:
    started = time.monotonic()
    result = SourceResult(source="openalex", status="error", query_sent=query)

    params = {
        "filter": f"title_and_abstract.search:{query}",
        "per-page": str(min(limit, 200)),
        "select": "id,doi,title,publication_year,abstract_inverted_index,"
                  "authorships,primary_location",
    }
    if CONTACT:
        # The polite pool provides more consistent latency and lets the service
        # contact the operator instead of blocking the traffic outright.
        params["mailto"] = CONTACT

    response, error, attempts = _request(OPENALEX_BASE, params, "openalex")
    result.attempts = attempts
    if response is None or error:
        result.error = error or "no response from OpenAlex"
        result.http_status = response.status_code if response is not None else None
        result.elapsed_s = time.monotonic() - started
        return result
    result.http_status = response.status_code

    try:
        payload = response.json()
        works = payload.get("results", [])
        result.total_available = int(payload.get("meta", {}).get("count", len(works)))
    except (ValueError, TypeError, AttributeError) as exc:
        result.error = f"openalex: unreadable response ({exc.__class__.__name__})"
        result.elapsed_s = time.monotonic() - started
        return result

    for work in works:
        identifier = str(work.get("id", "")).rsplit("/", 1)[-1]
        if not identifier:
            continue
        doi = (work.get("doi") or "").replace("https://doi.org/", "").lower()
        location = work.get("primary_location") or {}
        venue = ((location.get("source") or {}).get("display_name")) or ""
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in (work.get("authorships") or [])[:6]
        ]
        result.records.append(Record(
            source="openalex",
            identifier=f"OA:{identifier}",
            title=work.get("title") or "",
            abstract=_reconstruct_abstract(work.get("abstract_inverted_index")),
            year=work.get("publication_year"),
            doi=doi,
            url=str(work.get("id", "")),
            venue=venue,
            authors=[a for a in authors if a],
            seen_in=["openalex"],
        ))

    result.status = "ok" if result.records else "empty"
    result.truncated = bool(
        result.total_available and result.total_available > len(result.records)
    )
    result.elapsed_s = time.monotonic() - started
    return result


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _merge(results: list[SourceResult]) -> tuple[list[Record], int]:
    """
    Combine sources, collapsing records that share a DOI.

    Deduplication is by DOI only. Title matching would merge distinct records
    that share a title, such as a preprint and a corrected version, and no
    downstream stage could recover the loss. Records without a DOI are kept
    separately even where they may be duplicates, since an over-count is
    visible to the operator and a silent deletion is not.

    Each surviving record lists the sources it was seen in, so that scoring can
    treat presence in two indexes as one record rather than two.
    """
    by_doi: dict[str, Record] = {}
    merged: list[Record] = []
    duplicates = 0

    for source in results:
        for record in source.records:
            key = record.doi.strip()
            if key and key in by_doi:
                existing = by_doi[key]
                if record.source not in existing.seen_in:
                    existing.seen_in.append(record.source)
                # Prefer the fuller abstract: PubMed carries structured text
                # while OpenAlex reconstruction can be partial, and either may
                # be the more complete of the two for a given record.
                if len(record.abstract) > len(existing.abstract):
                    existing.abstract = record.abstract
                duplicates += 1
                continue
            if key:
                by_doi[key] = record
            merged.append(record)

    return merged, duplicates


def retrieve(plan: QueryPlan) -> RetrievalResult:
    """
    Execute an approved plan against both sources.

    Sources are queried independently and a failure in one does not abort the
    other. Partial coverage is reported rather than concealed, because a scan
    that quietly dropped a source would report a narrower literature as though
    it were the whole of it.
    """
    limit = max(1, min(plan.max_records, MAX_RECORDS_CAP))
    results = [
        fetch_pubmed(plan.queries["pubmed"], limit),
        fetch_openalex(plan.queries["openalex"], limit),
    ]
    records, duplicates = _merge(results)
    return RetrievalResult(
        sources=results, records=records, duplicates_merged=duplicates
    )