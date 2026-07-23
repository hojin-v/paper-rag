# 16. 로깅 · LLM 호출 관찰

에러 발생 시 `docker logs`로 원인을 바로 추적하고, LLM(Ollama) 호출을 프롬프트·응답·
지연시간·토큰 단위로 조회한다. 새 컨테이너·새 인프라 없이 기존 stdout 로깅과 기존
Postgres에 그대로 얹은 것이다(배경: `docs/adr/0003-observability-approach.md`).

```
[API/워커/임베더/UI]
   ├─ logging → stdout ──────────────────► docker logs paper-rag-<서비스>-1
   └─ OllamaClient.generate_json() ──────► llm_calls 테이블(Postgres)
                                                 │
                                                 ▼
                                  GET /observability/llm-calls (조회 뷰어)
```

# 1단계: 로깅 확인

| 설정 | 환경변수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| 로그 레벨 | `PAPERRAG_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |

애플리케이션 로그는 표준 `logging`으로 stdout에 남아 기존 `docker logs` 습관을 그대로
쓴다. 별도 로그 수집기(Fluentd/Loki 등)는 두지 않았다.

```bash
docker logs -f paper-rag-api-1
```

검증:
```bash
# 검색 질의 1회를 던진 뒤 API 로그에 아래 형식의 줄이 찍히면 정상.
# 2026-07-23 10:00:00,000 INFO paperrag.search.api ...
docker logs --tail 20 paper-rag-api-1
```

> API/워커/임베더/UI 4개 진입점 각각에서 `configure_logging()`을 호출한다. 여러 번
> 호출해도 idempotent해 중복 핸들러가 쌓이지 않는다(Streamlit처럼 스크립트가
> 재실행되는 환경 대응).

# 2단계: LLM 호출 관찰 뷰어

| 설정 | 환경변수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| 관찰 기록 여부 | `PAPERRAG_LLM_OBSERVABILITY_ENABLED` | `true` | false면 `llm_calls` 기록을 건너뜀(저장공간/쓰기 부하 절약) |

`db/migrations/0007_llm_calls.sql`이 배포 시 자동 적용된다(수동 조치 불필요, `make deploy`/
CD가 항상 마이그레이션을 실행함).

```bash
open "http://localhost:8000/observability/llm-calls"
```

쿼리 파라미터로 필터링한다.

| 파라미터 | 예시 | 설명 |
| --- | --- | --- |
| `operation` | `?operation=paragraph_enrich` | 아래 표의 라벨 중 하나 |
| `success` | `?success=false` | 실패한 호출만 |
| `limit` | `?limit=50` | 최근 N건(기본 200, 최대 1000) |

기록되는 `operation` 라벨:

| 라벨 | 호출 위치 |
| --- | --- |
| `paragraph_enrich` | 단락 정제/요약/관련성 판정 |
| `keywords` | 논문 대표 키워드 후보 생성 |
| `table_summary` | 표 요약 |
| `abstract_summary` | 초록 요약 |
| `query_keywords` | 검색 질의 키워드 추출 |
| `relevance_explanation` | 검색 결과 관련도 설명 생성 |

검증:
```bash
# API 키를 설정했다면 헤더가 필요하다(PAPERRAG_API_KEY).
curl -s "http://localhost:8000/observability/llm-calls?limit=1" | grep -o "operation" | head -1
```

> 프롬프트/응답은 논문 원문이 포함될 수 있어 그대로 Postgres에 저장된다 — 이미 같은
> DB에 논문 전문·요약이 저장돼 있으므로 새로운 데이터 유출 경로는 아니지만, 별도
> 백업·보존 정책이 필요하면 `llm_calls` 테이블도 함께 고려한다.

## 완료 체크리스트
- [ ] `docker logs paper-rag-api-1`에서 요청 처리 로그가 보인다
- [ ] 검색 질의 1회 후 `/observability/llm-calls`에 해당 호출이 나타난다
- [ ] `?operation=`/`?success=` 필터가 정상 동작한다
- [ ] 의도적으로 Ollama를 잠깐 내려 실패 호출을 만들면 실패 배지와 에러 메시지가 보인다
