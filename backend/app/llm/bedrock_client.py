import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3

from app.config import get_settings

settings = get_settings()


@dataclass
class BedrockResponse:
    text: str
    parsed: dict[str, Any] | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class BedrockCallError(Exception):
    """Raised when the Bedrock call fails or returns a response we cannot use."""


@lru_cache
def _client():
    return boto3.client("bedrock-runtime", region_name=settings.AWS_REGION)


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    pricing = settings.BEDROCK_PRICING_USD_PER_1K.get(
        settings.BEDROCK_MODEL_ID, settings.BEDROCK_PRICING_USD_PER_1K["_default"]
    )
    return round((input_tokens / 1000) * pricing["input"] + (output_tokens / 1000) * pricing["output"], 6)


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from model output, tolerating markdown fences."""
    candidate = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1)
    else:
        brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace_match:
            candidate = brace_match.group(0)

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise BedrockCallError(f"Model did not return valid JSON: {exc}. Raw output: {text!r}") from exc


async def converse(
    system_prompt: str,
    user_message: str,
    response_schema: dict[str, Any] | None = None,
) -> BedrockResponse:
    """The only place in the codebase that talks to Bedrock. All agents route LLM calls through here
    so token/cost accounting stays centralized."""
    effective_system_prompt = system_prompt
    if response_schema is not None:
        effective_system_prompt = (
            f"{system_prompt}\n\n"
            "You must respond with ONLY a single valid JSON object matching this schema, "
            "and nothing else - no markdown fences, no commentary:\n"
            f"{json.dumps(response_schema)}"
        )

    try:
        response = _client().converse(
            modelId=settings.BEDROCK_MODEL_ID,
            system=[{"text": effective_system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
        )
    except Exception as exc:  # boto3 raises various botocore exceptions
        raise BedrockCallError(f"Bedrock Converse call failed: {exc}") from exc

    output_message = response["output"]["message"]
    text = "".join(block.get("text", "") for block in output_message["content"])
    usage = response.get("usage", {})
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)

    parsed = _extract_json(text) if response_schema is not None else None

    return BedrockResponse(
        text=text,
        parsed=parsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=_estimate_cost(input_tokens, output_tokens),
    )
