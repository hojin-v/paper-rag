# 통합 설계서 — 온프레미스 논문 분석 RAG 시스템

모든 PDF 논문을 페이지 이미지 기반 OCR·문서 구조 분석으로 처리해 본문·표·단락 요약·대표 키워드
3~5개를 RDB+Vector DB에 저장하고, 자연어 검색 요청에 대해 대표 논문 1편과 연관 논문 1편을 엑셀로
제공하는 온프레미스 시스템. 사전학습 모델 추론이 기본이며 파인튜닝은 실측 합격선 미달 시에만 수행한다.

## 1. 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                        사용자 (Web UI / API)                  │
└──────────────┬──────────────────────────────┬────────────────┘
               │ ① PDF 업로드                  │ ② 자연어 검색
┌──────────────▼──────────────┐  ┌────────────▼────────────────┐
│   수집 파이프라인 (배치)       │  │   검색 서비스 (FastAPI)       │
│  1. PDF 검증 + 전 페이지 렌더 │  │  1. 키워드 추출 (LLM)         │
│  2. 전체 레이아웃 분석 + OCR │  │  2. 정확 매칭 (RDB)          │
│  3. 영역 분류/필터링          │  │  3. 유사 키워드 Top-3        │
│  4. 단락 분리·표 추출         │  │  4. 대표/연관 논문 선정        │
│  5. 정제·요약·키워드 (LLM)   │  │  5. 엑셀 생성                │
│  6~8. 임베딩·연관도 계산      │  │                             │
└──────┬──────────────────────┘  └──────┬──────────────────────┘
       │                                │
┌──────▼────────────────────────────────▼───────────────────────┐
│              저장 계층 (PostgreSQL 16 + pgvector)               │
│   정형 테이블 + 벡터 컬럼/HNSW 인덱스 (단락·키워드·표·논문)        │
└───────────────────────────────────────────────────────────────┘
┌───────────────────────────────────────────────────────────────┐
│  로컬 모델 서빙: Ollama(경량 LLM) · BGE-M3 임베딩(CPU)           │
│  문서 처리: pypdfium2 페이지 렌더링 · PP-StructureV3 전체 OCR    │
└───────────────────────────────────────────────────────────────┘
```

## 2. 기술 스택

| 계층 | 선정 | 근거 |
| --- | --- | --- |
| PDF 페이지 렌더링 | pypdfium2 (Apache 2.0/BSD-3-Clause) | 텍스트 레이어를 본문 추출에 사용하지 않고 모든 페이지를 이미지화. 이전 PyMuPDF(AGPL-3.0)를 라이선스 문제로 교체(2026-07-20) |
| 전체 레이아웃·OCR | PaddleOCR PP-StructureV3 (Apache 2.0) | 사전학습 모델로 디지털·스캔 PDF에 같은 경로 적용, 좌표·신뢰도 보존 |
| 진단 비교 | Docling(MIT)·SimpleTextLayerBackend(pdfplumber, MIT) | 운영 적재 금지, 장애 원인 비교와 단위 테스트에만 사용 |
| 임베딩 | BGE-M3 (sentence-transformers, 1024차원) | 한/영 혼용, CPU 실행. ONNX/int8은 실측 후 별도 최적화 |
| LLM | Qwen2.5-7B-Instruct Q4 (Ollama) | Apache 2.0, 한국어 양호, CPU 구동 가능 |
| 키워드 정규화 | Kiwi 형태소 분석 | 표기 변형 흡수 → 정확 매칭률 확보 |
| 저장 | PostgreSQL 16 + pgvector (HNSW) | ADR-0001 참조 — RDB 조인+벡터 결합 질의가 핵심 |
| API | FastAPI / 엑셀: openpyxl / UI: Streamlit(1차) | Celery worker는 있으나 작업 제출·상태 조회 API 연결은 미완료 |
| 배포 | docker-compose (폐쇄망 반입: 이미지 tar + 모델 파일 번들) | |

라이선스 배제: LayoutLMv3(CC BY-NC), Surya/Marker(가중치 상용 조건부). MinerU(AGPL)는 벤치마크 기준으로만 사용.

> `docs/adr/0002-parsing-stack.md`는 과거 이중 트랙 결정을 기록한 문서다. 2026-07-12 사용자 결정으로
> 디지털 파싱 운영 경로는 폐기됐으며, 현재 운영 기준은 본 설계서의 전체 PDF OCR 단일 경로다.

## 3. 수집 파이프라인 (STEP 1~8)

| STEP | 이름 | 처리 |
| --- | --- | --- |
| 1 | source check | PDF 시그니처·크기 검증 후 pypdfium2로 모든 페이지를 이미지화. 텍스트 레이어는 본문 추출에 사용하지 않음 |
| 2 | layout | 블록 분류: 제목/저자/초록/섹션헤더/본문/표/표캡션/그림/그림캡션/수식/참고문헌/헤더푸터 + 읽기 순서 복원 |
| 3 | filter | 그림·그림캡션·독립 수식·참고문헌 이후·부록 제외. 표는 Markdown으로 직렬화해 포함. 제목/저자/초록은 메타데이터 |
| 4 | paragraph | 섹션 귀속 → 단락 분리. <100자 병합, >1,500자 문장 경계 분할. section_name·paragraph_order 부여 |
| 5 | llm_enrich | 단락당 LLM 1회 호출로 JSON 생성: `{cleaned_text, summary, keywords[1~3], is_topic_relevant}`. 논문 단위로 대표 키워드 3~5개 + 초록 요약. 표는 table_summary 생성. JSON 스키마 강제 |
| 6 | keywords | Kiwi 정규화 + 영한 별칭 매핑 → keywords upsert(frequency 증가), 유사도 ≥0.95면 동의어 병합(keyword_aliases) |
| 7 | embed | 단락(cleaned_text)·키워드·표(title+summary)·논문(제목+초록+키워드) 임베딩 생성·저장 |
| 8 | relate | 논문 임베딩 기준 상위 20편 → paper_relations. `score = 0.6×논문 임베딩 유사도 + 0.3×키워드 자카드 + 0.1×연도 근접도`, relation_reason에 겹치는 키워드 기록 |

- 단계별 상태를 `processing_jobs`에 기록한다. 현재 CLI의 실패 단계 재개와 중간 레이아웃 체크포인트는
  미구현이므로 재실행 시 STEP 1부터 처리한다.
- `is_topic_relevant=false` 단락은 저장하되 검색·엑셀 출력에서 제외

## 4. DB 스키마

임베딩 차원 1024 (BGE-M3). 단락·키워드 임베딩에 HNSW(vector_cosine_ops) 인덱스.

```sql
papers(paper_id PK, title, authors, published_year, journal, abstract,
       abstract_summary, full_text_link, source_file_path,
       paper_embedding VECTOR(1024), status, created_at)

