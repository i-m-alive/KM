"""DB-polling background worker for two-phase agents.

Run from the backend/ directory:
    python -m app.worker

Picks up `pending` runs for background agents, runs Phase 1 (detect), and
either parks them at `awaiting_review` or auto-applies. No broker (Redis/Celery)
- appropriate for the current single-Postgres scale. Phase 2 (apply on approval)
is driven by the review endpoint, not here.
"""

import asyncio
import logging

from app.agents.registry import get_agent, is_background
from app.db import SessionLocal
from app.models import AgentRun
from app.runs.background import mark_failed, run_detection

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(levelname)s %(message)s")
log = logging.getLogger("naviknow.worker")

POLL_INTERVAL_SECONDS = 2.0


def _claim_next_pending() -> tuple | None:
    """Claim one pending background run: flip it to a working status so it isn't
    picked up twice, and return (run_id, agent_id)."""
    db = SessionLocal()
    try:
        run = (
            db.query(AgentRun)
            .filter(AgentRun.status == "pending")
            .order_by(AgentRun.created_at.asc())
            .first()
        )
        if run is None:
            return None
        agent = get_agent(run.agent_id)
        if agent is None or not is_background(agent):
            return None  # interactive agents don't run here
        run.status = "working"
        db.commit()
        return (run.id, run.agent_id)
    finally:
        db.close()


async def _process(run_id, agent_id) -> None:
    db = SessionLocal()
    try:
        run = db.get(AgentRun, run_id)
        agent = get_agent(agent_id)
        log.info("detecting run %s (%s)", run_id, agent_id)
        try:
            await run_detection(db, run, agent)
            log.info("run %s -> %s", run_id, run.status)
        except Exception as exc:
            log.exception("run %s failed", run_id)
            mark_failed(db, run, str(exc))
    finally:
        db.close()


async def main() -> None:
    log.info("worker started; polling every %.1fs", POLL_INTERVAL_SECONDS)
    while True:
        claimed = _claim_next_pending()
        if claimed is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        await _process(*claimed)


if __name__ == "__main__":
    asyncio.run(main())
