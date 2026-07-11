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
        # used when BEDROCK_MODEL_ID isn't in this table
        "_default": {"input": 0.003, "output": 0.015},
    }

    # Storage
    OUTPUTS_DIR: str = "outputs"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # Cookies
    REFRESH_COOKIE_NAME: str = "naviknow_refresh_token"
    COOKIE_SECURE: bool = False  # set True behind HTTPS in real deployments


@lru_cache
def get_settings() -> Settings:
    return Settings()
