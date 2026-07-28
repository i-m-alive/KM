from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populates os.environ from .env so boto3's standard credential chain (which
# reads real env vars, not our Settings model) can see AWS_ACCESS_KEY_ID etc.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql+psycopg://naviknow:naviknow@localhost:5433/naviknow"

    # Auth
    JWT_SECRET: str = "change-me-in-env"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_EXPIRE_DAYS: int = 7

    # AWS / Bedrock
    AWS_REGION: str = "us-east-1"
    BEDROCK_MODEL_ID: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"

    # $ per 1K tokens, keyed by Bedrock model id. Extend as new models are used.
    BEDROCK_PRICING_USD_PER_1K: dict[str, dict[str, float]] = {
        "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 0.003, "output": 0.015},
        "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": 0.0008, "output": 0.004},
        "anthropic.claude-3-haiku-20240307-v1:0": {"input": 0.00025, "output": 0.00125},
        "anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.001, "output": 0.005},
        "global.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.001, "output": 0.005},
        # Intro pricing through 2026-08-31 ($2/$10 per 1M); becomes $3/$15 per 1M
        # (0.003/0.015 per 1K) after - update this row when that window closes.
        "anthropic.claude-sonnet-5": {"input": 0.002, "output": 0.010},
        "global.anthropic.claude-sonnet-5": {"input": 0.002, "output": 0.010},
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 0.003, "output": 0.015},
        "anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 0.003, "output": 0.015},
        # used when BEDROCK_MODEL_ID isn't in this table
        "_default": {"input": 0.003, "output": 0.015},
    }

    # Storage
    OUTPUTS_DIR: str = "outputs"
    UPLOADS_DIR: str = "uploads"

    # Chunking (Sanitization)
    CHUNK_PARAGRAPHS: int = 8  # paragraphs per DOCX chunk
    CHUNK_OVERLAP_CHARS: int = 200  # small overlap so entities aren't lost at seams

    # Below its entity type's threshold, a not-yet-known candidate entity is
    # excluded from the proposal entirely (not even a warning) rather than
    # re-surfacing on every single run - a reviewer can still add it manually
    # via "add entity" if one genuinely matters. Distinct from
    # MIN_OCR_ENTITY_LENGTH's confidence cap (which produces exactly these low
    # scores for short logo-OCR fragments) - this is what actually acts on
    # that cap.
    #
    # Per-entity-type, not one global scalar: a missed CLIENT_NAME and a
    # wrongly-masked common noun are not symmetric failures, but one shared
    # threshold treats them as if they were. EVERY VALUE BELOW IS THE SAME
    # UNTUNED 0.6 DEFAULT the single global threshold used before this was
    # split - none of these are measured. The intended way to tune them is
    # the review_deltas.precision_miss_by_type / recall_miss_by_type counters
    # (see agent.py's apply()) accumulated across real reviewed runs, not a
    # guess made here. A type missing from this map falls back to 0.6 (see
    # its .get(..., 0.6) call sites).
    SANITIZATION_CONFIDENCE_THRESHOLDS: dict[str, float] = {
        "CLIENT_NAME": 0.6,
        "CLIENT_PERSON": 0.6,
        "CLIENT_LOCATION": 0.6,
        "CLIENT_EMAIL_DOMAIN": 0.6,
        "CLIENT_SYSTEM_NAME": 0.6,
        "CLIENT_CONTRACT_ID": 0.6,
    }

    # Precision QA: an extra Bedrock pass over the MASKED text that flags mask
    # tokens whose surrounding grammar suggests a common/generic word was
    # over-redacted (e.g. "[CLIENT_18] Allocation" from "Capital Allocation").
    # Advisory only - never blocks a run. Off by default since it's one more
    # paid call per run; enable once you've seen its signal/cost on real runs.
    SANITIZATION_PRECISION_CHECK_ENABLED: bool = False

    # Custom replacement aliases (Task C): when a reviewer sets an alias for
    # an entity, this gates the LLM "is this alias itself a real
    # organization" check (alias_validate.py) on top of the always-on
    # deterministic dictionary-collision checks. Unlike the flags above,
    # defaults True - aliases are already an explicit, opt-in reviewer
    # action (nothing changes for a run where nobody sets one), so there's
    # no existing-run cost to protect against, only a real safety gap
    # (silently accepting an alias that happens to name a real company) if
    # left off.
    SANITIZATION_ALIAS_VALIDATION_ENABLED: bool = True

    # Mosaic re-identification QA: an extra Bedrock pass that tries to guess
    # the real client behind each mask token using ONLY the remaining
    # unmasked text (quantities, events, dates, unique descriptors). Advisory
    # only - never blocks a run. Off by default, same cost reasoning as above.
    SANITIZATION_REIDENTIFY_ENABLED: bool = False
    SANITIZATION_REIDENTIFY_THRESHOLD: float = 0.5

    # Revalidation agent: a second, independent pass over the RENDERED masked
    # output (see revalidate.py) that catches what verify.py structurally
    # cannot - an entity that was never in the dictionary in the first place
    # (a total detection miss), or a partial-name fragment left next to its
    # own mask token. Unlike precision/reidentify above, defaults True and
    # runs unconditionally on every completed run: this exists specifically
    # to catch what nobody already knew to look for, so gating it behind
    # manual opt-in would defeat the point. Findings are surfaced as
    # "blocking" flags for a human to approve/reject, never auto-applied.
    SANITIZATION_REVALIDATION_ENABLED: bool = True

    # Tagging
    TAG_CONFIDENCE_THRESHOLD: float = 0.7  # below this, flag for reviewer instead of auto-applying

    # Worker stale-run reaper: a run stuck at a non-terminal working status
    # (crash or restart mid-detect leaves it there forever, with no way to
    # tell "stuck" from "slow" in the UI) is failed cleanly after this long.
    # Generous vs. the observed 2-5 min real detect times, so a legitimately
    # slow run isn't reaped mid-flight.
    STALE_RUN_TIMEOUT_MINUTES: int = 15

    # Sanitization guardrails
    # Your own delivery firm's name(s) - never proposed/masked as a CLIENT_NAME,
    # in text, OCR'd image text, or the vision-scan judgment itself. Matched as
    # a normalized "contains this token" check, not a bare prefix - a prefix
    # match on e.g. "navi" would also wrongly exclude unrelated real companies
    # that happen to start with the same letters (Navistar, Navient, ...).
    OWN_FIRM_NAMES: list[str] = ["navikenz", "navicade", "spend"]
    # Perceptual hashes of known own-firm logo images - a second, OCR-
    # independent signal for the same exclusion. Needed because vision-model
    # OCR transcription is unreliable (observed: a stylized multi-color
    # "NAVICADE" wordmark correctly flagged contains_client_identity=True at
    # 99% confidence, but with EMPTY ocr_text - the text-based own-firm check
    # has nothing to match against in that case). Grows the same way client
    # logo_reference does: compute once from a confirmed own-firm image.
    OWN_FIRM_LOGO_PHASHES: list[str] = ["9b99e42e9542aeaa"]
    # OCR-derived candidates (from a logo/image, not a text run) shorter than
    # this are too collision-prone with ordinary words/acronyms to auto-trust
    # at full confidence - they still surface for reviewer visibility, just
    # capped below TAG's low-confidence bar so they need deliberate sign-off.
    MIN_OCR_ENTITY_LENGTH: int = 4

    # Document preview (LibreOffice headless -> PDF for DOCX/PPTX)
    SOFFICE_PATH: str | None = None  # override if `soffice` isn't discoverable on PATH

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # Cookies
    REFRESH_COOKIE_NAME: str = "naviknow_refresh_token"
    COOKIE_SECURE: bool = False  # set True behind HTTPS in real deployments

    # If set, this email is (re-)promoted to the 'admin' role on every startup -
    # bootstraps the very first admin without a manual DB update.
    ADMIN_BOOTSTRAP_EMAIL: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
