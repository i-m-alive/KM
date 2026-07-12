"""Summarizer — produces the sanitized summary + tag-relevant metadata hints
that the Tagging agent consumes. Runs over MASKED text, so no client identity
leaks into the taggable metadata."""

from app.llm import bedrock_client

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "sanitized_summary": {"type": "string"},
        "metadata": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "use_case": {"type": "array", "items": {"type": "string"}},
                "technology": {"type": "array", "items": {"type": "string"}},
                "geography": {"type": "array", "items": {"type": "string"}},
                "engagement_type": {"type": "string"},
            },
        },
    },
    "required": ["sanitized_summary", "metadata"],
}

SYSTEM_PROMPT = (
    "You summarize an already-sanitized (client-masked) engagement document for a knowledge base. "
    "Client identity has been replaced with tokens like [CLIENT_1] - never try to guess who the client is. "
    "Write a one-paragraph sanitized_summary, then extract tag-relevant hints: domain, use_case(s), "
    "technology(ies), geography(ies), engagement_type. Use short lowercase terms. These are hints for a "
    "downstream tagging step, not final tags. "
    "domain and engagement_type are EACH A SINGLE SHORT TERM, not a list and not comma-separated - if "
    "several could apply, pick the ONE that best characterizes the whole document. "
    "use_case, technology, and geography MAY have several terms each, as separate array items."
)


async def summarize(masked_text: str) -> bedrock_client.BedrockResponse:
    # Bound the input; the summary needs the gist, not every page.
    excerpt = masked_text[:24000]
    return await bedrock_client.converse(
        system_prompt=SYSTEM_PROMPT,
        user_message=f"Sanitized document:\n\n{excerpt}",
        response_schema=SUMMARY_SCHEMA,
    )
