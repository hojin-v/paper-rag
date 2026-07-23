-- LLM(Ollama) 호출 관찰 기록 — 프롬프트/응답/지연시간/토큰/성공여부를 남긴다.
-- 2026-07-23: 애플리케이션에 로깅/관찰 인프라가 전혀 없어 실서비스 버그 두 건의
-- 원인 파악이 Celery 기본 트레이스백에만 의존했던 것을 계기로 도입한다. Langfuse
-- 셀프호스팅(Postgres+ClickHouse+Redis+MinIO 필요)이나 Prometheus+Grafana 같은
-- 별도 인프라 대신, 이미 쓰고 있는 이 Postgres에 그대로 기록한다(ADR-0001 단일
-- 스토어 원칙과 CLAUDE.md의 "무거운 의존성 지양" 규칙에 부합, docs/adr/0003 참고).
--
-- operation은 호출부(paperrag.ingest.llm_enrich/paperrag.search.service)가 붙이는
-- 라벨(예: "paragraph_enrich", "query_keywords")로, 어떤 기능이 LLM을 얼마나
-- 쓰는지 구분하는 용도다. prompt_tokens/completion_tokens는 Ollama
-- `/api/chat` 응답에 이미 포함되는 prompt_eval_count/eval_count를 그대로 옮긴
-- 것이라 새 요청 파라미터가 필요 없다. context는 document_id/paper_id 같은
-- 호출 시점 부가 정보를 자유 형식으로 담는다(있으면).
CREATE TABLE llm_calls (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT,
    success BOOLEAN NOT NULL,
    error TEXT,
    latency_ms DOUBLE PRECISION,
    cache_hit BOOLEAN NOT NULL DEFAULT false,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    context JSONB
);

CREATE INDEX idx_llm_calls_created_at ON llm_calls (created_at DESC);
CREATE INDEX idx_llm_calls_success ON llm_calls (success);
