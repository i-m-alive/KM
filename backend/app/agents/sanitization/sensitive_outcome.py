"""Sensitive Outcome detection (Phase 3) - a single Bedrock pass over the
ORIGINAL (unmasked) document text that asks one document-level question:
does this document discuss a failure, outage, breach, compliance violation,
negative metric, or regulatory finding at all.

Deliberately NOT a span/surface_text entity like everything else the
Detector proposes (see detector.py's ENTITY_TYPES docstring) - a root-cause
narrative isn't a string you mask, it's a judgment call about whether this
document is safe to share at all, which is exactly why it becomes a
distinct, non-dismissible reviewer callout (see agent.py's detect() and
review/routes.py's submit_review) instead of a checkbox in the mask table.

Runs during detect() - not folded into the existing summarizer.py pass,
which only ever sees the ALREADY-MASKED text in apply() (after approval).
This has to see the ORIGINAL text, and has to be visible to the reviewer
BEFORE they approve, not after.
"""

from app.llm import bedrock_client

NEGATIVE_OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "discusses_negative_outcome": {"type": "boolean"},
        "excerpts": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["discusses_negative_outcome", "excerpts", "summary"],
}

SYSTEM_PROMPT = (
    "Read this engagement document and decide whether it discusses a SENSITIVE OUTCOME: the root cause "
    "of a failure, outage, or security breach; a compliance or regulatory violation and its finding; or "
    "a negative metric (missed target, revenue decline, customer loss) presented as a factual result of "
    "this engagement. This is NOT about tone - a case study can be candid about challenges without this "
    "flag; it's specifically about whether sharing this document could expose the client (or us) to "
    "relationship, legal, or reputational risk if shared without their explicit sign-off.\n\n"
    "Set discusses_negative_outcome=true only if such content is actually present. If true, quote the "
    "specific sentence(s) verbatim into excerpts (empty list if false), and write a one-sentence, "
    "reviewer-facing summary of what was found (empty string if false)."
)


async def check_negative_outcome(text: str) -> bedrock_client.BedrockResponse:
    """Always makes a call (unlike reidentify's early-return) - there's no
    cheap deterministic signal here to gate the call on the way an empty
    mask_tokens list gates reidentify(); every document needs this read at
    least once."""
    excerpt = text[:24000]
    return await bedrock_client.converse(
        system_prompt=SYSTEM_PROMPT,
        user_message=f"Document text:\n\n{excerpt}",
        response_schema=NEGATIVE_OUTCOME_SCHEMA,
    )
