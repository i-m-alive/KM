from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class AgentStep:
    order: int
    name: str  # e.g. "parse input", "call Claude", "score confidence"
    detail: str  # short human-readable description of what happened
    tool: str | None = None  # e.g. "bedrock", "masking_dictionary", None for pure reasoning
    started_at: datetime = field(default_factory=datetime.utcnow)
    duration_ms: int | None = None


@dataclass
class AgentFlag:
    message: str
    severity: Literal["info", "warning", "blocking"] = "warning"


@dataclass
class AgentResult:
    agent_id: str
    output: dict[str, Any]  # the actual work product, agent-specific shape
    confidence: float  # 0.0-1.0, overall; agents may also nest per-item confidence inside `output`
    flags: list[AgentFlag]
    steps: list[AgentStep]
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    output_file_path: str | None  # path under backend/outputs/, if this run produced a file


class Agent(ABC):
    agent_id: str
    display_name: str
    description: str
    mode: Literal["background", "interactive"]
    tools: list[str]  # human-readable list, shown in the catalogue/demo UI
    allowed_roles: list[str] = []  # role names permitted to trigger this agent; empty = all roles

    @abstractmethod
    async def run(self, input_data: dict[str, Any]) -> AgentResult: ...


@dataclass
class ReviewProposal:
    """Phase-1 output of a background agent: what it proposes, plus whether a
    human must review before it is applied."""

    summary: str  # reviewer-facing one-liner
    needs_review: bool  # False => auto-apply (e.g. Tagging with all high-confidence in-vocab tags)
    proposal: dict[str, Any]  # the payload the reviewer sees (entities, tags, ...)
    steps: list[AgentStep]
    flags: list[AgentFlag]
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    working_status: str = "running"  # the in-progress status label (e.g. "detecting", "tagging")


class BackgroundAgent(ABC):
    """Two-phase agent executed by the worker: detect() proposes, the human
    reviews (unless auto-applied), then apply() commits. Detection is the LLM's
    job; application is deterministic code's."""

    agent_id: str
    display_name: str
    description: str
    mode: Literal["background"] = "background"
    tools: list[str]
    allowed_roles: list[str] = []

    @abstractmethod
    async def detect(self, db, run) -> ReviewProposal:
        """Phase 1 — read input, propose. Must not mutate the durable dictionary/
        vocabulary as approved; new items go in as pending_approval."""
        ...

    @abstractmethod
    async def apply(self, db, run, decision: dict[str, Any]) -> AgentResult:
        """Phase 2 — deterministic application of the (reviewer-approved) proposal."""
        ...
