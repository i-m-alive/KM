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

    @abstractmethod
    async def run(self, input_data: dict[str, Any]) -> AgentResult: ...
