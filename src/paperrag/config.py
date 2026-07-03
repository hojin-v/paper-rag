from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAPERRAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://paperrag:paperrag@localhost:5432/paperrag"
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    llm_timeout_seconds: int = 120
    embed_base_url: str = "http://localhost:8100"
    embed_dim: int = 1024
    embed_timeout_seconds: int = 60
    data_dir: Path = Path("./data")
    result_dir: Path = Path("./outputs")
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    search_suggestion_limit: int = 3
    search_similarity_threshold: float = 0.6
    relation_top_k: int = 20
    paragraph_min_chars: int = 100
    paragraph_max_chars: int = 1500
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
