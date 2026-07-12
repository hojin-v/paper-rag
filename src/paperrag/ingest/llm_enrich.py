import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from paperrag.config import Settings, get_settings
from paperrag.ingest.models import EnrichedParagraph

PARAGRAPH_SCHEMA_HINT = """
{
  "summary": "string",
  "keywords": ["string", "string", "string"],
  "is_topic_relevant": true
}
""".strip()

PARAGRAPH_PROMPT_TEMPLATE = """
너는 한국어/영어 논문을 정제하는 연구 보조자다.
아래 단락의 1문장 요약, 핵심 키워드 1~3개, 연구 본문 관련 여부를 JSON으로만 반환하라.
저자명, 소속, 이메일, 머리말, 꼬리말, 참고문헌만 있는 단락은
is_topic_relevant=false이고 keywords=[]이다. 입력에 없는 내용을 추가하지 마라.

예시 입력:
본 연구는 온프레미스 검색 시스템을 제안한다. 실험 결과 검색 정확도가 향상되었다.
예시 출력:
{{"summary":"온프레미스 검색 시스템이 검색 정확도를 높였다는 내용이다.","keywords":["온프레미스","검색 시스템","검색 정확도"],"is_topic_relevant":true}}

예시 입력:
John Doe, Example University, john@example.com
예시 출력:
{{"summary":"저자와 소속 정보이다.","keywords":[],"is_topic_relevant":false}}

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
ABSTRACT_SCHEMA_HINT = '{"summary":"string"}'
TABLE_PROMPT_TEMPLATE = """
아래 논문 표 내용을 한 문장으로 요약하고 JSON으로만 반환하라.
반환 형식: {{"summary":"표 요약"}}

표:
{table_text}
""".strip()

ABSTRACT_PROMPT_TEMPLATE = """
아래 논문 초록을 원문에 없는 내용을 추가하지 말고 2문장 이내로 요약해 JSON으로만 반환하라.
반환 형식: {{"summary":"초록 요약"}}

초록:
{abstract}
""".strip()


class LLMClient(Protocol):
    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        """Generate JSON matching the schema hint."""


class LLMOutputError(RuntimeError):
    """LLM 응답을 검증하지 못해 운영 결과를 만들 수 없음."""


class OllamaClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        cache_path = self._cache_path(prompt, schema_hint)
        if cache_path is not None and cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return _coerce_json_dict(cached)
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
            "options": {
                "temperature": self.settings.llm_temperature,
                "num_predict": self.settings.llm_max_output_tokens,
            },
        }
        response = httpx.post(
            f"{self.settings.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content", data)
        result = _coerce_json_dict(content)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = cache_path.with_suffix(".json.part")
            temporary_path.write_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary_path.replace(cache_path)
        return result

    def _cache_path(self, prompt: str, schema_hint: str) -> Path | None:
        if not self.settings.llm_cache_enabled:
            return None
        key_payload = json.dumps(
            {
                "model": self.settings.llm_model,
                "temperature": self.settings.llm_temperature,
                "max_output_tokens": self.settings.llm_max_output_tokens,
                "prompt": prompt,
                "schema_hint": schema_hint,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cache_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        return self.settings.llm_cache_dir / f"{cache_key}.json"


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
            data["cleaned_text"] = _normalize_original_text(text)
            return EnrichedParagraph.model_validate(_coerce_json_dict(data))
        except Exception:
            if attempt == 0:
                prompt += "\n\n이전 응답은 JSON 파싱 또는 스키마 검증에 실패했다. JSON만 다시 반환하라."
                continue
    if not _allow_degraded_result(client):
        raise LLMOutputError("단락 정제 LLM 응답이 두 번 연속 유효하지 않습니다.")
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
            if len(keywords) >= 3:
                return keywords[:5]
        except Exception:
            if attempt == 0:
                prompt += "\n\n이전 응답은 JSON 파싱 또는 스키마 검증에 실패했다. JSON만 다시 반환하라."
                continue
    if not _allow_degraded_result(client):
        raise LLMOutputError("논문 키워드 LLM 응답이 두 번 연속 유효하지 않습니다.")
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
    if not _allow_degraded_result(client):
        raise LLMOutputError("표 요약 LLM 응답이 두 번 연속 유효하지 않습니다.")
    return table_text.strip()[:200]


def summarize_abstract(client: LLMClient | PassthroughEnricher, abstract: str) -> str:
    text = abstract.strip()
    if not text:
        return ""
    if isinstance(client, PassthroughEnricher):
        return text[:500]
    prompt = ABSTRACT_PROMPT_TEMPLATE.format(abstract=text)
    for attempt in range(2):
        try:
            data = _coerce_json_dict(client.generate_json(prompt, ABSTRACT_SCHEMA_HINT))
            summary = str(data.get("summary", "")).strip()
            if summary:
                return summary
        except Exception:
            if attempt == 0:
                prompt += "\n\n이전 응답은 유효한 JSON이 아니었다. JSON만 다시 반환하라."
                continue
    if not _allow_degraded_result(client):
        raise LLMOutputError("초록 요약 LLM 응답이 두 번 연속 유효하지 않습니다.")
    return text[:500]


def _allow_degraded_result(client: LLMClient) -> bool:
    settings = getattr(client, "settings", None)
    if settings is None:
        return True
    return bool(getattr(settings, "allow_degraded_results", False))


def _normalize_original_text(text: str) -> str:
    return " ".join(text.split())


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
