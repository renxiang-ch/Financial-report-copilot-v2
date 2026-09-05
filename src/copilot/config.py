from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it works regardless of cwd
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # LLM. Any OpenAI-compatible endpoint works: leave openai_base_url empty for
    # api.openai.com, or point it at an aggregator (AIHubMix and similar) to reach
    # other vendors' models through the same SDK and the same tool-calling shape.
    openai_api_key: str = ""
    openai_base_url: str = ""

    # Database
    database_url: str = "postgresql://postgres:password@localhost:5432/financial_copilot"

    # Langfuse
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # API access key (empty = no auth required)
    api_key: str = ""

    # App
    log_level: str = "INFO"


settings = Settings()
