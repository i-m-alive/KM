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
    "Do NOT mask: our own consultants' names, generic industry or technology terms (retail, AWS, "
    "Kafka...), public email domains (gmail.com), or dollar amounts.\n\n"
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
