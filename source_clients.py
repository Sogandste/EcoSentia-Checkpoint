"""Retrieval from PubMed and OpenAlex, with normalisation and deduplication.

Both indices are optional at run time. A source that fails is reported by name
in the result rather than reducing the record count silently, because a support
level computed from half the intended corpus is not comparable with one
computed from all of it.
"""

from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from query_builder import (
    MIN_RECORDS_BEFORE_RELAXATION,
    QueryPlan,
    to_openalex_plain_query,
    to_openalex_query,
    to_pubmed_query,
)
from schemas import Record

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX_BASE = "https://api.openalex.org"

NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "").strip()
USER_AGENT = f"EcoSentia/2026.08 ({CONTACT_EMAIL or 'contact-not-configured'})"

REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
BACKOFF_BASE = 1.5
RESULT_CEILING = 50

# NCBI permits three requests per second without a key and ten with one.
# OpenAlex asks for a contact address in exchange for a dedicated request pool.
RATE_LIMITS: Dict[str, float] = {
    "eutils.ncbi.nlm.nih.gov": 0.11 if NCBI_API_KEY else 0.35,
    "api.openalex.org": 0.11,
}

_ABSTRACT_MIN_LENGTH = 40
_TITLE_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


@dataclass
class SourceResult:
    """Outcome of one retrieval round across all requested sources."""

    records: List[Record] = field(default_factory=list)
    sources_used: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    query_used: str = ""
    relaxed: bool = False
    raw_counts: Dict[str, int] = field(default_factory=dict)


class _RateLimiter:
    """Per-host minimum interval between requests.

    Enforced in-process with a lock. Politeness is a hard requirement here:
    exceeding the published rate on either service results in an IP block, which
    would present to the operator as an unexplained empty result set.
    """

    def __init__(self) -> None:
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        interval = RATE_LIMITS.get(host, 0.2)
        with self._lock:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < interval:
                time.sleep(interval - elapsed)
            self._last[host] = time.monotonic()


_limiter = _RateLimiter()


def _request(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
) -> requests.Response:
    """Issue one rate-limited request with bounded retries.

    Only transport failures and server-side or throttling status codes are
    retried. A 400 is returned to the caller unretried, since a malformed query
    will not become valid on repetition and the caller needs to see it in order
    to fall back.
    """
    host = url.split("/")[2]
    last_error: Optional[Exception] = None

    for attempt in range(MAX_RETRIES):
        _limiter.wait(host)
        try:
            response = requests.request(
                method,
                url,
                params=params,
                data=data,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except requests.RequestException as exc:
            last_error = exc
        else:
            if response.status_code < 400 or response.status_code == 400:
                return response
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = requests.HTTPError(f"HTTP {response.status_code}")
            else:
                response.raise_for_status()
                return response

        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_BASE ** attempt)

    raise requests.RequestException(f"{host} unreachable after {MAX_RETRIES} attempts: {last_error}")


# --------------------------------------------------------------------------
# PubMed
# --------------------------------------------------------------------------

def _pubmed_params(extra: dict) -> dict:
    params = {"db": "pubmed", "tool": "EcoSentia"}
    if CONTACT_EMAIL:
        params["email"] = CONTACT_EMAIL
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    params.update(extra)
    return params


def search_pubmed(query: str, max_results: int) -> List[Record]:
    """Two-step E-utilities retrieval: esearch for identifiers, efetch for metadata.

    efetch is issued as a POST because an identifier list at the upper result
    ceiling exceeds the practical length of a GET URL.
    """
    limit = max(1, min(RESULT_CEILING, max_results))
    tagged = to_pubmed_query(query)
    if not tagged:
        return []

    search = _request(
        "GET",
        f"{PUBMED_BASE}/esearch.fcgi",
        params=_pubmed_params({"term": tagged, "retmax": limit, "retmode": "json", "sort": "relevance"}),
    )
    if search.status_code == 400:
        raise requests.HTTPError("PubMed rejected the query expression")

    id_list = search.json().get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return []

    fetch = _request(
        "POST",
        f"{PUBMED_BASE}/efetch.fcgi",
        data=_pubmed_params({"id": ",".join(id_list), "retmode": "xml"}),
    )
    return _parse_pubmed_xml(fetch.text)


