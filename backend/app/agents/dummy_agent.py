import time
import uuid
from datetime import datetime
from typing import Any

from app.agents.base import Agent, AgentFlag, AgentResult, AgentStep
from app.llm import bedrock_client
from app.storage.local_store import save_run_output

LOW_CONFIDENCE_THRESHOLD = 0.6

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["summary", "confidence"],
}


class DummyEchoAgent(Agent):
    agent_id = "dummy-echo"
    display_name = "Demo Agent"
    description = "Exercises the full NaviKnow pipeline: summarizes input text via Claude on Bedrock and reports confidence. No real KM logic - plumbing only."
    mode = "interactive"
    tools = ["bedrock"]

    async def run(self, input_data: dict[str, Any]) -> AgentResult:
        steps: list[AgentStep] = []
        flags: list[AgentFlag] = []
        run_id = str(uuid.uuid4())

        # Step 1: parse input
        step_start = time.monotonic()
        text = (input_data.get("text") or "").strip()
        if not text:
            raise ValueError("input_data.text must be a non-empty string")
        steps.append(
            AgentStep(
                order=1,
                name="parse input",
                detail=f"Validated non-empty text input ({len(text)} characters).",
                tool=None,
                duration_ms=int((time.monotonic() - step_start) * 1000),
            )
        )

        # Step 2: call Claude
        step_start = time.monotonic()
        response = await bedrock_client.converse(
            system_prompt=(
                "You are a concise summarization assistant. Summarize the user's text in exactly "
                "one sentence, then rate from 0.0 to 1.0 how confident you are that the summary "
                "accurately captures the text."
            ),
            user_message=text,
            response_schema=SUMMARY_SCHEMA,
        )
        summary = response.parsed["summary"]
        confidence = float(response.parsed["confidence"])
        steps.append(
            AgentStep(
                order=2,
                name="call Claude",
                detail="Requested a one-sentence summary and self-reported confidence via Bedrock Converse.",
                tool="bedrock",
                duration_ms=int((time.monotonic() - step_start) * 1000),
            )
        )

        # Step 3: score + flag
        step_start = time.monotonic()
        if confidence < LOW_CONFIDENCE_THRESHOLD:
            flags.append(
                AgentFlag(
                    message="Low confidence - review before trusting this summary.",
                    severity="warning",
                )
            )
        steps.append(
            AgentStep(
                order=3,
                name="score + flag",
                detail=f"Confidence {confidence:.2f} evaluated against threshold {LOW_CONFIDENCE_THRESHOLD}.",
                tool=None,
                duration_ms=int((time.monotonic() - step_start) * 1000),
            )
        )

        output = {"input_text": text, "summary": summary, "confidence": confidence}

        # Step 4: save output
        step_start = time.monotonic()
        output_file_path = save_run_output(
            self.agent_id,
            run_id,
            {
                "run_id": run_id,
                "agent_id": self.agent_id,
                "output": output,
                "confidence": confidence,
                "flags": [{"message": f.message, "severity": f.severity} for f in flags],
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "estimated_cost_usd": response.estimated_cost_usd,
                "generated_at": datetime.utcnow().isoformat(),
            },
        )
        steps.append(
            AgentStep(
                order=4,
                name="save output",
                detail=f"Wrote run output to {output_file_path}.",
                tool=None,
                duration_ms=int((time.monotonic() - step_start) * 1000),
            )
        )

        return AgentResult(
            agent_id=self.agent_id,
            output=output,
            confidence=confidence,
            flags=flags,
            steps=steps,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            estimated_cost_usd=response.estimated_cost_usd,
            output_file_path=output_file_path,
        )
