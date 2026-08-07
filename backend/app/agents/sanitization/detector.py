"""Detector — the LLM pass. Uses the Bedrock tool-use loop with a direct,
in-process fs_read_document tool: the model reads the document via a plain
Python function call (no MCP subprocess/JSON-RPC) and returns the
client-identifying SURFACE STRINGS (not spans — spans are found
deterministically by agent code afterwards, since LLMs are unreliable at exact
offsets). Seeded with the free NER candidates so it rarely re-reads.
"""

from app.agents.sanitization.ner_prepass import Candidate
from app.agents.sanitization.tools import FS_READ_DOCUMENT_SPEC, sanitization_tool_executor
from app.llm import bedrock_client

ENTITY_TYPES = [
    "CLIENT_NAME",
    "CLIENT_PERSON",
    "CLIENT_LOCATION",
    "CLIENT_EMAIL_DOMAIN",
    "CLIENT_SYSTEM_NAME",
    "CLIENT_CONTRACT_ID",
    # Generic PII types (Phase 1) - distinct from the CLIENT_* types above:
    # these cover identifying information that is NOT specifically about who
    # the client is (a third party's name, a personal contact phone number, a
    # postal address) but should still be masked. The LLM chooses between a
    # CLIENT_* type and its generic counterpart using context, exactly as it
    # already disambiguates any other ambiguous surface string.
    "PERSON",
    "ORGANIZATION",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    # Infrastructure & Security (Phase 2). INFRA_IDENTIFIER is an ordinary,
    # reviewer-overridable mask; CREDENTIAL is mandatory and non-overridable
    # once confirmed - see agent.py's apply() and regex_patterns/
    # infra_credential.py's docstring for why that split exists.
    "INFRA_IDENTIFIER",
    "CREDENTIAL",
    # Phase 3. Five of these default to FLAG (proposed, but NOT masked
    # unless the reviewer opts in) rather than mask-by-default - see
    # entity_actions.py's FLAG_DEFAULT_TYPES for exactly which and why.
    # CLIENT_PERSON_TITLE/CLIENT_PHONE mask by default like every other
    # CLIENT_* type. INTERNAL_TEAM_MEMBER's default depends on a per-person
    # consent lookup, not a static rule.
    # NEGATIVE_OUTCOME is deliberately NOT in this list - it's a document-
    # level signal (does this document discuss a failure/breach/negative
    # metric AT ALL), not a span/surface_text entity like everything else
    # here. See sensitive_outcome.py and agent.py's detect().
    "COMMERCIAL_TERM",
    "COMPETITOR_NAME",
    "STRATEGY_MENTION",
    "OWN_COST_DETAIL",
    "ORG_CHART_STRUCTURE",
    "INTERNAL_TEAM_MEMBER",
    "CLIENT_PERSON_TITLE",
    "CLIENT_PHONE",
]

DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface_text": {"type": "string"},
                    "entity_type": {"type": "string", "enum": ENTITY_TYPES},
                    "confidence": {"type": "number"},
                },
                "required": ["surface_text", "entity_type", "confidence"],
            },
        }
    },
    "required": ["entities"],
}

