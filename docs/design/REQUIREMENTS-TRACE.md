# 요구사항 추적표 (최초 요구 ↔ 구현 ↔ 검증)

상태: ✅ 코드 구현+현재 검증 완료 · 🔶 부분 구현 또는 실모델 검증 필요 · 📋 계획만 있음

## 1. 수집·데이터 구축

| # | 요구사항 | 구현 위치 | 검증 방법 | 상태 |
| --- | --- | --- | --- | --- |
| 1 | 모든 비정형 PDF를 전 페이지 OCR 처리 | `layout/paddle_backend.py`, `review/service.py` | 전 페이지 렌더링·paddle 강제·진단 backend 차단 테스트 | ✅ |
| 2 | 사전학습 OCR·레이아웃 모델 조합 | `ingest/layout/paddle_backend.py` | PP-StructureV3 결과 매핑·좌표 환산 테스트, 합성 PDF 실제 API 업로드 | 🔶 실모델 실행 확인, 실제 논문 평가셋 미검증 |
| 3 | 제목/저자/본문/표/참고문헌 영역 구분 | `paddle_backend.LABEL_MAP` (12 블록 타입) | 합성 결과 매핑 테스트 | 🔶 실모델 클래스별 mAP 미측정 |
| 4 | 이미지·그래프·수식·용어설명·참고문헌 제외 | `ingest/filterer.py` + `is_topic_relevant` | 단위 + 통합(참고문헌 미저장 SQL 확인) | ✅ |
| 5 | 본문 단락 분리 (병합·분할·섹션 귀속) | `ingest/paragraphs.py` | 단위 (경계 케이스) | ✅ |
| 6 | 표 데이터 추출 + 표 요약 | 표 영역 좌표 OCR 기준선 + `TableDraft` + `summarize_table` + `paper_tables` | 좌표 토큰 행 정렬·LLM 계약 테스트 | 🔶 병합 셀 구조 복원·TEDS 미검증 |
| 7 | 단락별 원문/정제문/요약/키워드 | `ingest/llm_enrich.py` (Ollama JSON 강제) | 재시도·스키마·운영 폴백 차단, 지정 7B Q4 JSON 응답 | 🔶 실제 논문 배치 품질 미측정 |
| 8 | 논문 전체 키워드 3~5개 | `extract_paper_keywords` + 정규화(`keywords.py`) | 통합 | ✅ |
| 9 | RDB 저장 구조 (papers~paper_relations) | `db/migrations/0001_init.sql` (9테이블) | **실 DB 마이그레이션+적재** | ✅ |
| 10 | Vector DB 임베딩 저장 (단락·키워드·표) | pgvector VECTOR(1024) + HNSW 3개 + BGE-M3 서버 | 오프라인 BGE-M3 실제 추론 1024차원, HTTP health, pgvector 확장 확인 | 🔶 실제 논문 적재·검색 수용 테스트 필요 |

## 2. 검색 사용자 시나리오

| # | 요구사항 | 구현 위치 | 검증 방법 | 상태 |
| --- | --- | --- | --- | --- |
| 11 | 자연어 질의 → 핵심 키워드 추출 | `search/service.py` (LLM JSON, 운영 폴백 차단) | 목 LLM 통합 테스트 | 🔶 지정 실 LLM 품질 미측정 |
| 12 | 키워드 **정확 매칭 우선** (별칭 포함) | `find_keyword_exact` + `keyword_aliases` | 통합 시나리오 1 | ✅ |
| 13 | 대표 논문 1 + 연관 논문 1 반환 | 점수식(`0.5/0.3/0.1/0.1`) + `paper_relations` 사전 계산 | 단위(점수 수치) + 통합 | ✅ |
| 14 | 미매칭 시 코사인 Top-3 제시 → 사용자 선택 | `suggest`(현재 하한 0.5) + 세션 + `/search/select` | 통합 시나리오 2 (유사어 "예지보전"→"예측 유지보수") | ✅ |
| 15 | 엑셀 출력 — 전문이 아닌 단락 원문/요약/표 요약/메타, 6시트 | `search/excel.py` | 통합: 생성 파일 재로드해 시트·셀 검증 | ✅ |
| 16 | 선택 인터랙션 UI | `ui/app.py` (Streamlit) + `ui/client.py` + 데모 서버(`scripts/demo_server.py`) | Streamlit 실기동 + 데모 API로 전 플로우(매칭/제안/선택/엑셀) HTTP 검증 | ✅ |

## 3. 온프레미스·운영

| # | 요구사항 | 구현 위치 | 검증 방법 | 상태 |
| --- | --- | --- | --- | --- |
| 17 | CPU 우선·경량 모델 (Paddle mobile, Ollama Q4, BGE-M3) | `docker-compose.yml` + Settings + `/ready` | Paddle 3.3 CPU OCR, PostgreSQL·Redis·Ollama, BGE-M3 CPU 추론 | ✅ 현재 머신 `/ready` 통과; 처리량 합격은 별도 측정 |
| 18 | 파인튜닝(LoRA)·프롬프트 최적화 계획 | `DESIGN.md` §6 (게이트 방식) | 계획 수립 (트리거 지표 정의) | 📋 |
| 19 | 대량 배치 파이프라인 | CLI 배치 + `processing_jobs` + worker profile | CLI·단계 기록 테스트 | 🔶 실패 단계 재개·idempotency·UI/worker 비동기 연결 미구현 |
| 20 | 구현 과정 문서화 | 배치 리포트, 가이드, readiness 점검 보고서 | 생성 파일과 문서 검토 | 🔶 CI·DVC·MLflow·자동 모델 버전 기록 미구현 |

## 잔여 작업 (우선순위순)

1. 한글 5편+영문 5편 수용 테스트 — 2단 조판·스캔본 포함 (#2, #3, #6)
2. 50~100편 평가셋으로 mAP/CER/TEDS/E2E F1·검색 recall 측정 (#2, #7, #10)
3. 복잡 표가 기준선에 미달하면 정밀 셀 검출 모델 도입 또는 파인튜닝 결정 (#6)
4. 표 캡션↔표 본체 연결(table_title 매핑) 개선 (#6)
5. Celery 작업 제출·상태 조회·UI polling과 실패 단계 재개 구현 (#19)
6. 논문 출처·라이선스·checksum과 모델·프롬프트 버전 저장 (#20)
