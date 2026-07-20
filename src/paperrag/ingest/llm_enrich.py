"""STEP 5 llm_enrich (paragraph enrichment): 단락 정제/요약/키워드, 논문 대표
키워드, 표/초록 요약 생성.

Ollama(Settings.ollama_base_url, Settings.llm_model)에 JSON 스키마를 강제하는
프롬프트로 호출해 결과를 얻는다. 응답이 스키마를 못 지키거나 한자/중국어 문자가
섞이면(_validate_korean_output) 한 번 더 강한 한국어 지시 프롬프트로 재시도하고,
그래도 실패하면 Settings.allow_degraded_results가 true일 때만 원문을 그대로
쓰는 PassthroughEnricher 폴백으로 넘어간다(운영 기본값은 false. 실패 단계로
처리해 조용히 품질이 낮은 결과가 섞이지 않게 한다. docs/guide/04-ingest-pipeline.md
7단계 참고).

이 재시도 1회 + 실패 시 원문 그대로 통과시키는 폴백 경로는
docs/reports/benchmarks/2026-07-04-llm-cpu.md에서 실측 확인됐다: 7B 모델이
900초 타임아웃을 2회 연속 겪은 상황에서도 Passthrough 폴백 덕분에 파이프라인
전체가 막히지 않고 계속 진행됐다. 다만 2026-07-12 실측
(docs/reports/assessments/2026-07-12-two-paper-ocr-evaluation.md)에서는 요약
언어 오염(중국어/벵골어 혼입)과 관련성 오판정 사례도 함께 확인되었으므로, 이
폴백은 "막힘 방지"를 보장할 뿐 품질까지 보장하지는 않는다.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from paperrag.concurrency import heavy_task_slot
from paperrag.config import Settings, get_settings
from paperrag.ingest.models import EnrichedParagraph

# LLM 요약/키워드 출력에서 한자·중국어 문자를 탐지하는 정규식. 2026-07-12 실측에서
# Qwen2.5 7B 출력에 중국어·벵골어가 섞이는 언어 오염이 확인되어 검증에 사용한다.
CJK_IDEOGRAPH_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KOREAN_OUTPUT_RULE = (
    "요약과 키워드는 한국어로 작성하고 한자 또는 중국어 문자를 사용하지 마라. "
    "원문의 영문 기술 용어는 영어로 유지해도 된다."
)
KOREAN_OUTPUT_RETRY = (
    "\n\nThe previous response was rejected. Act as a native Korean academic editor. "
    "Write every Korean word in Hangul and never use Chinese or Japanese characters. "
    "Keep model names and source English technical terms in English. Return JSON only."
)
JSON_SYSTEM_PROMPT = "반드시 유효한 JSON만 반환하라. 스키마: "
KOREAN_JSON_SYSTEM_RULE = (
    " 너는 한국어 논문 편집자다. JSON의 자연어 값은 반드시 한글과 원문의 "
    "영문 기술 용어로만 작성한다. 중국어와 일본어 문자는 절대로 사용하지 않는다."
)

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
{korean_output_rule}

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

PARAGRAPH_KOREAN_RETRY_TEMPLATE = """
You are a native Korean academic editor. Read the source paragraph and return one
concise Korean summary sentence. Write every Korean word in Hangul and never use
Chinese or Japanese characters. Keep model names and source English technical terms
in English. Return exactly this JSON shape:
{{"summary":"한국어 한 문장","keywords":["keyword 1","keyword 2"],"is_topic_relevant":true}}