paragraphs(paragraph_id PK, paper_id FK, section_name, paragraph_order,
           original_text, cleaned_text, summary, is_topic_relevant, embedding VECTOR(1024))

keywords(keyword_id PK, keyword UNIQUE(정규화형), display_form, frequency, embedding VECTOR(1024))
keyword_aliases(alias PK, keyword_id FK)
paper_keywords(paper_id, keyword_id, score, PK(paper_id, keyword_id))
tables(table_id PK, paper_id FK, table_title, table_text, table_summary, embedding VECTOR(1024))
paper_relations(source_paper_id, related_paper_id, relation_score, relation_reason, PK(source, related))
processing_jobs(job_id PK, paper_id FK, stage, status, error, started_at, finished_at)
```

`paper_keywords.score` (수집 시 산출): `0.4×제목 등장 + 0.3×초록 등장 + 0.3×본문 등장 빈도(정규화)`

## 5. 검색 서비스

### 5.1 API (2단계 인터랙션)

```
POST /search          {query}          → matched(결과) | suggest(유사 키워드 3개 + session_id)
POST /search/select   {session_id, keyword_id} → 결과
GET  /result/{result_id}/excel         → xlsx 다운로드
```

### 5.2 로직

1. LLM으로 질의 핵심 키워드 추출 → Kiwi 정규화
2. **정확 매칭 우선**: keywords + keyword_aliases 대조. 복수 매칭 시 frequency×질의 등장 순서 가중치 최고 1개 채택
3. 매칭 실패 시: 질의 키워드 임베딩 ↔ keywords.embedding 코사인 유사도 Top 3 (현재 하한 0.5) → 사용자 선택 대기
4. **대표 논문**: `0.5×paper_keywords.score + 0.3×단락 최고 유사도 + 0.1×제목/초록 등장 + 0.1×연도 가중치`
5. **연관 논문**: 사전 계산된 paper_relations에서 최고 score 1편 (실시간 계산 없음 → CPU에서도 수 초 응답)
6. 엑셀 생성 후 result_id로 캐시

### 5.3 엑셀 출력 (6시트)

① 검색 결과 요약(질의·매칭 키워드·매칭 방식·선정 사유·유사도) ② 대표 논문 정보 ③ 대표 논문 단락(번호·섹션·원문·정제문·요약·키워드) ④ 연관 논문 정보(+연관 점수·사유) ⑤ 연관 논문 단락 ⑥ 표 데이터(구분·제목·내용·요약).
열 너비 자동 조정, 헤더 고정, 원문 셀 줄바꿈.

## 6. 정확도 개선·파인튜닝 계획 (게이트 방식 — "측정 없이 튜닝 없다")

자체 평가셋: 우선 한글 5편+영문 5편으로 기능 수용 테스트를 수행하고, 이후 50~100편 층화 샘플
(디지털/스캔, 1단/2단, 한/영, 스캔 품질)로 확대한다. 운영 파서는 PP-StructureV3로 확정하며
Docling/Simple은 비교 진단 결과일 뿐 운영 대체 경로가 아니다.

| 대상 | 지표 | 합격선 | 미달 시 조치 |
| --- | --- | --- | --- |
| 레이아웃 분류 | 클래스별 mAP@0.5 | ≥ 0.85 | PP-DocLayout 파인튜닝 (Label Studio 사전 라벨 주입, 300→1,000페이지, PaddleX, GPU 1장) |
| 읽기 순서 | 단락 순서 일치율 | ≥ 0.95 | 컬럼 감지 후처리 보강 |
| OCR | CER (LLM 정제 후 잔존 기준) | ≤ 3% | PP-OCRv5 한국어 인식 파인튜닝 (합성 5만 라인 + 실측 크롭 3천~1만) |
| 표 구조 | TEDS | ≥ 0.85 | 1차: LLM 후처리 복원 → 2차: SLANeXt 파인튜닝 (표 500개 어노테이션) |
| E2E | 단락 추출 F1 | ≥ 0.90 | 병목 단계 역추적 |

파인튜닝 게이트가 실제로 발동한 뒤에만 능동학습 루프를 추가한다. 후보 방식은 신뢰도 하위 5% 페이지를
검수 큐에 적재하고 사람 교정을 다음 학습 데이터로 축적하는 것이다. 현재 MVP에는 자동 재학습이나
분기 재학습 스케줄이 구현되어 있지 않다.

### LLM 파인튜닝

- 대상 태스크: ① 단락 정제+요약+키워드 JSON ② 질의 키워드 추출 ③ 표 요약 — **프롬프트 최적화 베이스라인이 합격선 미달인 태스크만** 파인튜닝
- 데이터: 교사 모델 Qwen2.5-32B(4bit, 48GB GPU)로 3,000~5,000 단락 증류 + 15% 사람 검수. 골드 평가셋 300~500 단락(전량 수작업, 논문 단위 분리). 질의셋 500~1,000건. 어려운 사례(OCR 노이즈·영한 혼용·비관련 단락) 20% 강제 포함
- 학습: QLoRA(4bit, r=16~32, 2~3 epoch), LLaMA-Factory/Unsloth, 24GB GPU 1장. 3개 태스크 멀티태스크 SFT 통합
- 합격선: JSON 유효율 ≥99%, 키워드 F1 ≥0.75, 요약 환각률 ≤5%(개체 대조), 사람 평가 ≥4.0/5, 질의 추출 재현율 ≥0.85
- 서빙: LoRA 병합 → GGUF Q4_K_M → Ollama 교체 (서빙 경로 무변경). 모델·프롬프트 버전을 DB에 기록
- 임베딩(조건부): 검색 평가셋 recall@10 <0.85 시 BGE-M3를 (키워드↔단락) 양성쌍 + BM25 하드 네거티브로 파인튜닝 → ONNX 재변환 + 전체 벡터 재계산

## 7. 데이터 수집·학습 데이터 계획

| 우선순위 | 수집원 | 방법 |
| --- | --- | --- |
| 1 | 사내/기관 보유 PDF | 일괄 반입 (저작권 리스크 최소) |
| 2 | OAK·KCI 공개 메타데이터 | KCI OAI-PMH로 언어·공개 여부 집계, OAK/KJCI로 원문 링크·CCL 확인 |
| 3 | Europe PMC·CORE·CC 라이선스 arXiv·DOAJ | API/공식 벌크 경로, 논문별 라이선스 필터 |
| 4 | 유료 DB (DBpia 등) | 구독 라이선스 범위 내 수동만, 크롤링 금지 |

- 언어 범위는 먼저 합법적으로 저장 가능한 한글·영문 PDF 수를 실사한 뒤 결정한다. 초기 수용 테스트는
  한글 5편+영문 5편으로 어느 한쪽에 조기 고정하지 않는다.
- 규모: 수용 테스트 10편 → 평가셋 50~100편 → 운영 후보 1,000편 이상. 논문 수는 모델 학습량이 아니라
  검색 커버리지이며, 파인튜닝 데이터는 게이트 발동 시 별도로 선별한다.
- 수집은 DMZ 수집 서버에서 수행 후 내부망 반입. 중복 제거: DOI 우선, 없으면 정규화 제목+제1저자+연도 해시
- 학습/평가 분리는 **논문 단위**(누수 차단), 데이터셋은 DVC 버전 관리
- 검색 종단 평가셋: (질의 → 정답 키워드 → 정답 대표/연관 논문) 트리플 50~100건, 검색 품질 회귀 테스트에 사용

> **저작권 필수 검토(착수 전 외부 의존)**: 원문 단락을 엑셀로 재출력하므로 ① 기관 내부 연구용 이용 범위,
> ② 유료 DB 약관의 TDM·발췌 재제공 허용 여부, ③ 출력물 출처 표기를 법무와 확정해야 한다.

## 8. 문서화 자동화 목표와 현재 상태

| 문서 | 트리거 | 자동화 | 위치 |
| --- | --- | --- | --- |
| ADR | 설계 변경 | 수동 문서 | docs/adr/ | 일부 구현 |
| 개발 일지 | 작업 종료 | 수동 기록 | docs/dev-log/ | 일부 구현 |
| CHANGELOG | 태그 | Conventional Commits + git-cliff | CHANGELOG.md | 미구현 |
| API 문서 | API 실행 | FastAPI OpenAPI | `/docs`, `/redoc` | 구현 |
| DB 스키마 문서 | 마이그레이션 변경 | tbls (실 DB → Markdown ER) | docs/db/ | 미구현 |
| 수집 배치 리포트 | 배치 종료 | 건수·단계별 성공/실패 원인 | docs/reports/ingest/ | 부분 구현 |
| 실험 기록·모델 카드 | 학습 실행 | MLflow + 카드 템플릿 | docs/models/ | 미구현 |
| 주간 보고 | cron | 지표 취합 | docs/weekly/ | 미구현 |

소스 변경+문서 미변경 CI 경고, DVC·MLflow, 모델·프롬프트 버전 자동 기록은 목표 상태이며 현재 구현됐다고
주장하지 않는다. 현재 자동화 근거는 FastAPI 문서와 배치 Markdown 리포트에 한정한다.

## 9. 성능 가설과 측정 항목

- CPU에서도 전체 기능은 실행 가능하지만 현재 머신에서 PP-StructureV3·BGE-M3·Ollama 동시 실행의
  p50/p95 처리 시간을 측정하지 않았다. 기존의 논문당 15~30분 또는 수 초 검색 수치는 보장값으로 쓰지 않는다.
- 측정값: 페이지당 OCR 시간, 논문당 전체 처리 시간, 메모리 최대치, 검색 p50/p95, 동시 작업 수,
  LLM 생성 속도(tokens/s).
- GPU는 대량 처리 또는 게이트 발동 후 파인튜닝에만 검토한다.

## 10. 로드맵

| 단계 | 산출물 |
| --- | --- |
| 현재 | 수집·검수·검색·엑셀 코드 경로, Paddle·BGE-M3·Ollama 실기동, `/ready` 통과, 자동 테스트 |
| Gate A (완료) | Paddle·BGE-M3·Ollama 로컬 모델 준비, 합성 PDF OCR API 확인 |
| Gate B | 한글 5편+영문 5편 실사용 수용 테스트, 처리 시간과 오류 기록 |
| Phase 1 | 50~100편 품질 평가셋, mAP·CER·TEDS·단락 F1·검색 recall 기준선 |
| Phase 2 | 합법적 원문 데이터 실사 후 한글 또는 영문 서비스 범위 결정, 1,000편 이상 적재 |
| Phase 3 | 비동기 작업 API·idempotency·provenance·모델 버전·인증 보강 |
| Phase 4 | 합격선 미달 단계에 한해서만 파인튜닝과 능동학습 검토 |
