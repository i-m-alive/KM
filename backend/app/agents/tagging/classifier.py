"""Tagging classifier — a single LLM pass mapping sanitized content to the
controlled vocabulary. Cheap (best-match, not exhaustive)."""

import json

from app.llm import bedrock_client
from app.tags.service import CATEGORIES

TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["category", "value", "confidence"],
            },
        }
    },
    "required": ["tags"],
}

SYSTEM_PROMPT = (
    "You are the Tagging classifier for a knowledge base. You see ALREADY-SANITIZED content "
    "(client identity is masked as [CLIENT_x]) - never try to identify the client, and never "
    "propose a client as a tag. Assign tags from a controlled vocabulary across five categories: "
    "domain, use_case, technology, geography, engagement_type. Prefer an existing vocabulary value "
    "verbatim; only propose a new lowercase term when nothing fits. domain and engagement_type are "
    "single-valued; the others may have several. Give a confidence (0-1) per tag."
)


async def classify(sanitized_summary: str, hints: dict, vocabulary: dict[str, list[str]]) -> bedrock_client.BedrockResponse:
    user_message = (
        f"Controlled vocabulary (prefer these values):\n{json.dumps(vocabulary, indent=2)}\n\n"
        f"Sanitized summary:\n{sanitized_summary or '(none)'}\n\n"
        f"Metadata hints from sanitization:\n{json.dumps(hints or {}, indent=2)}\n\n"
        "Return the tags as JSON."
    )
    return await bedrock_client.converse(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        response_schema=TAG_SCHEMA,
    )