Source paragraph:
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
    """Ollama /api/chat을 호출해 JSON 응답을 받는 STEP 5 운영용 LLM 클라이언트.

    format="json"으로 스키마를 강제하고, 동일 프롬프트+모델+설정 조합에 대해서는
    파일 캐시(llm_cache_dir)를 사용해 재실행 비용을 줄인다. 2026-07-12 실측에서
    확인된 것처럼 논문 1편 처리 중 장애로 재시작해도 이미 생성한 단락 결과를
    다시 계산하지 않도록 하는 안전장치다.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        """캐시를 먼저 확인하고 없으면 Ollama에 호출해 JSON 딕셔너리를 반환한다.

        timeout은 Settings.llm_timeout_seconds를 사용하며, 이 값을 넘기면 httpx가
        예외를 던져 상위 enrich_paragraph 등의 재시도/폴백 로직으로 넘어간다
        (docs/reports/benchmarks/2026-07-04-llm-cpu.md에서 7B 모델 900초 타임아웃
        실측 근거). 캐시 미스로 실제 Ollama를 호출하는 구간만
        `concurrency.heavy_task_slot`로 감싸 동시 실행 개수를 제한한다 — 캐시
        히트는 자원을 거의 안 쓰므로 세마포어를 거칠 필요가 없다.
        """
        cache_path = self._cache_path(prompt, schema_hint)
        if cache_path is not None and cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return _coerce_json_dict(cached)
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": _system_prompt(self.settings, schema_hint),
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
        with heavy_task_slot(self.settings):
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
        """캐시가 비활성화되어 있으면 None, 아니면 요청 내용 해시로 캐시 파일 경로를 만든다."""
        if not self.settings.llm_cache_enabled:
            return None
        key_payload = json.dumps(
            {
                "model": self.settings.llm_model,
                "temperature": self.settings.llm_temperature,
                "max_output_tokens": self.settings.llm_max_output_tokens,
                "prompt": prompt,
                "schema_hint": schema_hint,
                "system_prompt": _system_prompt(self.settings, schema_hint),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cache_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        return self.settings.llm_cache_dir / f"{cache_key}.json"


def _system_prompt(settings: Settings, schema_hint: str) -> str:
    """JSON 스키마 강제 지시문을 만들고, 설정에 따라 한자/일어 금지 규칙을 덧붙인다."""
    prompt = JSON_SYSTEM_PROMPT + schema_hint
    if settings.llm_forbid_cjk_ideographs:
        prompt += KOREAN_JSON_SYSTEM_RULE
    return prompt


class PassthroughEnricher:
    """LLM 호출 없이 원문을 그대로 통과시키는 개발/폴백용 정제기.

    STEP 5의 두 가지 상황에서 쓰인다: (1) CLI --skip-llm(dry-run 또는
    PAPERRAG_ALLOW_DEGRADED_RESULTS=true 개발 모드)에서 처음부터 이 클래스를 사용,
    (2) 운영 중 실제 LLM 응답이 재시도까지 실패했을 때 allow_degraded_results가
    true이면 마지막 안전장치로 이 클래스의 결과를 대신 사용한다. 두 경우 모두
    요약은 원문 앞부분을 자르는 수준이고 키워드는 규칙 기반 폴백만 제공하므로
    품질을 보장하지 않는다.
    """

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        raise ValueError("PassthroughEnricher는 LLM JSON 생성을 수행하지 않습니다.")

    def enrich_paragraph(self, text: str) -> EnrichedParagraph:
        """원문을 그대로 cleaned_text로 쓰고 앞 200자를 요약으로 삼는 패스스루."""
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
        """제목/초록/요약 텍스트에서 빈도 기반 규칙으로 대표 키워드 후보를 뽑는다."""
        return _fallback_keywords(" ".join([title, abstract, *summaries]))

    def summarize_table(self, table_text: str) -> str:
        """표 본문 앞 200자를 그대로 요약으로 사용하는 패스스루."""
        return table_text.strip()[:200]


def enrich_paragraph(client: LLMClient | PassthroughEnricher, text: str) -> EnrichedParagraph:
    """STEP 5의 핵심 함수: 단락 원문 1개를 LLM 1회 호출로 정제/요약/키워드/관련성 JSON으로 바꾼다.

    client가 PassthroughEnricher면 곧바로 패스스루 결과를 반환한다(개발 모드).
    그 외에는 한국어 출력 규칙을 포함한 프롬프트로 호출하고, 결과에 한자/중국어
    문자가 섞여 있으면(_validate_korean_output) 예외로 간주해 더 강한 한국어
    강제 프롬프트(PARAGRAPH_KOREAN_RETRY_TEMPLATE)로 1회 재시도한다. 재시도까지
    실패하면 allow_degraded_results 설정에 따라 LLMOutputError로 실패 처리하거나
    PassthroughEnricher 결과로 조용히 대체한다 — 이 폴백 경로는
    docs/reports/benchmarks/2026-07-04-llm-cpu.md에서 실제 타임아웃 상황에
    파이프라인이 멈추지 않음을 확인한 안전장치다.
    """
    if isinstance(client, PassthroughEnricher):
        return client.enrich_paragraph(text)

    prompt = PARAGRAPH_PROMPT_TEMPLATE.format(
        text=text,
        korean_output_rule=KOREAN_OUTPUT_RULE,
    )
    for attempt in range(2):
        try:
            data = client.generate_json(prompt, PARAGRAPH_SCHEMA_HINT)
            _validate_korean_output(
                client,
                data.get("summary", ""),
                *data.get("keywords", []),
            )
            data["cleaned_text"] = _normalize_original_text(text)
            return EnrichedParagraph.model_validate(_coerce_json_dict(data))
        except Exception:
            if attempt == 0:
                prompt = PARAGRAPH_KOREAN_RETRY_TEMPLATE.format(text=text)
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
    """STEP 5에서 논문 단위 대표 키워드 3~5개를 생성한다(제목/초록/단락 요약 최대 20개 근거).

    재시도까지 3개 미만이면 실패로 간주해 KOREAN_OUTPUT_RETRY를 덧붙여 다시
    시도하고, 그래도 실패하면 enrich_paragraph와 동일하게 allow_degraded_results
    여부로 예외 또는 규칙 기반 폴백(_fallback_keywords)을 선택한다.
    """
    if isinstance(client, PassthroughEnricher):
        return client.extract_keywords(title, abstract, summaries)

    prompt = KEYWORDS_PROMPT_TEMPLATE.format(
        title=title,
        abstract=abstract,
        summaries="\n".join(summaries[:20]),
    )
    prompt += "\n" + KOREAN_OUTPUT_RULE
    for attempt in range(2):
        try:
            data = _coerce_json_dict(client.generate_json(prompt, KEYWORDS_SCHEMA_HINT))
            keywords = _clean_keywords(data.get("keywords", []))
            _validate_korean_output(client, *keywords)
            if len(keywords) >= 3:
                return keywords[:5]
        except Exception:
            if attempt == 0:
                prompt += KOREAN_OUTPUT_RETRY
                continue
    if not _allow_degraded_result(client):
        raise LLMOutputError("논문 키워드 LLM 응답이 두 번 연속 유효하지 않습니다.")
    return _fallback_keywords(" ".join([title, abstract, *summaries]))


def summarize_table(client: LLMClient | PassthroughEnricher, table_text: str) -> str:
    """STEP 5에서 표 1개의 요약 문장을 생성한다. 실패 시 표 원문 앞 200자로 폴백."""
    if isinstance(client, PassthroughEnricher):
        return client.summarize_table(table_text)

    prompt = TABLE_PROMPT_TEMPLATE.format(table_text=table_text)
    prompt += "\n" + KOREAN_OUTPUT_RULE
    for attempt in range(2):
        try:
            data = _coerce_json_dict(client.generate_json(prompt, TABLE_SCHEMA_HINT))
            summary = str(data.get("summary", "")).strip()
            _validate_korean_output(client, summary)
            if summary:
                return summary
        except Exception:
            if attempt == 0:
                prompt += KOREAN_OUTPUT_RETRY
                continue
    if not _allow_degraded_result(client):
        raise LLMOutputError("표 요약 LLM 응답이 두 번 연속 유효하지 않습니다.")
    return table_text.strip()[:200]


def summarize_abstract(client: LLMClient | PassthroughEnricher, abstract: str) -> str:
    """STEP 5에서 논문 초록을 2문장 이내로 요약한다(papers.abstract_summary 컬럼용).

    초록이 없으면 호출 없이 빈 문자열을 반환한다. 실패 시 원문 앞 500자로 폴백한다.
    """
    text = abstract.strip()
    if not text:
        return ""
    if isinstance(client, PassthroughEnricher):
        return text[:500]
    prompt = ABSTRACT_PROMPT_TEMPLATE.format(abstract=text)
    prompt += "\n" + KOREAN_OUTPUT_RULE
    for attempt in range(2):
        try:
            data = _coerce_json_dict(client.generate_json(prompt, ABSTRACT_SCHEMA_HINT))
            summary = str(data.get("summary", "")).strip()
            _validate_korean_output(client, summary)
            if summary:
                return summary
        except Exception:
            if attempt == 0:
                prompt += KOREAN_OUTPUT_RETRY
                continue
    if not _allow_degraded_result(client):
        raise LLMOutputError("초록 요약 LLM 응답이 두 번 연속 유효하지 않습니다.")
    return text[:500]


def _allow_degraded_result(client: LLMClient) -> bool:
    """운영 저하 결과(패스스루 폴백) 허용 여부. client에 settings가 없으면 안전하게 허용(True)."""
    settings = getattr(client, "settings", None)
    if settings is None:
        return True
    return bool(getattr(settings, "allow_degraded_results", False))


def _validate_korean_output(client: LLMClient, *values: object) -> None:
    """LLM 출력 문자열들에 한자/중국어 문자가 섞였는지 검사해 있으면 예외를 던진다.

    2026-07-12 실측에서 Qwen2.5 7B가 한국어 요약에 중국어·벵골어 문자를 섞어
    내보낸 사례가 확인되어 도입한 검증이다. 이 예외는 enrich_paragraph 등의
    재시도 루프에서 "응답 실패"로 취급되어 강화된 한국어 강제 프롬프트로
    재시도하는 트리거가 된다.
    """
    settings = getattr(client, "settings", None)
    forbid_cjk = (
        True
        if settings is None
        else bool(getattr(settings, "llm_forbid_cjk_ideographs", True))
    )
    if forbid_cjk and any(CJK_IDEOGRAPH_RE.search(str(value)) for value in values):
        raise ValueError("한국어 출력에 한자 또는 중국어 문자가 포함됨")


def _normalize_original_text(text: str) -> str:
    """cleaned_text에 저장하기 전 단락 원문의 줄바꿈/연속 공백을 한 칸으로 축약한다."""
    return " ".join(text.split())


def _coerce_json_dict(value: Any) -> dict[str, Any]:
    """LLM 응답이 dict든 JSON 문자열이든 동일하게 dict로 강제 변환한다."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"JSON object expected, got {type(value).__name__}")


def _clean_keywords(value: Any) -> list[str]:
    """키워드 리스트에서 공백/중복을 제거해 정리한다. 리스트가 아니면 빈 리스트."""
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        keyword = str(item).strip()
        if keyword and keyword not in cleaned:
            cleaned.append(keyword)
    return cleaned


def _fallback_keywords(text: str) -> list[str]:
    """LLM 없이(또는 LLM 실패 시) 영문/한글 토큰 빈도로 대표 키워드 후보를 뽑는 규칙 기반 폴백.

    영문은 3자 이상 알파벳 토큰, 한글은 2자 이상 음절 토큰만 대상으로 하고
    불용어(this/that/논문/연구 등)를 제외한 뒤 빈도 내림차순 상위 5개를 반환한다.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[가-힣]{2,}", text.lower())
    stopwords = {"this", "that", "with", "from", "study", "paper", "논문", "연구", "결과"}
    counts: dict[str, int] = {}
    for token in tokens:
        if token in stopwords:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts, key=lambda token: (-counts[token], token))
    return ranked[:5]
