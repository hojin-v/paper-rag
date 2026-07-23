# ADR-0003: 로깅·LLM 호출 관찰 — 기존 Postgres에 기록 + 자체 뷰어

- 상태: 승인 (2026-07-23)

## 배경

2026-07-23 실서비스 버그 두 건(Celery daemonic 프로세스 제약, Redis 소켓 타임아웃
불일치)의 원인을 파악하는 동안, `docker logs`로 새어나온 Celery의 기본 트레이스백
출력에만 의존해야 했다 — `src/paperrag/` 전체를 감사한 결과 `logging` 모듈을 쓰는
곳이 단 한 곳도 없었고(예외 핸들러도 없음), LLM 호출(프롬프트·응답·지연시간·토큰·
실패)을 관찰할 수단도 전혀 없었다.

에러를 제대로 추적할 수 있는 로깅과, LLM 호출을 볼 수 있는 관찰 수단이 필요했다.
단, Prometheus+Grafana나 Langfuse 셀프호스팅처럼 무거운 인프라는 원하지 않는다는
제약이 있었다(맥북 한 대에 이미 앱 4개 + postgres/redis/ollama 컨테이너가 떠 있는
상태).

## 결정

1. **로깅**: 표준 라이브러리 `logging`만 사용, stdout으로 출력(`docker logs` 그대로
   유지). 새 외부 의존성 없음.
2. **LLM 호출 관찰**: 새 Postgres 테이블(`llm_calls`)에 호출마다 기록하고, 기존
   `review/viewer.py`와 같은 스타일(서버에서 조립하는 단일 HTML)의 조회 페이지
   (`GET /observability/llm-calls`)를 추가한다.

## 배제

| 후보 | 배제 사유 |
| --- | --- |
| Langfuse 셀프호스팅 | Postgres+ClickHouse+Redis+MinIO(S3 호환)가 다 필요해, 실제로는 Prometheus+Grafana보다 컨테이너 수가 더 늘어난다. "단순한 관찰"이라는 목표와 어긋남 |
| Langfuse 클라우드(무료 티어) | 인프라 부담은 0에 가깝지만, 논문 내용이 담긴 프롬프트·응답이 외부 클라우드로 나간다 — 이 프로젝트의 온프레미스 설계 원칙과 정면으로 어긋남 |
| Prometheus + Grafana | 메트릭 수집·시계열 저장·대시보드용 컨테이너를 최소 2개 더 추가해야 함. 이 시스템 규모(단일 서버, 단일 사용자)에 비해 과함 |

## 근거

1. ADR-0001(pgvector 단일 스토어 원칙)과 CLAUDE.md의 "무거운 의존성 지양" 규칙에
   가장 잘 맞는다 — 새 컨테이너·새 DB 없이 이미 쓰는 Postgres에 테이블 하나만
   추가한다.
2. 로깅은 stdout 출력이라 기존 `docker logs` 운영 습관을 그대로 유지한다 — 별도
   로그 수집기(Fluentd/Loki 등)를 배우거나 운영할 필요가 없다.
3. `llm_calls` 조회 뷰어는 review 뷰어와 동일한 패턴(서버 조립 HTML)이라 새 프론트엔드
   빌드 파이프라인이 필요 없고, 유지보수 부담이 기존 코드베이스와 일관되다.
4. Ollama 응답에 이미 포함된 `prompt_eval_count`/`eval_count`를 그대로 옮기므로
   토큰 집계를 위한 별도 계측이 필요 없다.

## 영향

- `Settings.llm_observability_enabled`(기본 true)로 기록을 끌 수 있다 — 저장공간/
  쓰기 부하가 걱정되는 경우의 탈출구.
- 세션 그룹핑, 비용 계산, 알림 같은 Langfuse/Grafana류의 고급 기능은 없다 — 필요해
  지면 그때 재검토한다(현재 규모에서는 과함).
- 기록 실패는 LLM 호출 자체를 막지 않는다(`concurrency.py`의 가용성 우선 원칙과
  동일) — 관찰은 최적화이지 필수 안전장치가 아니다.
