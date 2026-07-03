import json
import re
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from paperrag.config import Settings, get_settings
from paperrag.ingest.models import EnrichedParagraph

PARAGRAPH_SCHEMA_HINT = """
{
  "cleaned_text": "string",
  "summary": "string",
  "keywords": ["string", "string", "string"],
  "is_topic_relevant": true
}
""".strip()

PARAGRAPH_PROMPT_TEMPLATE = """
너는 한국어/영어 논문을 정제하는 연구 보조자다.
아래 단락을 원문 의미를 유지하며 정제하고, 1문장 요약, 핵심 키워드 1~3개, 주제 관련 여부를 JSON으로만 반환하라.

예시 입력:
본 연구는 온프레미스 검색 시스템을 제안한다. 실험 결과 검색 정확도가 향상되었다.
예시 출력:
{{"cleaned_text":"본 연구는 온프레미스 검색 시스템을 제안한다. 실험 결과 검색 정확도가 향상되었다.","summary":"온프레미스 검색 시스템이 검색 정확도를 높였다는 내용이다.","keywords":["온프레미스","검색 시스템","검색 정확도"],"is_topic_relevant":true}}

입력 단락:
{text}
""".strip()

KEYWORDS_SCHEMA_HINT = '{"keywords":["string","string","string"]}'
KEYWORDS_PROMPT_TEMPLATE = """
논문 제목, 초록, 단락 요약을 바탕으로 대표 키워드 3~5개를 JSON으로만 반환하라.
반환 형식: {{"keywords":["키워드1","키워드2","키워드3"]}}

제목: {title}
초록: {abstract}
단락 요약:
{summaries}
""".strip()

TABLE_SCHEMA_HINT = '{"summary":"string"}'
TABLE_PROMPT_TEMPLATE = """
아래 논문 표 내용을 한 문장으로 요약하고 JSON으로만 반환하라.
반환 형식: {{"summary":"표 요약"}}

표:
{table_text}
""".strip()


class LLMClient(Protocol):
    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        """Generate JSON matching the schema hint."""


class OllamaClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": "반드시 유효한 JSON만 반환하라. 스키마: " + schema_hint,
                },
                {"role": "user", "content": prompt},
            ],
            "format": "json",
            "stream": False,
        }
        response = httpx.post(
            f"{self.settings.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content", data)
        return _coerce_json_dict(content)


class PassthroughEnricher:
    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        raise ValueError("PassthroughEnricher는 LLM JSON 생성을 수행하지 않습니다.")

    def enrich_paragraph(self, text: str) -> EnrichedParagraph:
        cleaned = text.strip()
        return EnrichedParagraph(
            cleaned_text=cleaned,
            summary=cleaned[:200],
            keywords=[],
            is_topic_relevant=True,
        )

    def extract_keywords(
        self,
        title: str,
        abstract: str,
        summaries: Sequence[str],
    ) -> list[str]:
        return _fallback_keywords(" ".join([title, abstract, *summaries]))

    def summarize_table(self, table_text: str) -> str:
        return table_text.strip()[:200]


def enrich_paragraph(client: LLMClient | PassthroughEnricher, text: str) -> EnrichedParagraph:
    if isinstance(client, PassthroughEnricher):
        return client.enrich_paragraph(text)

    prompt = PARAGRAPH_PROMPT_TEMPLATE.format(text=text)
    for attempt in range(2):
        try:
            data = client.generate_json(prompt, PARAGRAPH_SCHEMA_HINT)
            return EnrichedParagraph.model_validate(_coerce_json_dict(data))
        except Exception:
            if attempt == 0:
                prompt += "\n\n이전 응답은 JSON 파싱 또는 스키마 검증에 실패했다. JSON만 다시 반환하라."
                continue
    return PassthroughEnricher().enrich_paragraph(text)


def extract_paper_keywords(
    client: LLMClient | PassthroughEnricher,
    title: str,
    abstract: str,
    summaries: Sequence[str],
) -> list[str]:
    if isinstance(client, PassthroughEnricher):
        return client.extract_keywords(title, abstract, summaries)

    prompt = KEYWORDS_PROMPT_TEMPLATE.format(
        title=title,
        abstract=abstract,
        summaries="\n".join(summaries[:20]),
    )
    for attempt in range(2):
        try:
            data = _coerce_json_dict(client.generate_json(prompt, KEYWORDS_SCHEMA_HINT))
            keywords = _clean_keywords(data.get("keywords", []))
            if keywords:
                return keywords[:5]
        except Exception:
            if attempt == 0:
                prompt += "\n\n이전 응답은 JSON 파싱 또는 스키마 검증에 실패했다. JSON만 다시 반환하라."
                continue
    return _fallback_keywords(" ".join([title, abstract, *summaries]))


def summarize_table(client: LLMClient | PassthroughEnricher, table_text: str) -> str:
    if isinstance(client, PassthroughEnricher):
        return client.summarize_table(table_text)

    prompt = TABLE_PROMPT_TEMPLATE.format(table_text=table_text)
    for attempt in range(2):
        try:
            data = _coerce_json_dict(client.generate_json(prompt, TABLE_SCHEMA_HINT))
            summary = str(data.get("summary", "")).strip()
            if summary:
                return summary
        except Exception:
            if attempt == 0:
                prompt += "\n\n이전 응답은 JSON 파싱 또는 스키마 검증에 실패했다. JSON만 다시 반환하라."
                continue
    return table_text.strip()[:200]


def _coerce_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"JSON object expected, got {type(value).__name__}")


def _clean_keywords(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        keyword = str(item).strip()
        if keyword and keyword not in cleaned:
            cleaned.append(keyword)
    return cleaned


def _fallback_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[가-힣]{2,}", text.lower())
    stopwords = {"this", "that", "with", "from", "study", "paper", "논문", "연구", "결과"}
    counts: dict[str, int] = {}
    for token in tokens:
        if token in stopwords:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts, key=lambda token: (-counts[token], token))
    return ranked[:5]
