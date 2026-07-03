# 통합 설계서 — 온프레미스 논문 분석 RAG 시스템

비정형 PDF 논문을 OCR·문서 구조 분석으로 처리해 본문·표·단락 요약·키워드를 RDB+Vector DB에 저장하고,
자연어 검색 요청에 대해 대표 논문 1편과 연관 논문 1편을 엑셀로 제공하는 온프레미스 시스템.

## 1. 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                        사용자 (Web UI / API)                  │
└──────────────┬──────────────────────────────┬────────────────┘
               │ ① PDF 업로드                  │ ② 자연어 검색
┌──────────────▼──────────────┐  ┌────────────▼────────────────┐
│   수집 파이프라인 (배치)       │  │   검색 서비스 (FastAPI)       │
│  1. PDF 유형 판별 (triage)   │  │  1. 키워드 추출 (LLM)         │
│  2. 레이아웃 분석 + OCR      │  │  2. 정확 매칭 (RDB)          │
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
│  로컬 모델 서빙: Ollama(경량 LLM) · BGE-M3 임베딩(ONNX, CPU)     │
│  문서 파싱: Docling(디지털 PDF) · PP-StructureV3(스캔/파인튜닝)  │
└───────────────────────────────────────────────────────────────┘
```

## 2. 기술 스택

| 계층 | 선정 | 근거 |
| --- | --- | --- |
| 디지털 PDF 파싱 | Docling (MIT) | CPU에서 레이아웃·읽기 순서·표(TableFormer) 일괄 처리 |
| 스캔 PDF + 파인튜닝 트랙 | PaddleOCR PP-StructureV3 (Apache 2.0) | 한국어 OCR 강함, PaddleX로 레이아웃·OCR·표 파인튜닝 공식 지원 |
| 텍스트 레이어 추출 | PyMuPDF | 디지털 PDF 고속 추출 (triage 포함) |
| 임베딩 | BGE-M3 (ONNX int8, 1024차원) | 한/영 혼용 강함, CPU 실행 |
| LLM | Qwen2.5-7B-Instruct Q4 (Ollama) | Apache 2.0, 한국어 양호, CPU 구동 가능 |
| 키워드 정규화 | Kiwi 형태소 분석 | 표기 변형 흡수 → 정확 매칭률 확보 |
| 저장 | PostgreSQL 16 + pgvector (HNSW) | ADR-0001 참조 — RDB 조인+벡터 결합 질의가 핵심 |
| API | FastAPI / 배치: Celery+Redis / 엑셀: openpyxl / UI: Streamlit(1차) | |
| 배포 | docker-compose (폐쇄망 반입: 이미지 tar + 모델 파일 번들) | |

라이선스 배제: LayoutLMv3(CC BY-NC), Surya/Marker(가중치 상용 조건부). MinerU(AGPL)는 벤치마크 기준으로만 사용.

## 3. 수집 파이프라인 (STEP 1~8)

| STEP | 이름 | 처리 |
| --- | --- | --- |
| 1 | triage | PyMuPDF로 텍스트 레이어 커버리지 측정. ≥80% → 디지털 경로(OCR 생략), 미만 → 스캔 경로(OCR) |
| 2 | layout | 블록 분류: 제목/저자/초록/섹션헤더/본문/표/표캡션/그림/그림캡션/수식/참고문헌/헤더푸터 + 읽기 순서 복원 |
| 3 | filter | 그림·그림캡션·독립 수식·참고문헌 이후·부록 제외. 표는 Markdown으로 직렬화해 포함. 제목/저자/초록은 메타데이터 |
| 4 | paragraph | 섹션 귀속 → 단락 분리. <100자 병합, >1,500자 문장 경계 분할. section_name·paragraph_order 부여 |
| 5 | llm_enrich | 단락당 LLM 1회 호출로 JSON 생성: `{cleaned_text, summary, keywords[1~3], is_topic_relevant}`. 논문 단위로 대표 키워드 3~5개 + 초록 요약. 표는 table_summary 생성. JSON 스키마 강제 |
| 6 | keywords | Kiwi 정규화 + 영한 별칭 매핑 → keywords upsert(frequency 증가), 유사도 ≥0.95면 동의어 병합(keyword_aliases) |
| 7 | embed | 단락(cleaned_text)·키워드·표(title+summary)·논문(제목+초록+키워드) 임베딩 생성·저장 |
| 8 | relate | 논문 임베딩 기준 상위 20편 → paper_relations. `score = 0.6×논문 임베딩 유사도 + 0.3×키워드 자카드 + 0.1×연도 근접도`, relation_reason에 겹치는 키워드 기록 |

- 단계별 상태를 `processing_jobs`에 기록, 실패 시 해당 단계부터 재시작 (중간 산출물 레이아웃 JSON 보존)
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
3. 매칭 실패 시: 질의 키워드 임베딩 ↔ keywords.embedding 코사인 유사도 Top 3 (하한 0.6) → 사용자 선택 대기
4. **대표 논문**: `0.5×paper_keywords.score + 0.3×단락 최고 유사도 + 0.1×제목/초록 등장 + 0.1×연도 가중치`
5. **연관 논문**: 사전 계산된 paper_relations에서 최고 score 1편 (실시간 계산 없음 → CPU에서도 수 초 응답)
6. 엑셀 생성 후 result_id로 캐시

### 5.3 엑셀 출력 (6시트)

① 검색 결과 요약(질의·매칭 키워드·매칭 방식·선정 사유·유사도) ② 대표 논문 정보 ③ 대표 논문 단락(번호·섹션·원문·정제문·요약·키워드) ④ 연관 논문 정보(+연관 점수·사유) ⑤ 연관 논문 단락 ⑥ 표 데이터(구분·제목·내용·요약).
열 너비 자동 조정, 헤더 고정, 원문 셀 줄바꿈.

## 6. 정확도 개선·파인튜닝 계획 (게이트 방식 — "측정 없이 튜닝 없다")

자체 평가셋: 50~100편 층화 샘플(디지털/스캔, 1단/2단, 한/영, 스캔 품질). Phase 0에서 Docling/PP-StructureV3/MinerU 3자 벤치마크로 파서 최종 확정.

| 대상 | 지표 | 합격선 | 미달 시 조치 |
| --- | --- | --- | --- |
| 레이아웃 분류 | 클래스별 mAP@0.5 | ≥ 0.85 | PP-DocLayout 파인튜닝 (Label Studio 사전 라벨 주입, 300→1,000페이지, PaddleX, GPU 1장) |
| 읽기 순서 | 단락 순서 일치율 | ≥ 0.95 | 컬럼 감지 후처리 보강 |
| OCR | CER (LLM 정제 후 잔존 기준) | ≤ 3% | PP-OCRv5 한국어 인식 파인튜닝 (합성 5만 라인 + 실측 크롭 3천~1만) |
| 표 구조 | TEDS | ≥ 0.85 | 1차: LLM 후처리 복원 → 2차: SLANeXt 파인튜닝 (표 500개 어노테이션) |
| E2E | 단락 추출 F1 | ≥ 0.90 | 병목 단계 역추적 |

운영 단계 능동학습 루프: 신뢰도 하위 5% 페이지 자동 검수 큐 적재 → 사람 교정 → 다음 파인튜닝 라운드 학습 데이터로 축적, 분기 1회 재학습. 어노테이션 품질: 2인 교차 10% 샘플, Cohen's κ ≥ 0.8.

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
| 2 | KoreaScience·KCI 오픈액세스 | KCI/ScienceON 오픈 API |
| 3 | arXiv·DOAJ | API |
| 4 | 유료 DB (DBpia 등) | 구독 라이선스 범위 내 수동만, 크롤링 금지 |

- 규모: Phase 0 100편(벤치마크·평가셋) → Phase 1 1,000편(학습 데이터 원천) → Phase 2 10,000편+(운영)
- 수집은 DMZ 수집 서버에서 수행 후 내부망 반입. 중복 제거: DOI 우선, 없으면 정규화 제목+제1저자+연도 해시
- 학습/평가 분리는 **논문 단위**(누수 차단), 데이터셋은 DVC 버전 관리
- 검색 종단 평가셋: (질의 → 정답 키워드 → 정답 대표/연관 논문) 트리플 50~100건, 검색 품질 회귀 테스트에 사용

> **저작권 필수 검토(착수 전 외부 의존)**: 원문 단락을 엑셀로 재출력하므로 ① 기관 내부 연구용 이용 범위,
> ② 유료 DB 약관의 TDM·발췌 재제공 허용 여부, ③ 출력물 출처 표기를 법무와 확정해야 한다.

## 8. 문서화 자동화

| 문서 | 트리거 | 자동화 | 위치 |
| --- | --- | --- | --- |
| ADR | 설계 변경 PR | 템플릿 강제 + LLM 초안 | docs/adr/ |
| 개발 일지 | 커밋/merge | diff 요약 (로컬 LLM) | docs/dev-log/ |
| CHANGELOG | 태그 | Conventional Commits + git-cliff | CHANGELOG.md |
| API 문서 | push | FastAPI OpenAPI → Redoc | 문서 사이트 |
| DB 스키마 문서 | 마이그레이션 merge | tbls (실 DB → Markdown ER) | docs/db/ |
| 수집 배치 리포트 | 배치 종료 | 파이프라인이 자동 생성 (건수·단계별 성공률·품질 지표·실패 원인) | docs/reports/ingest/ |
| 실험 기록·모델 카드 | 학습 실행 | MLflow 자동 로깅 + 카드 템플릿 자동 채움 | docs/models/ |
| 주간 보고 | cron | 로컬 LLM이 커밋+리포트+지표 취합 | docs/weekly/ |

부패 방지: 소스 변경+문서 미변경 시 CI 경고, 자동 생성 초안도 리뷰 통과 필수, 모든 리포트에 커밋 해시·데이터(DVC)·프롬프트·모델 버전 자동 기입.

## 9. 성능 기준선

- CPU 16코어/64GB 1대: 수집은 야간 배치(논문 1편 15~30분), 검색은 LLM 1회 호출뿐 → 수 초 응답
- GPU 1장(24GB) 추가 시 논문당 1~2분 — 대량 구축 단계에만 GPU, 운영은 CPU 전환 가능
- 파인튜닝은 학습 시에만 GPU 필요 (§6)

## 10. 로드맵

| 단계 | 산출물 |
| --- | --- |
| Phase 0 (1~2주) | 평가셋 구축 + 파서 3자 벤치마크, 문서화 인프라(Git 서버·CI·MkDocs·MLflow·DVC) |
| Phase 1 (3주) | 수집 파이프라인 STEP 1~8 완성, 100→1,000편 적재 |
| Phase 2 (2주) | 검색 서비스 + 엑셀 출력 |
| Phase 3 (2주) | Streamlit UI, E2E 검증 |
| Phase 4 (지속) | 품질 고도화 — 게이트 발동 시 파인튜닝(§6), 능동학습 루프 |
