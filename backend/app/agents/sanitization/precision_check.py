"""Precision QA — a single Bedrock pass over the MASKED text that flags mask
tokens whose surrounding grammar suggests a common/generic word was
over-redacted, e.g. "[CLIENT_18] Allocation" from "Capital Allocation", or a
bare "[CLIENT_55]" from "partners" used as an ordinary business term.

Verification (verify.py) is one-sided by design: it only checks that a
flagged surface disappeared, never that a masked token SHOULD have been
flagged in the first place. This module is the other half - advisory only,
never mutates anything, never blocks a run. A human still decides whether a
flagged token is really an over-redaction.
"""

from app.llm import bedrock_client

PRECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mask_token": {"type": "string"},
                    "surrounding_text": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["mask_token", "surrounding_text", "reason", "confidence"],
            },
        }
    },
    "required": ["flags"],
}

SYSTEM_PROMPT = (
    "You are doing PRECISION QA on an already-sanitized document. Sensitive spans have been "
    "replaced with mask tokens like [CLIENT_18]. For each mask token that appears in the text, "
    "judge from its grammatical context whether the ORIGINAL text was almost certainly a genuine "
    "proper noun identifying a real client/company/organization/person - or whether it was more "
    "likely an ordinary common/generic word or phrase that got over-redacted. Two real examples of "
    "the failure this catches: '[CLIENT_18] Allocation' where the original phrase was the ordinary "
    "term 'Capital Allocation', and a bare '[CLIENT_55]' where the original word was 'partners' used "
    "as a generic business term, not a company name.\n\n"
    "Flag ONLY tokens you have real reason to suspect are over-redactions of a common word or phrase. "
    "Do not flag a token just because you can't tell what it replaced - only flag when the surrounding "
    "grammar strongly suggests a generic word sits there, not a proper noun. If every token in the text "
    "looks like a genuine proper noun given its context, return an empty flags list.\n\n"
    "For each flag, quote the exact surrounding phrase or sentence as it appears (with the token still "
    "in place) in surrounding_text, give a short reason, and a confidence (0-1) that this is really an "
    "over-redaction."
)


async def check_precision(masked_text: str, mask_tokens: list[str]) -> bedrock_client.BedrockResponse | None:
    """Returns None (no call made) if there's nothing to check - an empty
    document or one where nothing was masked has no precision question to
    ask."""
    if not mask_tokens:
        return None

    # Bound the input; the model needs enough surrounding context per token,
    # not the whole document verbatim for a very large deck.
    excerpt = masked_text[:24000]
    tokens_line = ", ".join(sorted(set(mask_tokens)))
    user_message = (
        f"Mask tokens present in this document: {tokens_line}\n\n"
        f"Masked document:\n\n{excerpt}"
    )
    return await bedrock_client.converse(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        response_schema=PRECISION_SCHEMA,
    )
