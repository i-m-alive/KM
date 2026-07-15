import logging

from app.agents.base import Agent, BackgroundAgent
from app.agents.dummy_agent import DummyEchoAgent

log = logging.getLogger("naviknow.registry")

# Interactive agents run synchronously inside POST /runs.
# Background agents (Sanitization, Tagging) are executed by the worker.
_INTERACTIVE: list[Agent] = [DummyEchoAgent()]
_BACKGROUND: list[BackgroundAgent] = []  # populated in Phase 2/3


def _load_background() -> None:
    """Import background agents lazily so the web process doesn't need their
    heavy deps (Presidio, etc.) unless they're registered."""
    if _BACKGROUND:
        return
    try:
        from app.agents.sanitization.agent import SanitizationAgent

        _BACKGROUND.append(SanitizationAgent())
    except Exception:
        log.exception("Sanitization agent failed to load (check deps: presidio, pdfplumber)")
    try:
        from app.agents.tagging.agent import TaggingAgent

        _BACKGROUND.append(TaggingAgent())
    except Exception:
        log.exception("Tagging agent failed to load")
    try:
        from app.agents.coordinator.agent import CoordinatorAgent

        _BACKGROUND.append(CoordinatorAgent())
    except Exception:
        log.exception("Coordinator agent failed to load")


def _all() -> dict[str, object]:
    _load_background()
    return {a.agent_id: a for a in [*_INTERACTIVE, *_BACKGROUND]}


def list_agents() -> list[object]:
    return list(_all().values())


def get_agent(agent_id: str):
    return _all().get(agent_id)


def is_background(agent) -> bool:
    return isinstance(agent, BackgroundAgent)
