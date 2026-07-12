from pathlib import Path
from typing import Any

import httpx
import pytest

from paperrag.config import Settings
from paperrag.ingest.llm_enrich import (
    LLMOutputError,
    OllamaClient,
    enrich_paragraph,
    extract_paper_keywords,
    summarize_table,
)


class FakeLLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls = 0

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_enrich_paragraph_uses_fixed_json() -> None:
    client = FakeLLM(
        [
            {
                "summary": "요약",
                "keywords": ["검색"],
                "is_topic_relevant": True,
            }
        ]
    )

    enriched = enrich_paragraph(client, "원문  문장\n다음 줄")

    assert enriched.cleaned_text == "원문 문장 다음 줄"
    assert enriched.summary == "요약"
    assert enriched.keywords == ["검색"]


def test_enrich_paragraph_retries_once_after_failure() -> None:
    client = FakeLLM(
        [
            ValueError("bad json"),
            {
                "summary": "retry summary",
                "keywords": [],
                "is_topic_relevant": False,
            },
        ]
    )

    enriched = enrich_paragraph(client, "원문")

    assert client.calls == 2
    assert enriched.cleaned_text == "원문"
    assert enriched.is_topic_relevant is False


def test_enrich_paragraph_falls_back_after_two_failures() -> None:
    client = FakeLLM([ValueError("bad"), ValueError("still bad")])

    enriched = enrich_paragraph(client, "원문 텍스트")

    assert client.calls == 2
    assert enriched.cleaned_text == "원문 텍스트"
    assert enriched.summary == "원문 텍스트"
    assert enriched.keywords == []


def test_enrich_paragraph_does_not_hide_failure_in_production() -> None:
    client = FakeLLM([ValueError("bad"), ValueError("still bad")])
    client.settings = Settings(_env_file=None, allow_degraded_results=False)

    with pytest.raises(LLMOutputError, match="두 번 연속"):
        enrich_paragraph(client, "원문 텍스트")


def test_keyword_and_table_helpers_parse_fake_json() -> None:
    keyword_client = FakeLLM([{"keywords": ["RAG", "OCR", "검색"]}])
    table_client = FakeLLM([{"summary": "표 요약"}])

    assert extract_paper_keywords(keyword_client, "title", "abstract", ["summary"]) == [
        "RAG",
        "OCR",
        "검색",
    ]
    assert summarize_table(table_client, "a | b") == "표 요약"


def test_paper_keywords_retry_when_model_returns_fewer_than_three() -> None:
    client = FakeLLM(
        [
            {"keywords": ["OCR"]},
            {"keywords": ["OCR", "레이아웃", "문서 분석", "PaddleOCR"]},
        ]
    )

    keywords = extract_paper_keywords(client, "title", "abstract", ["summary"])

    assert keywords == ["OCR", "레이아웃", "문서 분석", "PaddleOCR"]
    assert client.calls == 2


def test_ollama_client_caches_valid_json(monkeypatch, tmp_path: Path) -> None:
    calls = 0

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", str(args[0]))
        return httpx.Response(
            200,
            request=request,
            json={"message": {"content": '{"summary":"cached"}'}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(_env_file=None, llm_cache_dir=tmp_path)
    client = OllamaClient(settings)

    first = client.generate_json("prompt", '{"summary":"string"}')
    second = client.generate_json("prompt", '{"summary":"string"}')

    assert first == second == {"summary": "cached"}
    assert calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
