"""Alias validation — the LLM half. Judges whether a reviewer-proposed
replacement string (e.g. "Acme Pharma" for [CLIENT_16]) is itself a real,
identifiable organization - which would just trade one leak for another,
different one (aliasing a masked client to "Pfizer" doesn't hide anything if
Pfizer is a real company, it just falsely implicates a different one).

The deterministic half (does the alias collide with another entity already
in OUR dictionary, or with an already-assigned alias) lives in
masking/dictionary.py's validate_custom_replacement - that only knows about
entities THIS system has already seen. This model call is what catches a
real-world company our own dictionary has never heard of.

Advisory in effect, not architecture: unlike Tasks 1/2's precision/
re-identification flags, a failed alias validation does not silently ship
with a warning attached - the alias is REJECTED (masking falls back to the
[CLIENT_N] token) and a flag explains why, since a bad alias is a fresh leak,
not a residual one to merely flag for later review.
"""

from app.llm import bedrock_client

ALIAS_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_real_organization": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_real_organization", "reason"],
}

SYSTEM_PROMPT = (
    "You are validating a proposed REPLACEMENT name for a masked client entity in a sanitized "
    "document - e.g. the real client is masked as [CLIENT_16], and a reviewer wants every occurrence "
    "replaced with a chosen alias instead of the generic token. Judge whether that proposed alias "
    "itself identifies a REAL, existing, identifiable organization or company (which would be a "
    "problem - it either falsely implicates a real, unrelated company, or is simply not fictional "
    "enough to serve as a safe placeholder), as opposed to a clearly fictional or generic placeholder "
    "name (e.g. 'Acme Pharma', 'Client A', 'ClientCo', 'Contoso'). "
    "Respond with is_real_organization (true if it names a real company you recognize) and a short reason."
)


async def validate_alias(alias: str) -> bedrock_client.BedrockResponse:
    return await bedrock_client.converse(
        system_prompt=SYSTEM_PROMPT,
        user_message=f"Proposed alias: {alias!r}",
        response_schema=ALIAS_VALIDATION_SCHEMA,
    )
