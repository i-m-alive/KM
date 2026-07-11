from app.agents.base import Agent
from app.agents.dummy_agent import DummyEchoAgent

# Add new agents here as they're built (Sanitization, Tagging, Cleanup, Coordinator, Search, Deck).
AGENTS: dict[str, Agent] = {
    agent.agent_id: agent
    for agent in [
        DummyEchoAgent(),
    ]
}


def list_agents() -> list[Agent]:
    return list(AGENTS.values())


def get_agent(agent_id: str) -> Agent | None:
    return AGENTS.get(agent_id)
