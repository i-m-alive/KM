"""Deterministic, free candidate detection: regex for structured identifiers +
(optional) Presidio for names/orgs/locations. Output is deduped to distinct
surface strings the LLM Detector then classifies.

Presidio is optional — if it (or its spaCy model) isn't installed, we degrade to
regex-only and lean on the LLM Detector to catch names/orgs from the text.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.documents.extract import Chunk as DocChunk

# ---- Regex for structured identifiers (always available, zero cost) ----
_EMAIL_RE = re.compile(r"\b[\w.+-]+@([A-Za-z0-9-]+\.[A-Za-z0-9.-]+)\b")
_URL_RE = re.compile(r"\bhttps?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b")
_ACCOUNT_RE = re.compile(r"\b(?:[A-Z]{2,}-?\d{4,}|\d{6,})\b")
# Full email address, distinct from _EMAIL_RE above which only ever surfaces
# the DOMAIN (for CLIENT_EMAIL_DOMAIN). This is a separate candidate under a
# separate surface string (the whole address vs. just the domain), so it
# cannot collide with or change the existing domain-only detection.
_FULL_EMAIL_RE = re.compile(r"\b[\w.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+\b")
# Best-effort postal/mailing address heuristic (Phase 1: generic PII, no
# address detection existed before this). Two independent shapes, since a
# single regex can't reasonably cover both: a US-style street line (number +
# street name + common suffix), or a "City, ST 12345" locality line. Neither
# is exhaustive - this is a free, deterministic SIGNAL for the LLM Detector
# to confirm/reject, not a claim of complete address recall.
_STREET_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,4}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|"
    r"Circle|Cir|Highway|Hwy|Parkway|Pkwy|Terrace|Ter|Trail|Trl|Square|Sq|Loop|Crescent|Cres|Row|Path)\b\.?"
    r"(?:,?\s+(?:Suite|Ste|Apt|Unit|#)\s*[A-Za-z0-9-]+)?",
    re.IGNORECASE,
)
_CITY_STATE_ZIP_RE = re.compile(r"\b[A-Za-z][A-Za-z .'-]{1,40},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")

# Common public email/domain suffixes we should NOT treat as client identifiers.
_PUBLIC_DOMAINS = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "example.com"}

_PRESIDIO_TO_TYPE = {
    "PERSON": "CLIENT_PERSON",
    "LOCATION": "CLIENT_LOCATION",
    "GPE": "CLIENT_LOCATION",
    "ORG": "CLIENT_NAME",
    "ORGANIZATION": "CLIENT_NAME",
    "NRP": "CLIENT_NAME",
}


@dataclass
class Candidate:
    surface_text: str
    entity_type_guess: str
    source: str  # "regex" | "presidio"
    occurrences: int = 0
    contexts: list[str] = field(default_factory=list)


@lru_cache
def _analyzer():
    """Load Presidio lazily; return None if unavailable so we degrade gracefully."""
    try:
        from presidio_analyzer import AnalyzerEngine

        return AnalyzerEngine()
    except Exception:
        return None


def _regex_candidates(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for m in _EMAIL_RE.finditer(text):
        domain = m.group(1).lower()
        if domain not in _PUBLIC_DOMAINS:
            found.append((domain, "CLIENT_EMAIL_DOMAIN"))
    for m in _URL_RE.finditer(text):
        domain = m.group(1).lower()
        if domain not in _PUBLIC_DOMAINS:
            found.append((domain, "CLIENT_EMAIL_DOMAIN"))
    for m in _PHONE_RE.finditer(text):
        found.append((m.group(0).strip(), "CLIENT_CONTRACT_ID"))
    for m in _ACCOUNT_RE.finditer(text):
        found.append((m.group(0).strip(), "CLIENT_CONTRACT_ID"))
    # Generic PII (Phase 1) - additive, distinct surface strings from the
    # CLIENT_* candidates above so neither list's dedupe key collides with
    # the other's.
    for m in _FULL_EMAIL_RE.finditer(text):
        found.append((m.group(0).strip(), "EMAIL"))
    for m in _STREET_ADDRESS_RE.finditer(text):
        found.append((m.group(0).strip(), "ADDRESS"))
    for m in _CITY_STATE_ZIP_RE.finditer(text):
        found.append((m.group(0).strip(), "ADDRESS"))
    return found


def _presidio_candidates(text: str) -> list[tuple[str, str]]:
    analyzer = _analyzer()
    if analyzer is None:
        return []
    try:
        results = analyzer.analyze(text=text, language="en")
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for r in results:
        etype = _PRESIDIO_TO_TYPE.get(r.entity_type)
        if not etype:
            continue
        surface = text[r.start : r.end].strip()
        if len(surface) >= 2:
            out.append((surface, etype))
    return out


def presidio_available() -> bool:
    return _analyzer() is not None


def regex_candidates_for_text(text: str) -> list[tuple[str, str]]:
    """Public wrapper over the regex identifier pass, for callers outside this
    module (e.g. OCR'd image text) that want the same free, deterministic
    detection document text already gets, without reaching into a private name."""
    return _regex_candidates(text)


def extract_candidates(chunks: list[DocChunk]) -> list[Candidate]:
    """Deduped distinct candidate surface strings across the whole document."""
    by_key: dict[str, Candidate] = {}
    for chunk in chunks:
        pairs = _regex_candidates(chunk.text) + _presidio_candidates(chunk.text)
        for surface, etype in pairs:
            key = surface.lower()
            if key not in by_key:
                by_key[key] = Candidate(surface_text=surface, entity_type_guess=etype, source="regex/presidio")
            cand = by_key[key]
            cand.occurrences += 1
            if len(cand.contexts) < 2:
                # a short context window helps the LLM judge if it's client-identifying
                idx = chunk.text.lower().find(key)
                if idx >= 0:
                    start = max(0, idx - 40)
                    cand.contexts.append(chunk.text[start : idx + len(surface) + 40].replace("\n", " "))
    return list(by_key.values())
