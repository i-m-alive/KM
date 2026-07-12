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


def _repair_trailing_object(candidate: str, exc: json.JSONDecodeError) -> dict[str, Any] | None:
    """Some models (observed with Haiku 4.5, never with Sonnet) echo the
    response schema as one complete JSON object, then tack the real answer on
    as a second object glued on with a stray leading comma - invalid JSON,
    but the intent is clear. If json.loads stopped with "Extra data" right
    after a complete object, try parsing the leftover as its own object and
    merge the two (the real answer's keys sit alongside the echoed schema's -
    callers only read the specific keys they expect, so the extra noise from
    the echoed schema is harmless). Returns None if the shape doesn't match,
    so the caller falls back to the original clear error."""
    if "Extra data" not in exc.msg:
        return None
    head, tail = candidate[: exc.pos], candidate[exc.pos :].strip()
    if not tail.startswith(",") or not tail.endswith("}"):
        return None
    try:
        first = json.loads(head)
        second = json.loads("{" + tail[1:])
    except json.JSONDecodeError:
        return None
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    return {**first, **second}


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
        repaired = _repair_trailing_object(candidate, exc)
        if repaired is not None:
            return repaired
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


_VISION_FORMATS = {"png", "jpeg", "jpg", "gif", "webp"}


async def converse_vision(
    system_prompt: str,
    user_message: str,
    image_bytes: bytes,
    image_format: str,
    response_schema: dict[str, Any] | None = None,
) -> BedrockResponse:
    """A single image + text turn, for scanning embedded images (logos,
    screenshots) that text extraction can never see. Bedrock Converse's image
    block, not a separate vision API."""
    fmt = image_format.lower().lstrip(".")
    fmt = "jpeg" if fmt == "jpg" else fmt
    if fmt not in _VISION_FORMATS:
        raise BedrockCallError(f"Unsupported image format for vision call: {image_format}")

    effective_system_prompt = system_prompt
    if response_schema is not None:
        effective_system_prompt = (
            f"{system_prompt}\n\nRespond with ONLY a single valid JSON object matching this schema - "
            f"no markdown fences, no commentary:\n{json.dumps(response_schema)}"
        )

    try:
        response = _client().converse(
            modelId=settings.BEDROCK_MODEL_ID,
            system=[{"text": effective_system_prompt}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
                        {"text": user_message},
                    ],
                }
            ],
        )
    except Exception as exc:
        raise BedrockCallError(f"Bedrock Converse (vision) call failed: {exc}") from exc

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


async def converse_with_tools(
    system_prompt: str,
    user_message: str,
    tool_specs: list[dict],
    tool_executor,
    response_schema: dict[str, Any] | None = None,
    max_iterations: int = 12,
    cache_system_prompt: bool = False,
) -> BedrockResponse:
    """Bedrock Converse tool-use loop. The model may call the provided tools;
    `tool_executor(name, input_dict)` (async) runs each and returns a string.
    Token/cost is summed across every iteration. This is the centralized loop
    all tool-using agents share.

    Prompt caching: a cachePoint is placed after the (stable) system prompt so
    the skill/instructions are cached across the loop's iterations and across
    documents (reads bill at ~0.1x input)."""
    effective_system_prompt = system_prompt
    if response_schema is not None:
        effective_system_prompt = (
            f"{system_prompt}\n\n"
            "When you have finished using tools, respond with ONLY a single valid JSON object "
            "matching this schema - no markdown fences, no commentary:\n"
            f"{json.dumps(response_schema)}"
        )

    system_blocks: list[dict] = [{"text": effective_system_prompt}]
    if cache_system_prompt:
        system_blocks.append({"cachePoint": {"type": "default"}})

    messages: list[dict] = [{"role": "user", "content": [{"text": user_message}]}]
    tool_config = {"tools": tool_specs} if tool_specs else None

    total_input = 0
    total_output = 0
    final_text = ""

    for _ in range(max_iterations):
        kwargs = {
            "modelId": settings.BEDROCK_MODEL_ID,
            "system": system_blocks,
            "messages": messages,
        }
        if tool_config:
            kwargs["toolConfig"] = tool_config

        try:
            response = _client().converse(**kwargs)
        except Exception as exc:
            raise BedrockCallError(f"Bedrock Converse (tools) call failed: {exc}") from exc

        usage = response.get("usage", {})
        total_input += usage.get("inputTokens", 0)
        total_output += usage.get("outputTokens", 0)

        output_message = response["output"]["message"]
        content = output_message["content"]
        messages.append({"role": "assistant", "content": content})

        text_this_turn = "".join(b.get("text", "") for b in content if "text" in b)
        if text_this_turn:
            final_text = text_this_turn

        stop_reason = response.get("stopReason")
        if stop_reason == "tool_use":
            tool_results = []
            for block in content:
                if "toolUse" not in block:
                    continue
                tu = block["toolUse"]
                try:
                    result_text = await tool_executor(tu["name"], tu.get("input", {}))
                    tool_results.append(
                        {"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"text": result_text}]}}
                    )
                except Exception as exc:
                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": tu["toolUseId"],
                                "content": [{"text": f"error: {exc}"}],
                                "status": "error",
                            }
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn (or any non-tool stop): done.
        break

    parsed = _extract_json(final_text) if response_schema is not None else None
    return BedrockResponse(
        text=final_text,
        parsed=parsed,
        input_tokens=total_input,
        output_tokens=total_output,
        estimated_cost_usd=_estimate_cost(total_input, total_output),
    )
