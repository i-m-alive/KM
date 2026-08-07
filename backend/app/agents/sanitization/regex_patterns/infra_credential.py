"""Deterministic, free candidate detection for infrastructure identifiers and
credentials (Phase 2). Same role as ner_prepass.py's existing regex block -
zero-cost signals fed to the LLM Detector as candidates, never applied
directly - but kept in their own module because CREDENTIAL carries a
different downstream contract than every other entity type: once the
Detector/reviewer confirms a surface as CREDENTIAL, it is masked
unconditionally (see agent.py's apply()) and cannot be un-included by a
reviewer, the same "mandatory, no override" contract already built for a
confirmed own-firm logo match.

That contract only holds up if the CREDENTIAL patterns below are high-
precision - a false positive here would force-redact something a reviewer
has no way to undo. Deliberately does NOT include a generic
"password\\s*[=:]\\s*..." keyword pattern for exactly this reason (too prone
to matching placeholder/example text); only distinctive, low-false-positive
credential SHAPES are covered. INFRA_IDENTIFIER is an ordinary, reviewer-
overridable mask like every Phase 1 type, so its bar is lower.
"""

import re

# ---- INFRA_IDENTIFIER: overridable mask, lower precision bar ----
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:/\d{1,2})?\b"
)
# A conservative subset of RFC 4291 forms - full IPv6 has many legal
# abbreviations; this catches the common colon-hex shapes without trying to
# be a complete IPv6 grammar (a partial match here is still a useful
# candidate for the LLM Detector to confirm/reject).
_IPV6_RE = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?\b")
_INTERNAL_HOSTNAME_RE = re.compile(
    r"\b[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"\.(?:internal|corp|intranet|lan|local)\b",
    re.IGNORECASE,
)

# ---- CREDENTIAL: mandatory, non-overridable once confirmed - distinctive
# shapes only, see module docstring. ----
# AWS-style access key ID prefixes (AKIA/ASIA/... = access key ID variants,
# not the secret itself - but a key ID alone is still an identifying,
# revocable credential and shouldn't be published).
_AWS_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA)[A-Z0-9]{16}\b")
_BEARER_TOKEN_RE = re.compile(r"\bBearer\s+[A-Za-z0-9\-_.~+/]{20,}={0,2}")
_CONNECTION_STRING_RE = re.compile(
    r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^:\s/'\"]+:[^@\s/'\"]+@[^\s/'\"]+"
)


def scan(text: str) -> list[tuple[str, str]]:
    """(surface, entity_type) pairs for INFRA_IDENTIFIER and CREDENTIAL shapes
    found in `text`. Same free, deterministic signal shape as
    ner_prepass.regex_candidates_for_text - callers dedupe by surface."""
    found: list[tuple[str, str]] = []
    for m in _IPV4_RE.finditer(text):
        found.append((m.group(0), "INFRA_IDENTIFIER"))
    for m in _IPV6_RE.finditer(text):
        found.append((m.group(0), "INFRA_IDENTIFIER"))
    for m in _INTERNAL_HOSTNAME_RE.finditer(text):
        found.append((m.group(0), "INFRA_IDENTIFIER"))
    for m in _AWS_KEY_ID_RE.finditer(text):
        found.append((m.group(0), "CREDENTIAL"))
    for m in _BEARER_TOKEN_RE.finditer(text):
        found.append((m.group(0).strip(), "CREDENTIAL"))
    for m in _CONNECTION_STRING_RE.finditer(text):
        found.append((m.group(0), "CREDENTIAL"))
    return found


# The set of entity types this module ever produces - used by callers (the
# mandatory-redaction enforcement in agent.py) that need to know "is this
# entity_type a CREDENTIAL-class type" without re-deriving it from scan().
CREDENTIAL_TYPES = {"CREDENTIAL"}
