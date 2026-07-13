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
import time
from datetime import datetime, timedelta, timezone

from app.agents.registry import get_agent, is_background
from app.config import get_settings
from app.db import SessionLocal
from app.models import AgentRun
from app.runs.background import mark_failed, run_detection

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(levelname)s %(message)s")
log = logging.getLogger("naviknow.worker")

settings = get_settings()

POLL_INTERVAL_SECONDS = 2.0
REAP_CHECK_INTERVAL_SECONDS = 60.0

# Statuses a run can legitimately sit at indefinitely. Anything else is a
# transient working state ("working", an agent's detect label like
# "detecting", "applying") - if a run has been there longer than the timeout,
# the process driving it is gone (crash/restart) and it will never move again.
_RESTING_STATUSES = ("pending", "awaiting_review", "completed", "completed_with_issues", "failed", "rejected")


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


def _reap_stale_runs() -> None:
    """Fail cleanly any run stuck at a transient working status past the
    timeout - a crash or restart mid-detect() otherwise leaves it 'detecting'
    forever with no automatic recovery and no signal that it's actually dead."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.STALE_RUN_TIMEOUT_MINUTES)
    db = SessionLocal()
    try:
        stale = (
            db.query(AgentRun)
            .filter(AgentRun.status.notin_(_RESTING_STATUSES), AgentRun.updated_at < cutoff)
            .all()
        )
        for run in stale:
            log.warning("reaping stale run %s (status '%s' since %s)", run.id, run.status, run.updated_at)
            mark_failed(
                db, run,
                f"Run was stuck at status '{run.status}' for more than "
                f"{settings.STALE_RUN_TIMEOUT_MINUTES} minutes (likely a worker crash or restart mid-run) "
                "and was failed automatically. Re-run the agent to try again.",
            )
    except Exception:
        log.exception("stale-run reaper pass failed")
    finally:
        db.close()


async def main() -> None:
    log.info(
        "worker started; polling every %.1fs, reaping runs stuck >%d min",
        POLL_INTERVAL_SECONDS, settings.STALE_RUN_TIMEOUT_MINUTES,
    )
    last_reap = 0.0
    while True:
        if time.monotonic() - last_reap >= REAP_CHECK_INTERVAL_SECONDS:
            _reap_stale_runs()
            last_reap = time.monotonic()
        claimed = _claim_next_pending()
        if claimed is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        await _process(*claimed)


if __name__ == "__main__":
    asyncio.run(main())
