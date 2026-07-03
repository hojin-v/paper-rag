# 요구사항 추적표 (최초 요구 ↔ 구현 ↔ 검증)

상태: ✅ 구현+검증 완료 · 🔶 부분 구현(후속 작업 명시) · 📋 계획 수립 단계

## 1. 수집·데이터 구축

| # | 요구사항 | 구현 위치 | 검증 방법 | 상태 |
| --- | --- | --- | --- | --- |
| 1 | 비정형 PDF 입력, 디지털/스캔 판별 | `ingest/triage.py` | 단위 + 실 PDF E2E | ✅ |
| 2 | OCR·레이아웃 분석 모델 조합 | `ingest/layout/` (simple 구현, docling·paddle 어댑터) | simple만 실검증 | 🔶 docling/PP-OCR 연결은 Phase 0 벤치마크에서 |
| 3 | 제목/저자/본문/표/참고문헌 영역 구분 | `layout/simple_backend.py` (12 블록 타입) | 통합 테스트 | ✅ (휴리스틱 수준, 모델 백엔드로 고도화 예정) |
| 4 | 이미지·그래프·수식·용어설명·참고문헌 제외 | `ingest/filterer.py` + `is_topic_relevant` | 단위 + 통합(참고문헌 미저장 SQL 확인) | ✅ |
| 5 | 본문 단락 분리 (병합·분할·섹션 귀속) | `ingest/paragraphs.py` | 단위 (경계 케이스) | ✅ |
| 6 | 표 데이터 추출 + 표 요약 | `TableDraft` + `summarize_table` + `paper_tables` | 단위(페이크 표) — simple 백엔드는 표 미추출 | 🔶 docling TableFormer 연결 필요 |
| 7 | 단락별 원문/정제문/요약/키워드 | `ingest/llm_enrich.py` (Ollama JSON 강제, 폴백 포함) | 단위+통합(Scripted LLM) — 실 LLM 품질 미측정 | 🔶 Ollama 모델 pull 후 품질 확인 |
| 8 | 논문 전체 키워드 3~5개 | `extract_paper_keywords` + 정규화(`keywords.py`) | 통합 | ✅ |
| 9 | RDB 저장 구조 (papers~paper_relations) | `db/migrations/0001_init.sql` (9테이블) | **실 DB 마이그레이션+적재** | ✅ |
| 10 | Vector DB 임베딩 저장 (단락·키워드·표) | pgvector VECTOR(1024) + HNSW 3개 | **실 DB 코사인 검색** — 실 임베딩 모델(BGE-M3)은 미연결 | 🔶 |

## 2. 검색 사용자 시나리오

| # | 요구사항 | 구현 위치 | 검증 방법 | 상태 |
| --- | --- | --- | --- | --- |
| 11 | 자연어 질의 → 핵심 키워드 추출 | `search/service.py` (LLM + 폴백) | 통합 (요구서 예시 질의 그대로 사용) | ✅ |
| 12 | 키워드 **정확 매칭 우선** (별칭 포함) | `find_keyword_exact` + `keyword_aliases` | 통합 시나리오 1 | ✅ |
| 13 | 대표 논문 1 + 연관 논문 1 반환 | 점수식(`0.5/0.3/0.1/0.1`) + `paper_relations` 사전 계산 | 단위(점수 수치) + 통합 | ✅ |
| 14 | 미매칭 시 코사인 Top-3 제시 → 사용자 선택 | `suggest`(하한 0.6) + 세션 + `/search/select` | 통합 시나리오 2 (유사어 "예지보전"→"예측 유지보수") | ✅ |
| 15 | 엑셀 출력 — 전문이 아닌 단락 원문/요약/표 요약/메타, 6시트 | `search/excel.py` | 통합: 생성 파일 재로드해 시트·셀 검증 | ✅ |
| 16 | 선택 인터랙션 UI | `ui/app.py` (Streamlit) + `ui/client.py` | ApiClient 오프라인 테스트 — 화면 수동 확인은 후속 | 🔶 |

## 3. 온프레미스·운영

| # | 요구사항 | 구현 위치 | 검증 방법 | 상태 |
| --- | --- | --- | --- | --- |
| 17 | CPU 우선·경량 모델 (Ollama Q4, BGE-M3 ONNX) | `docker-compose.yml` + Settings | 스택 정의 완료 — Docker WSL 통합 후 실기동 | 🔶 |
| 18 | 파인튜닝(LoRA)·프롬프트 최적화 계획 | `DESIGN.md` §6 (게이트 방식) | 계획 수립 (트리거 지표 정의) | 📋 |
| 19 | 대량 배치 파이프라인 | CLI 배치 + 실패 단계 재시작(`processing_jobs`) + worker profile | dry-run + 통합 — celery 병렬화는 후속 | ✅ |
| 20 | 구현 과정 문서화 자동화 | 배치 리포트 자동 생성(`docs/reports/ingest/`), 가이드 01~07, dev-log | 리포트 자동 생성 실확인 — CI 연동은 후속 | 🔶 |

## 잔여 작업 (우선순위순)

1. Docker Desktop WSL 통합 → compose 스택 실기동 (`make up && make migrate`) — guide 02·03
2. Ollama 모델 pull → `--skip-llm` 없이 실 LLM 정제·요약·키워드 품질 측정 (#7)
3. BGE-M3 임베딩 서버 모듈 + compose `embedder` 서비스 (#10)
4. 실제 논문 PDF 10편으로 simple vs docling 백엔드 벤치마크, 표 추출 연결 (#2, #6)
5. Streamlit 화면 수동 검증 및 스크린샷 문서화 (#16)
