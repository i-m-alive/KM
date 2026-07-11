import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db import SessionLocal
from app.models import Role
from app.routers_agents import router as agents_router
from app.runs.routes import router as runs_router

settings = get_settings()

SEED_ROLES = ["km_governance", "km_reviewer", "practice_lead", "delivery", "read_only"]


def _seed_roles() -> None:
    db = SessionLocal()
    try:
        existing = {r.name for r in db.query(Role).all()}
        for name in SEED_ROLES:
            if name not in existing:
                db.add(Role(name=name))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.OUTPUTS_DIR, exist_ok=True)
    _seed_roles()
    yield


app = FastAPI(title="NaviKnow", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(runs_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