SYSTEM_PROMPT = (
    "You are the Sanitization Detector for a knowledge-management platform. Your job is to find "
    "every CLIENT-IDENTIFYING string in a document so it can be masked. Client-identifying means it "
    "reveals WHICH client this work was for: the client company name and its aliases, names of people "
    "who work at the client, the client's offices/locations, the client's email domains, the client's "
    "proprietary system/product names, and client contract/account identifiers.\n\n"
    "You must ALSO find generic personally-identifying information (PII) that is NOT specifically "
    "about who the client is, but is still identifying and should be masked: a person's name that "
    "isn't a client stakeholder or one of our own consultants (e.g. a quoted third party, a vendor "
    "contact), an organization name that isn't the client itself (e.g. a named subcontractor, partner, "
    "or competitor), a full email address, a phone number, or a postal/mailing address. Use the "
    "generic types PERSON, ORGANIZATION, EMAIL, PHONE, and ADDRESS for these - reserve the CLIENT_* "
    "types strictly for information that identifies the client. If you're genuinely unsure whether a "
    "name belongs to the client or a third party, prefer the CLIENT_* type (a missed client identifier "
    "is a worse failure than an over-cautious mask).\n\n"
    "You must ALSO find infrastructure and security identifiers: internal hostnames, IP addresses, "
    "internal domains, and firewall/network identifiers - classify these as INFRA_IDENTIFIER. And you "
    "must find credentials: API keys, access tokens, bearer tokens, passwords, or database connection "
    "strings with embedded credentials - classify these as CREDENTIAL. A CREDENTIAL you confirm will be "
    "removed unconditionally (a reviewer cannot choose to keep it), so only use CREDENTIAL for something "
    "that is actually a secret/credential shape, not a generic-looking number or identifier - when "
    "unsure whether a technical string is a credential or just an identifier, prefer INFRA_IDENTIFIER.\n\n"
    "You must ALSO find (Phase 3) - these are proposed for review but, unlike everything above, are NOT "
    "masked by default (a case study's value is in its real numbers and methodology, so over-redacting "
    "them would gut its credibility - a human decides what to do with each one):\n"
    "- COMMERCIAL_TERM: contract value, deal size, margins, discounts, pricing/billing structure, "
    "payment terms, penalty clauses, SLA-breach details.\n"
    "- COMPETITOR_NAME: a named vendor who competed for or lost this engagement, or any other named "
    "competitor of the client.\n"
    "- STRATEGY_MENTION: M&A plans, unannounced initiatives, roadmaps, or other forward-looking business "
    "strategy.\n"
    "- OWN_COST_DETAIL: OUR OWN delivery firm's internal staffing ratios, day-rate/FTE figures, or exact "
    "internal accelerator/framework configurations (not the client's costs - those are COMMERCIAL_TERM).\n"
    "- ORG_CHART_STRUCTURE: a reporting-line diagram or a structured block of name/title pairs that maps "
    "to real individuals (client or internal).\n\n"
    "You must ALSO find CLIENT_PERSON_TITLE (a job title tied to a named client stakeholder, e.g. 'VP of "
    "Operations') and CLIENT_PHONE (a phone number tied to a named client stakeholder, as opposed to a "
    "generic PHONE with no clear stakeholder link, or a CLIENT_CONTRACT_ID account/contract number) - "
    "both mask by default like any other CLIENT_* type.\n\n"
    "You must ALSO find INTERNAL_TEAM_MEMBER: OUR OWN consultants' names (and any of them named/pictured "
    "alongside the client) - classify these as INTERNAL_TEAM_MEMBER instead of silently ignoring them. "
    "Whether that name actually gets masked depends on a consent record you don't have visibility into; "
    "your job is only to identify them, not to decide.\n\n"
    "Do NOT mask: generic industry or technology terms (retail, AWS, Kafka...), public email domains "
    "(gmail.com), or a dollar amount with no commercial-term context (e.g. a KPI like \"grew revenue by "
    "$2M\" is not itself a COMMERCIAL_TERM unless it's describing the deal/contract itself).\n\n"
    "You may call fs_read_document(document_id, start_chunk, end_chunk) to read the document in "
    "chunk ranges. Read enough to be exhaustive — a missed client identifier is a data leak. "
    "Return every client-identifying surface string once, with its type and your confidence (0-1). "
    "Prefer to confirm or reject the provided candidate strings, and add any you find that they missed. "
    "You will also be given image alt-text/labels found in the document, separately from its body - these "
    "are often messy descriptive phrases (e.g. 'GMR Group | Delhi', 'Bandhan Bank Vector Logo Free "
    "Download') rather than clean names. Extract any client-identifying entity embedded in them the same "
    "way you would from body text - a name that appears ONLY in alt-text is just as real a leak as one in "
    "the body."
)


async def detect_entities(
    document_id: str, total_chunks: int, candidates: list[Candidate], alt_texts: list[str] | None = None,
) -> bedrock_client.BedrockResponse:
    """Run the tool-use detection loop. Returns a BedrockResponse whose .parsed
    is {"entities": [...]} and which carries token/cost usage.

    `alt_texts` (image descr/title values) are passed as extra CONTEXT, not
    fed through fs_read_document - they live outside the chunked body text
    entirely (see agent.py's alt-text merge), so the model needs them handed
    over directly rather than discovering them by reading chunks."""
    candidate_lines = "\n".join(
        f"- {c.surface_text!r} (guess: {c.entity_type_guess}; seen {c.occurrences}x)"
        + (f" e.g. ...{c.contexts[0]}..." if c.contexts else "")
        for c in candidates
    ) or "(none found by the pre-pass; read the document and find them yourself)"

    alt_text_block = ""
    if alt_texts:
        alt_text_lines = "\n".join(f"- {a!r}" for a in alt_texts)
        alt_text_block = f"\nImage alt-text/labels found in this document (NOT part of the body text):\n{alt_text_lines}\n"

    user_message = (
        f"document_id: {document_id}\n"
        f"total_chunks: {total_chunks} (chunk ids 0..{total_chunks - 1})\n\n"
        f"Candidate strings from the deterministic pre-pass:\n{candidate_lines}\n"
        f"{alt_text_block}\n"
        "Read the document with fs_read_document and return the client-identifying entities as JSON."
    )

    # Cap was too tight for real-sized decks/reports: at 16 max_iterations a
    # document with >13 chunks could exhaust its budget before the model ever
    # read every chunk, silently missing entities that only appear once, late
    # in the document. Raised so total_chunks + 3 has room to actually cover
    # documents up to ~30 chunks before hitting the ceiling.
    return await bedrock_client.converse_with_tools(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        tool_specs=[FS_READ_DOCUMENT_SPEC],
        tool_executor=sanitization_tool_executor,
        response_schema=DETECT_SCHEMA,
        max_iterations=max(4, min(total_chunks + 3, 32)),
        # SYSTEM_PROMPT is stable across every iteration of this loop and
        # across every document - exactly what a cachePoint is for. This was
        # already plumbed into converse_with_tools but never turned on here
        # (near-free cost/latency win on any multi-chunk document).
        cache_system_prompt=True,
    )
