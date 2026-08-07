"""Mosaic re-identification QA — a single adversarial Bedrock pass over the
MASKED text that tries to guess the real client behind each mask token using
ONLY the remaining unmasked text: co-occurring facts like acquisitions,
quantities (AUM, revenue), dates, and other unique descriptors that no
surface-string detector reasons about at all.

Every other detector in this pipeline (regex, dictionary, LLM detector, image
scan) operates on SURFACE STRINGS - a name, a logo, an account id. None of
them can catch the case where the client's NAME is masked but the surrounding
facts still uniquely identify them (the observed BlackRock/Aladdin case: name
and product masked, but "Preqin acquisition", "~$25T AUM", "Azure OpenAI",
"Dec 2025 AWS partnership" left in cleartext are enough for any reader to
re-identify the client). This is a missing STAGE, not a weak detector - hence
a dedicated adversarial pass rather than a tweak to an existing one.

Advisory only - never mutates anything, never blocks a run. A human decides
whether the cited phrases need masking or generalizing.
"""

from app.llm import bedrock_client

REIDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "guesses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mask_token": {"type": "string"},
                    "candidate_org": {"type": "string"},
                    "confidence": {"type": "number"},
                    "leaking_phrases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["mask_token", "candidate_org", "confidence", "leaking_phrases"],
            },
        }
    },
    "required": ["guesses"],
}

SYSTEM_PROMPT = (
    "You are trying to identify the real organization behind each [CLIENT_N] mask token in this "
    "document. Using ONLY the remaining unmasked text - NOT any outside knowledge you'd normally "
    "guess from the token itself - name your top candidate organization for each token, a confidence "
    "0-1, and the SPECIFIC phrases in the text that led you there (e.g. a distinctive acquisition, a "
    "precise AUM/revenue figure, a named partnership, a unique product combination, a date tied to a "
    "known event). Be adversarial: assume you are a motivated reader trying to unmask this document, "
    "not a cooperative assistant. If a token has no real identifying signal left in the surrounding "
    "text, still include it with a low confidence and an empty or weak leaking_phrases list rather "
    "than omitting it - omitting a token is not the same as judging it safe."
)


async def reidentify(masked_text: str, mask_tokens: list[str]) -> bedrock_client.BedrockResponse | None:
    """Returns None (no call made) if there's nothing to check."""
    if not mask_tokens:
        return None

    excerpt = masked_text[:24000]
    tokens_line = ", ".join(sorted(set(mask_tokens)))
    user_message = (
        f"Mask tokens present in this document: {tokens_line}\n\n"
        f"Masked document:\n\n{excerpt}"
    )
    return await bedrock_client.converse(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        response_schema=REIDENTIFY_SCHEMA,
    )
