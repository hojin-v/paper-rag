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

    def generate_json(self, prompt: str, schema_hint: str, operation: str = "") -> dict[str, Any]:
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


def test_enrich_paragraph_retries_when_summary_contains_chinese() -> None:
    client = FakeLLM(
        [
            {
                "summary": "模型使用中文摘要。",
                "keywords": ["문서 분석"],
                "is_topic_relevant": True,
            },
            {
                "summary": "모델이 문서를 분석한다.",
                "keywords": ["문서 분석"],
                "is_topic_relevant": True,
            },
        ]
    )

    enriched = enrich_paragraph(client, "The model analyzes documents.")

    assert client.calls == 2
    assert enriched.summary == "모델이 문서를 분석한다."


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
    settings = Settings(
        _env_file=None,
        llm_cache_dir=tmp_path,
        llm_observability_enabled=False,
        heavy_task_max_concurrency=0,
    )
    client = OllamaClient(settings)

    first = client.generate_json("prompt", '{"summary":"string"}')
    second = client.generate_json("prompt", '{"summary":"string"}')

    assert first == second == {"summary": "cached"}
    assert calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def _fake_post_with_tokens(*args: Any, **kwargs: Any) -> httpx.Response:
    request = httpx.Request("POST", str(args[0]))
    return httpx.Response(
        200,
        request=request,
        json={
            "message": {"content": '{"summary":"ok"}'},
            "prompt_eval_count": 12,
            "eval_count": 34,
        },
    )


def test_ollama_client_records_successful_call(monkeypatch, tmp_path: Path) -> None:
    """실제 호출이 성공하면 operation·모델·지연시간·토큰 수를 관찰 테이블에 기록해야 한다."""
    monkeypatch.setattr(httpx, "post", _fake_post_with_tokens)
    recorded: dict[str, Any] = {}

    def fake_record(engine: Any, **kwargs: Any) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr("paperrag.ingest.llm_enrich.record_llm_call", fake_record)
    settings = Settings(
        _env_file=None,
        llm_cache_dir=tmp_path,
        llm_cache_enabled=False,
        heavy_task_max_concurrency=0,
    )
    client = OllamaClient(settings)

    result = client.generate_json("prompt", '{"summary":"string"}', operation="paragraph_enrich")

    assert result == {"summary": "ok"}
    assert recorded["operation"] == "paragraph_enrich"
    assert recorded["model"] == settings.llm_model
    assert recorded["success"] is True
    assert recorded["cache_hit"] is False
    assert recorded["prompt_tokens"] == 12
    assert recorded["completion_tokens"] == 34
    assert recorded["latency_ms"] >= 0.0


def test_ollama_client_records_cache_hit(monkeypatch, tmp_path: Path) -> None:
    """캐시 히트도 기록하되 cache_hit=True, latency_ms=0으로 남겨야 한다."""
    calls = 0

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", str(args[0]))
        return httpx.Response(
            200, request=request, json={"message": {"content": '{"summary":"cached"}'}}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    recorded_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "paperrag.ingest.llm_enrich.record_llm_call",
        lambda engine, **kwargs: recorded_calls.append(kwargs),
    )
    settings = Settings(
        _env_file=None,
        llm_cache_dir=tmp_path,
        heavy_task_max_concurrency=0,
    )
    client = OllamaClient(settings)

    client.generate_json("prompt", '{"summary":"string"}', operation="keywords")
    client.generate_json("prompt", '{"summary":"string"}', operation="keywords")

    assert calls == 1
    assert len(recorded_calls) == 2
    assert recorded_calls[0]["cache_hit"] is False
    assert recorded_calls[1]["cache_hit"] is True
    assert recorded_calls[1]["latency_ms"] == 0.0


def test_ollama_client_records_failure_and_reraises(monkeypatch, tmp_path: Path) -> None:
    """호출 실패 시에도 기록을 남기고, 기존 재시도/폴백 로직이 그대로 동작하도록 예외를 다시 던져야 한다."""

    def fake_post_error(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post_error)
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        "paperrag.ingest.llm_enrich.record_llm_call",
        lambda engine, **kwargs: recorded.update(kwargs),
    )
    settings = Settings(_env_file=None, llm_cache_dir=tmp_path, llm_cache_enabled=False, heavy_task_max_concurrency=0)
    client = OllamaClient(settings)

    with pytest.raises(httpx.ConnectError):
        client.generate_json("prompt", '{"summary":"string"}', operation="table_summary")

    assert recorded["success"] is False
    assert recorded["operation"] == "table_summary"
    assert "connection refused" in recorded["error"]


def test_ollama_client_skips_recording_when_observability_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_with_tokens)

    def _raise_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("관찰이 꺼져 있는데 record_llm_call이 호출됐다.")

    monkeypatch.setattr("paperrag.ingest.llm_enrich.record_llm_call", _raise_if_called)
    settings = Settings(
        _env_file=None,
        llm_cache_dir=tmp_path,
        llm_cache_enabled=False,
        llm_observability_enabled=False,
        heavy_task_max_concurrency=0,
    )
    client = OllamaClient(settings)

    result = client.generate_json("prompt", '{"summary":"string"}')

    assert result == {"summary": "ok"}


def test_ollama_client_uses_custom_temperature_in_payload(monkeypatch, tmp_path: Path) -> None:
    captured_payloads: list[dict[str, Any]] = []

    def fake_post_capture(*args: Any, **kwargs: Any) -> httpx.Response:
        captured_payloads.append(kwargs.get("json", {}))
        request = httpx.Request("POST", str(args[0]))
        return httpx.Response(
            200, request=request, json={"message": {"content": '{"summary":"ok"}'}}
        )

    monkeypatch.setattr(httpx, "post", fake_post_capture)
    settings = Settings(
        _env_file=None,
        llm_cache_dir=tmp_path,
        llm_cache_enabled=False,
        llm_temperature=0.0,
        heavy_task_max_concurrency=0,
    )
    client = OllamaClient(settings)

    client.generate_json("prompt", '{"summary":"string"}', operation="test", temperature=0.2)

    assert len(captured_payloads) == 1
    assert captured_payloads[0]["options"]["temperature"] == 0.2

