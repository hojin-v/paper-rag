from typing import Any

from paperrag.ingest.llm_enrich import (
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
                "cleaned_text": "정제문",
                "summary": "요약",
                "keywords": ["검색"],
                "is_topic_relevant": True,
            }
        ]
    )

    enriched = enrich_paragraph(client, "원문")

    assert enriched.cleaned_text == "정제문"
    assert enriched.summary == "요약"
    assert enriched.keywords == ["검색"]


def test_enrich_paragraph_retries_once_after_failure() -> None:
    client = FakeLLM(
        [
            ValueError("bad json"),
            {
                "cleaned_text": "retry text",
                "summary": "retry summary",
                "keywords": [],
                "is_topic_relevant": False,
            },
        ]
    )

    enriched = enrich_paragraph(client, "원문")

    assert client.calls == 2
    assert enriched.cleaned_text == "retry text"
    assert enriched.is_topic_relevant is False


def test_enrich_paragraph_falls_back_after_two_failures() -> None:
    client = FakeLLM([ValueError("bad"), ValueError("still bad")])

    enriched = enrich_paragraph(client, "원문 텍스트")

    assert client.calls == 2
    assert enriched.cleaned_text == "원문 텍스트"
    assert enriched.summary == "원문 텍스트"
    assert enriched.keywords == []


def test_keyword_and_table_helpers_parse_fake_json() -> None:
    keyword_client = FakeLLM([{"keywords": ["RAG", "OCR", "검색"]}])
    table_client = FakeLLM([{"summary": "표 요약"}])

    assert extract_paper_keywords(keyword_client, "title", "abstract", ["summary"]) == [
        "RAG",
        "OCR",
        "검색",
    ]
    assert summarize_table(table_client, "a | b") == "표 요약"