def _parse_pubmed_xml(payload: str) -> List[Record]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return []

    records: List[Record] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = _element_text(article, ".//PMID")
        if not pmid:
            continue

        title = _element_text(article, ".//ArticleTitle")

        # Structured abstracts hold one node per section; labels are prefixed so
        # that section headings remain visible to the operator on inspection.
        segments: List[str] = []
        for node in article.findall(".//Abstract/AbstractText"):
            text = "".join(node.itertext()).strip()
            if not text:
                continue
            label = (node.get("Label") or "").strip()
            segments.append(f"{label}: {text}" if label else text)

        doi = ""
        for identifier in article.findall(".//ArticleIdList/ArticleId"):
            if identifier.get("IdType") == "doi":
                doi = (identifier.text or "").strip().lower()
                break

        records.append(
            Record(
                source="pubmed",
                identifier=pmid,
                title=title,
                abstract=" ".join(segments),
                journal=_element_text(article, ".//Journal/ISOAbbreviation")
                or _element_text(article, ".//Journal/Title"),
                year=_pubmed_year(article),
                doi=doi,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )
        )
    return records


def _element_text(node: ElementTree.Element, path: str) -> str:
    found = node.find(path)
    return _WHITESPACE.sub(" ", "".join(found.itertext()).strip()) if found is not None else ""


def _pubmed_year(article: ElementTree.Element) -> Optional[int]:
    """Read the publication year, tolerating MedlineDate ranges such as '2019-2020'."""
    year = _element_text(article, ".//PubDate/Year")
    if not year:
        medline = _element_text(article, ".//PubDate/MedlineDate")
        match = re.search(r"\b(1\d{3}|20\d{2})\b", medline)
        year = match.group(1) if match else ""
    try:
        return int(year)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# OpenAlex
# --------------------------------------------------------------------------

def search_openalex(query: str, max_results: int, plan: Optional[QueryPlan] = None) -> Tuple[List[Record], str]:
    """Retrieve from OpenAlex, degrading to a plain anchor query if needed.

    Returns the records and a note describing any degradation, so the caller can
    surface it rather than presenting a narrower search as the intended one.
    """
    limit = max(1, min(RESULT_CEILING, max_results))
    params = {
        "search": to_openalex_query(query),
        "per_page": limit,
        "select": "id,doi,display_name,publication_year,abstract_inverted_index,primary_location",
    }
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL

    response = _request("GET", f"{OPENALEX_BASE}/works", params=params)
    note = ""

    if response.status_code == 400 and plan is not None:
        params["search"] = to_openalex_plain_query(plan)
        response = _request("GET", f"{OPENALEX_BASE}/works", params=params)
        note = "Boolean expression rejected; anchor-only query substituted for this source."

    if response.status_code == 400:
        raise requests.HTTPError("OpenAlex rejected the query expression")

    results = response.json().get("results", []) or []
    return [_parse_openalex_work(work) for work in results if work.get("display_name")], note


def _parse_openalex_work(work: dict) -> Record:
    doi = (work.get("doi") or "").replace("https://doi.org/", "").strip().lower()
    identifier = (work.get("id") or "").rsplit("/", 1)[-1]
    location = work.get("primary_location") or {}
    source = location.get("source") or {}

    return Record(
        source="openalex",
        identifier=identifier,
        title=_WHITESPACE.sub(" ", (work.get("display_name") or "").strip()),
        abstract=_reconstruct_abstract(work.get("abstract_inverted_index")),
        journal=(source.get("display_name") or "").strip(),
        year=work.get("publication_year"),
        doi=doi,
        url=location.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else work.get("id", "")),
    )


def _reconstruct_abstract(inverted: Optional[dict]) -> str:
    """Rebuild running text from OpenAlex's inverted position index.

    OpenAlex stores abstracts as token to position lists for licensing reasons.
    Reconstruction is required because every lexical statistic in this system is
    computed over the abstract text.
    """
    if not inverted:
        return ""
    positions: List[Tuple[int, str]] = [
        (position, token)
        for token, offsets in inverted.items()
        for position in offsets
    ]
    if not positions:
        return ""
    positions.sort(key=lambda item: item[0])
    return _WHITESPACE.sub(" ", " ".join(token for _, token in positions)).strip()


# --------------------------------------------------------------------------
# Normalisation, deduplication, filtering
# --------------------------------------------------------------------------

