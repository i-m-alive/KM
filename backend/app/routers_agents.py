from fastapi import APIRouter, Depends

from app.agents.registry import list_agents
from app.auth.deps import get_current_user
from app.models import User
from app.schemas import AgentOut

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentOut])
def get_agent_catalogue(_: User = Depends(get_current_user)) -> list[AgentOut]:
    return [
        AgentOut(
            agent_id=agent.agent_id,
            display_name=agent.display_name,
            description=agent.description,
            mode=agent.mode,
            tools=agent.tools,
        )
        for agent in list_agents()
    ]
