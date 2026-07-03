from pathlib import Path
import os
import sys

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from paperrag.config import Settings, get_settings  # noqa: E402


def _clear_paperrag_env(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("PAPERRAG_"):
            monkeypatch.delenv(key, raising=False)


def test_settings_defaults(monkeypatch) -> None:
    _clear_paperrag_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert (
        settings.database_url
        == "postgresql+psycopg://paperrag:paperrag@localhost:5432/paperrag"
    )
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.llm_model == "qwen2.5:7b-instruct-q4_K_M"
    assert settings.embed_dim == 1024
    assert settings.search_suggestion_limit == 3
    assert settings.relation_top_k == 20
    assert settings.data_dir == Path("data")
    assert settings.result_dir == Path("outputs")


def test_settings_env_override(monkeypatch) -> None:
    _clear_paperrag_env(monkeypatch)
    monkeypatch.setenv("PAPERRAG_DATABASE_URL", "postgresql+psycopg://user:pass@db:5432/app")
    monkeypatch.setenv("PAPERRAG_EMBED_DIM", "768")
    monkeypatch.setenv("PAPERRAG_RESULT_DIR", "/tmp/paperrag-results")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://user:pass@db:5432/app"
    assert settings.embed_dim == 768
    assert settings.result_dir == Path("/tmp/paperrag-results")


def test_get_settings_uses_env_prefix(monkeypatch) -> None:
    _clear_paperrag_env(monkeypatch)
    get_settings.cache_clear()
    monkeypatch.setenv("PAPERRAG_LOG_LEVEL", "DEBUG")

    settings = get_settings()

    assert settings.log_level == "DEBUG"
    get_settings.cache_clear()