def normalise_title_key(title: str) -> str:
    """Deduplication key for records without a DOI.

    Case folding, punctuation removal and whitespace collapse only. Stemming is
    deliberately excluded: it would merge distinct papers whose titles differ by
    a single inflected word, understating the retrieved corpus.
    """
    return _WHITESPACE.sub(" ", _TITLE_PUNCT.sub(" ", (title or "").lower())).strip()


def deduplicate(records: List[Record]) -> List[Record]:
    """Collapse duplicates on DOI, falling back to a normalised title key.

    Runs before scoring so that a work indexed in both sources cannot contribute
    twice to the direct-hit count. The record retaining the longer abstract is
    kept, because abstract length determines how much text the scorer can see.
    """
    by_key: Dict[str, Record] = {}
    ordered_keys: List[str] = []

    for record in records:
        key = f"doi:{record.doi}" if record.doi else f"title:{normalise_title_key(record.title)}"
        if not key.split(":", 1)[1]:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = record
            ordered_keys.append(key)
        elif len(record.abstract) > len(existing.abstract):
            by_key[key] = record

    return [by_key[key] for key in ordered_keys]


def apply_exclusions(records: List[Record], exclusions: List[str]) -> List[Record]:
    """Drop records matching any exclusion term, uniformly across sources.

    Applied client-side in addition to the native negative clause. PubMed and
    OpenAlex interpret negation differently; filtering here guarantees that both
    sources contribute records screened by the same rule, which is a
    precondition for combining their counts.
    """
    if not exclusions:
        return records
    lowered = [term.lower() for term in exclusions if term]
    kept: List[Record] = []
    for record in records:
        haystack = f"{record.title} {record.abstract}".lower()
        if not any(term in haystack for term in lowered):
            kept.append(record)
    return kept


def has_usable_text(record: Record) -> bool:
    """Whether a record carries enough text to be scored on its abstract."""
    return len(record.abstract.strip()) >= _ABSTRACT_MIN_LENGTH


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def retrieve(plan: QueryPlan, source: str, max_results: int) -> SourceResult:
    """Execute the plan, relaxing the query only if the strict form is too sparse.

    The relaxation decision and the query actually executed are both recorded on
    the result. A support level derived from a relaxed query answers a broader
    question than the one the operator posed, and must be labelled as such.
    """
    result = _retrieve_once(plan, plan.strict_query, source, max_results)
    result.relaxed = False

    if len(result.records) < MIN_RECORDS_BEFORE_RELAXATION and plan.relaxed_query != plan.strict_query:
        relaxed = _retrieve_once(plan, plan.relaxed_query, source, max_results)
        if len(relaxed.records) > len(result.records):
            relaxed.relaxed = True
            return relaxed

    return result


def _retrieve_once(plan: QueryPlan, query: str, source: str, max_results: int) -> SourceResult:
    result = SourceResult(query_used=query)
    collected: List[Record] = []

    if source in ("pubmed", "combined"):
        try:
            found = search_pubmed(query, max_results)
            collected.extend(found)
            result.sources_used.append("pubmed")
            result.raw_counts["pubmed"] = len(found)
        except Exception as exc:
            result.errors["pubmed"] = str(exc)

    if source in ("openalex", "combined"):
        try:
            found, note = search_openalex(query, max_results, plan)
            collected.extend(found)
            result.sources_used.append("openalex")
            result.raw_counts["openalex"] = len(found)
            if note:
                result.errors["openalex_note"] = note
        except Exception as exc:
            result.errors["openalex"] = str(exc)

    filtered = apply_exclusions(deduplicate(collected), plan.exclusions)
    result.records = [record for record in filtered if has_usable_text(record)]
    result.raw_counts["deduplicated"] = len(filtered)
    result.raw_counts["scorable"] = len(result.records)
    return result


def check_sources() -> List[Dict[str, object]]:
    """Lightweight reachability probe for the health endpoint."""
    checks: List[Dict[str, object]] = []

    for name, method, url, params in (
        ("pubmed", "GET", f"{PUBMED_BASE}/einfo.fcgi", _pubmed_params({"retmode": "json"})),
        ("openalex", "GET", f"{OPENALEX_BASE}/works", {"per_page": 1, "select": "id"}),
    ):
        try:
            response = _request(method, url, params=params)
            checks.append({
                "name": name,
                "reachable": response.status_code < 400,
                "detail": f"HTTP {response.status_code}",
            })
        except Exception as exc:
            checks.append({"name": name, "reachable": False, "detail": str(exc)})

    return checks