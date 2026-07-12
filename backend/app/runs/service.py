import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.agents.registry import get_agent
from app.models import AgentRun, RunFlag, RunStep


class UnknownAgentError(Exception):
    pass


class RoleNotAllowedError(Exception):
    pass


async def execute_run(
    db: Session, *, agent_id: str, input_data: dict[str, Any], created_by: uuid.UUID, created_by_role: str
) -> AgentRun:
    agent = get_agent(agent_id)
    if agent is None:
        raise UnknownAgentError(f"No agent registered with id '{agent_id}'")

    if agent.allowed_roles and created_by_role not in agent.allowed_roles:
        raise RoleNotAllowedError(f"Role '{created_by_role}' is not permitted to trigger agent '{agent_id}'")

    run = AgentRun(agent_id=agent_id, status="running", input_json=input_data, created_by=created_by)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        result = await agent.run(input_data)
    except Exception as exc:
        run.status = "failed"
        run.output_json = {"error": str(exc)}
        run.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(run)
        return run

    run.status = "completed"
    run.output_json = result.output
    run.confidence = result.confidence
    run.input_tokens = result.input_tokens
    run.output_tokens = result.output_tokens
    run.estimated_cost_usd = result.estimated_cost_usd
    run.output_file_path = result.output_file_path
    run.completed_at = datetime.utcnow()

    for step in result.steps:
        db.add(
            RunStep(
                run_id=run.id,
                step_order=step.order,
                name=step.name,
                detail=step.detail,
                tool=step.tool,
                duration_ms=step.duration_ms,
            )
        )

    for flag in result.flags:
        db.add(RunFlag(run_id=run.id, message=flag.message, severity=flag.severity))

    db.commit()
    db.refresh(run)
    return run
