from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Azure OpenAI / Foundry
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    chat_deployment: str = "gpt-5.6-luna"
    utility_deployment: str = "gpt-5-nano"
    embed_deployment: str = "text-embedding-3-small"
    use_managed_identity: bool = False

    # Web search
    tavily_api_key: str = ""

    # Memory
    redis_url: str = "redis://localhost:6379"

    # Routing thresholds (similarity, per index — ADR-0004)
    cache_threshold: float = 0.85
    chunk_threshold: float = 0.70
    borderline_floor: float = 0.55
    slow_ttl_days: int = 7
    top_k: int = 5

    # Reliability
    search_timeout_s: float = 5.0
    fetch_timeout_s: float = 10.0
    llm_timeout_s: float = 30.0
    utility_timeout_s: float = 15.0
    degraded_answers: bool = True

    # HTTP API
    api_key: str = ""
    rate_limit_per_min: int = 30
    max_query_chars: int = 2000


@lru_cache
def get_settings() -> Settings:
    return Settings()
