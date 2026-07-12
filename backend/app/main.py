import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.routes import router as auth_router
from app.config import get_settings
from app.documents.routes import router as documents_router
from app.review.routes import router as review_router
from app.routers_tags import router as tags_router
from app.db import Base, SessionLocal, engine
from app.models import Role, TagVocabulary, User
from app.routers_admin import router as admin_router
from app.routers_agents import router as agents_router
from app.routers_governance import router as governance_router
from app.runs.routes import router as runs_router

settings = get_settings()

SEED_ROLES = ["admin", "km_governance", "km_reviewer", "practice_lead", "delivery", "read_only"]


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


# Starter controlled vocabulary for Tagging (A-02). KM-governance manages it
# from here via the vocabulary router; this only seeds the initial set.
SEED_TAG_VOCABULARY: dict[str, list[str]] = {
    "domain": ["retail", "banking", "healthcare", "public-sector", "telecom", "manufacturing", "insurance", "energy"],
    "use_case": ["data-migration", "cloud-modernisation", "fraud-detection", "cx-transformation", "analytics", "automation", "security-uplift"],
    "technology": ["aws", "azure", "gcp", "snowflake", "sap", "salesforce", "react", "kafka", "kubernetes", "databricks"],
    "geography": ["na", "emea", "apac", "india", "uk", "us", "latam"],
    "engagement_type": ["assessment", "implementation", "managed-service", "advisory", "migration"],
}


def _seed_tag_vocabulary() -> None:
    db = SessionLocal()
    try:
        existing = {(t.category, t.value) for t in db.query(TagVocabulary).all()}
        for category, values in SEED_TAG_VOCABULARY.items():
            for value in values:
                if (category, value) not in existing:
                    db.add(TagVocabulary(category=category, value=value, status="approved"))
        db.commit()
    finally:
        db.close()


def _bootstrap_admin() -> None:
    if not settings.ADMIN_BOOTSTRAP_EMAIL:
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == settings.ADMIN_BOOTSTRAP_EMAIL).first()
        if user is None:
            return
        admin_role = db.query(Role).filter(Role.name == "admin").first()
        if admin_role is not None and user.role_id != admin_role.id:
            user.role_id = admin_role.id
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.OUTPUTS_DIR, exist_ok=True)
    # Additive-only: creates tables for any new models without touching
    # existing ones, so schema changes don't require wiping the dev volume.
    Base.metadata.create_all(bind=engine)
    _seed_roles()
    _seed_tag_vocabulary()
    _bootstrap_admin()
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
app.include_router(admin_router)
app.include_router(governance_router)
app.include_router(documents_router)
app.include_router(review_router)
app.include_router(tags_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
