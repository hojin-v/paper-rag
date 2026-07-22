# 13. 코드베이스 읽기 가이드 — 전체 흐름과 모듈별 참고

발표 준비를 위해 코드를 처음부터 끝까지 정확히 이해할 수 있도록, 읽는 순서와 각 모듈의 역할·핵심
포인트·완성도를 정리한다. 소스 코드 자체에는 이 문서와 별도로 상세한 한국어 주석이 추가되어 있으니
(모듈 docstring + 함수별 설명), 이 문서는 "어떤 순서로, 왜 그렇게 짜여 있는지"를 잡아 주는 지도 역할만
한다 — 세부 구현은 각 파일을 열어 주석과 함께 읽는다.

```text
paper-rag 실행 단위
├─ FastAPI 검색·검수 API   (paperrag.search.api / paperrag.review.api)
├─ BGE-M3 임베딩 HTTP 서버  (paperrag.embed.server)
├─ Celery worker           (paperrag.worker.app)
└─ Streamlit UI            (paperrag.ui.app)

수집 파이프라인 STEP 1~8 (paperrag.ingest.pipeline.IngestPipeline)
1 source check → 2 layout → 3 filter → 4 paragraph → 5 llm_enrich
→ 6 keywords → 7 embed → 8 relate
```

# 0단계: 코드보다 먼저 읽을 설계 문서

코드만 봐서는 "왜 이렇게 짰는지"가 안 보이는 결정이 많다. 아래 문서로 배경을 먼저 잡는다.

| 문서 | 왜 먼저 읽어야 하는지 |
| --- | --- |
| `docs/design/DESIGN.md` | 전체 아키텍처, STEP 1~8 정의, DB 스키마, 검색 점수식 — 이 문서의 뼈대 |
| `docs/adr/0001-pgvector-single-store.md` | 왜 별도 벡터DB 없이 PostgreSQL+pgvector 하나로 갔는지 |
| `docs/adr/0002-parsing-stack.md` | 왜 Docling·PP-StructureV3 이중 트랙이었다가 Paddle 단일 경로로 굳어졌는지 |
| `docs/design/REQUIREMENTS-TRACE.md` | 요구사항 ↔ 구현 ↔ 검증 상태 추적표 |
| `docs/guide/10-production-readiness.md` | 지금 "안 되는 것"을 숨기지 않고 정리한 문서 — 발표 Q&A의 보험 |
| `docs/reports/assessments/`, `docs/reports/benchmarks/` | 실측 수치(레이아웃 신뢰도, LLM 지연·오염률 등)의 출처 |
| `docs/presentation/SPEAKER_NOTES_AND_QA.md` | 이미 준비된 발표자료·예상 질의응답(이 문서와 상호 보완) |

# 1단계: 이 프로젝트가 실행되는 4개 프로세스

| 프로세스 | 파일 | 실행 명령(개발) | 역할 |
| --- | --- | --- | --- |
| 검색·검수 API | `paperrag/search/api.py` (+ `review/api.py`를 하위 라우터로 포함) | `uvicorn paperrag.search.api:app` | `/search`, `/documents/*`, `/ready` |
| 임베딩 서버 | `paperrag/embed/server.py` | `uvicorn paperrag.embed.server:app --port 8100` | BGE-M3 HTTP 임베딩 |
| Celery worker | `paperrag/worker/app.py` | `celery -A paperrag.worker.app worker` | 검수 완료 문서의 비동기 적재 |
| Streamlit UI | `paperrag/ui/app.py` | `streamlit run src/paperrag/ui/app.py` | 검색 화면 + 검수 대시보드 |

4개가 전부 `paperrag.config.Settings`(환경변수, `PAPERRAG_` 접두사) 하나를 공유한다. **가장 먼저
`config.py`를 열어 필드 목록을 훑어 두면** 이후 어떤 파일에서 `settings.xxx`를 봐도 "이게 운영 정책
스위치구나"라고 바로 알아볼 수 있다.

# 2단계: 추천 읽기 순서 — STEP 1~8 파이프라인을 따라간다

가장 이해가 빠른 경로는 **논문 한 편이 실제로 통과하는 순서**를 그대로 따라가는 것이다. 오케스트레이터인
`paperrag/ingest/pipeline.py`의 `IngestPipeline.run()`을 펼쳐두고, 거기서 호출하는 각 함수를 따라
들어가며 아래 순서로 읽는다.

1. **`ingest/models.py`** — 파이프라인 전 구간이 공유하는 데이터 모델(`LayoutBlock`, `DocumentLayout`,
   `ParagraphDraft`, `EnrichedParagraph`, `TableDraft`, `PaperMeta`, `IngestReport`)부터 익힌다.
   여기 나오는 이름이 이후 모든 파일에서 반복된다.
2. **`ingest/pipeline.py` — `IngestPipeline.run()`** — STEP 1~8을 순서대로 호출하는 지휘자. 각 STEP은
   `stage()` 헬퍼로 감싸 `processing_jobs` 테이블에 상태를 기록하고, 실패 시 `report.record_stage`로
   에러를 남긴다. `run()` 끝의 `except Exception` 블록은 **STEP 4 이후 실패 시 이미 만든 `paper_id`를
   보상 삭제**하는 로직이다 — 부분 적재된 논문이 DB에 남지 않게 하는 안전장치.
3. **STEP 1 (`_validate_pdf_source`)** — PDF 시그니처만 확인. "digital/scanned 판정"은 더 이상 경로를
   바꾸지 않는다(2026-07-12 사용자 결정으로 전체 OCR 단일 경로로 통일됨). `ingest/triage.py`의
   `classify_pdf`는 이제 진단용 보조 정보일 뿐이라는 점이 헷갈리기 쉬우니 주의.
4. **STEP 2 (`layout_backend.analyze`)** — `ingest/layout/__init__.py`의 `get_backend()`로 실제
   구현체를 고른다. 운영 경로는 `paddle_backend.py`(`PaddleBackend`) 하나뿐이고, `simple_backend.py`·
   `docling_backend.py`는 진단/비교 전용이다. **`paddle_backend.py`는 이 프로젝트에서 가장 크고
   (~1700줄) 가장 중요한 파일**이니 별도로 3단계에서 깊게 다룬다. `layout/dedup.py`의
   `deduplicate_layout_blocks`는 여기서 만든 중복·컨테이너 박스를 정리한다.
5. **STEP 3 (`_filter_blocks` → `ingest/filterer.py`의 `split_blocks`)** — 12개 블록 타입을 메타
   (title/author/abstract) / 본문 / 표 / 제외(그림·수식·참고문헌 이후·각주)로 나눈다. 저자 영역이
   `text`로 오분류되는 문제를 완화하는 `_recover_author_regions`, 각주를 좌표 기하로 걸러내는
   `_is_probable_footnote`가 여기 있다. `pipeline.py`의 `_extract_meta`/`_extract_authors`가 이
   출력으로 제목·저자·연도를 뽑는 후처리를 담당한다(정규식 기반, 완벽하지 않음 — §4 참고).
6. **STEP 4 (`ingest/paragraphs.py`의 `build_paragraphs`)** — 섹션 귀속 → 100자 미만 병합, 1500자
   초과 문장 경계 분할. `_continues_sentence`가 "문장이 열에 걸쳐 끊겼는지" 판정하는 부분이 핵심이다.
7. **STEP 5 (`ingest/llm_enrich.py`)** — 단락마다 LLM 호출 1회로 `{cleaned_text, summary, keywords,
   is_topic_relevant}` JSON을 만든다. `OllamaClient`는 프롬프트+스키마 해시로 로컬 캐시를 쓰고, 한자/
   중국어 혼입을 감지하면 **1회 한정으로 영어 프롬프트 재시도**를 한다(`enrich_paragraph`의 `for attempt
   in range(2)` 루프). 그래도 실패하면 `allow_degraded_results` 설정에 따라 예외를 던지거나
   `PassthroughEnricher`(원문 그대로 통과)로 떨어진다 — 이 폴백이 2026-07-04 벤치마크에서 실제로
   동작이 확인된 안전장치다.
8. **STEP 6 (`ingest/keywords.py`)** — Kiwi 형태소 분석으로 정규화(`normalize`), `KeywordScore`로
   제목/초록/본문 등장 가중합 점수 계산.
9. **STEP 7 (`_embed_and_persist` → `ingest/embeddings.py` + `ingest/repository.py`)** — 단락·키워드·
   표·논문 각각을 BGE-M3로 임베딩한 뒤 `PostgresIngestRepository`로 저장. `upsert_keyword`가 코사인
   유사도 임계값(`keyword_alias_similarity_threshold`, 기본 0.95) 이상이면 새 키워드 대신 별칭
   (`keyword_aliases`)으로 병합하는 부분이 핵심.
10. **STEP 8 (`ingest/relations.py`의 `build_relations`)** — 임베딩 코사인×0.6 + 키워드 자카드×0.3 +
    연도 근접도×0.1로 연관 논문 점수를 매기고 상위 N개를 `paper_relations`에 저장.

여기까지 읽으면 "논문 한 편이 업로드되어 DB에 들어가기까지" 전체를 설명할 수 있다. 다음은 검수와 검색이다.

11. **`review/service.py`** — 위 STEP 2를 사람이 검수할 수 있게 쪼갠 상태 기계
    (`layout_review` → `ocr_review` → `ready_to_ingest`). `upload()`는 STEP 1~2만 실행하고 멈추고,
    `run_automatic_ocr()`가 나머지를 진행하며 `_automation_quality()`로 자동 합격/예외를 가른다.
12. **`search/service.py`** — `SearchService.search()`(정확 매칭 우선 → 실패 시 임베딩 유사도 제안)와
    `resolve()`(대표 논문 점수식·연관 논문 조회·엑셀 생성)를 읽는다.
13. **`ui/app.py`** — 검색 화면(`_render_search`)과 검수 대시보드(`_render_upload_review`)를 훑는다.
    UI는 `ui/client.py`를 통해서만 API를 호출하고 DB에 직접 붙지 않는다.

# 3단계: 모듈별 상세 가이드 (필요할 때 찾아보는 참고용)

## 3.1 기반 계층 — `config.py` / `db.py` / `readiness.py`

| 파일 | 핵심 | 참고 |
| --- | --- | --- |
| `config.py` | `Settings`(pydantic-settings) 하나가 전 구성요소의 유일한 설정 진입점. `.env` → `PAPERRAG_` 접두사 환경변수로만 값이 들어온다(하드코딩 금지 원칙). 임계값 옆 주석(예: `search_similarity_threshold` 옆 BGE-M3 실측 근거)을 눈여겨보면 "왜 이 숫자인지"가 보인다 | `docs/guide/01-environment.md` |
| `db.py` | SQLAlchemy 엔진을 프로세스당 1개로 캐시(`_engine` 전역 + `get_engine()`), `pool_pre_ping=True`로 죽은 커넥션 자동 재연결 | `docs/guide/03-database.md` |
| `readiness.py` | `/ready`가 쓰는 점검 로직. `_local_components`(운영 정책 스위치·모델 파일 존재 여부, 외부 호출 없음)와 `_database_status`/`_embedding_status`/`_llm_status`(실제 핑, `check_external=True`일 때만) 두 그룹으로 나뉜다. "구성요소 ready"와 "실제 논문 품질 합격"은 다르다는 게 이 파일의 핵심 메시지 | `docs/guide/10-production-readiness.md` |

## 3.2 논문 수집 — `collect/`

| 파일 | 핵심 |
| --- | --- |
| `collect/openalex.py` | OpenAlex API로 CC 라이선스·OA(오픈 액세스) 논문만 검색(`_allowed_licenses` 필터). work ID 정규화, 철회(retracted) 논문 배제 |
| `collect/service.py` | `PaperCollector._download`가 라이선스 재검증 → HTTPS 강제 → 크기 제한 스트리밍 다운로드 → PDF 시그니처 확인까지 다단 검증 후 저장. `ManifestStore`가 JSONL로 출처(provenance)를 기록해 중복 다운로드를 스킵 |
| `collect/smoke.py` | 수집된 PDF의 앞 N페이지만 잘라 CPU smoke test용 PDF를 만든다(`PAPERRAG_PAPER_SMOKE_PAGES`) |

## 3.3 레이아웃·OCR 백엔드 — `ingest/layout/`

| 파일 | 라이선스 | 위치 |
| --- | --- | --- |
| `base.py` | — | 모든 백엔드가 만족해야 하는 최소 계약(`analyze`), `Protocol`이라 상속 없이도 구조적으로 맞으면 통과 |
| `__init__.py` | — | `get_backend(name)` 레지스트리. `"paddle"/"pp-structure"/"pp-structurev3"`가 전부 같은 운영 백엔드를 가리키는 별칭 |
| `simple_backend.py` | pdfplumber, MIT | 텍스트 레이어 휴리스틱, **진단 전용** |
| `docling_backend.py` | MIT | 과거 디지털 PDF 트랙 후보, **현재 진단/비교 전용** (ADR-0002, 2026-07-12 폐기) |
| `dedup.py` | — | IoU/포함관계 기반 중복·컨테이너 박스 제거. `SPECIALIZED_PRIORITY`로 타입별 우선순위를 매겨 애매하면 더 구체적인 타입을 살린다 |
| `paddle_backend.py` | Apache-2.0 | **운영 단일 경로.** 아래 별도 항목 참고 |

### `paddle_backend.py` (약 1700줄 — 가장 중요한 파일)

`PaddleBackend`가 노출하는 3개 메서드가 전체 흐름이다.

1. `analyze_layout(pdf_path)` — 레이아웃 검출만 수행하고, **텍스트 검출선과 대조해 잘린 박스를
   확장하거나 누락된 본문 영역을 추가**(`_reconcile_layout_with_text` 계열 함수들)한다. 아직 OCR
   텍스트는 없다 — 사람이 검수할 박스 경계만 만드는 단계.
2. `recognize_layout(pdf_path, reviewed_blocks)` — 사람이 확정한 블록마다 crop → OCR. 표 블록은
   `_recognize_table`이 PP-LCNet으로 wired/wireless를 분류한 뒤 SLANeXt_wired 또는 SLANet_plus를
   적용하고, `_table_structure_quality`로 결과가 부실하면 반대 모델도 시도해 더 나은 쪽을 채택한다.
3. 그 사이에 있는 다수의 `_recover_*`/`_split_*`/`_merge_*` 함수들 — 제목/저자 영역 복구, 섹션 제목이
   본문과 붙어 잘못 분리된 경우 보정, 초록 인접 박스 병합 등 **실제 논문 조판에서 관찰된 구체적 실패
   패턴 하나하나를 겨냥한 보정 로직**이다. 함수 이름만 봐도 무엇을 고치는지 대략 짐작되도록 지어져
   있으니, 낯선 함수를 만나면 이름 먼저 읽고 docstring/주석으로 확인하는 순서를 권장한다.

CPU 환경 특유의 설정(`_configure_paddle_runtime`의 MKLDNN 비활성화, `review/service.py`의
`_run_isolated_paddle`이 별도 프로세스로 실행해 종료 후 메모리를 회수하는 방식)도 이 파일과 맞물려
동작한다.

## 3.4 검수 상태 기계 — `review/`

| 파일 | 핵심 |
| --- | --- |
| `models.py` | `ReviewDocument`(phase, blocks, warnings, automation_quality 등), `ReviewBlock`(`bbox` vs `detected_bbox`, `ocr_text` vs `corrected_text` — 자동 결과와 사람 교정을 분리 보존) |
| `store.py` | 파일시스템(JSON) 기반 저장소. **문서 삭제 API가 없고 다중 API replica 동시 수정 안전성도 없다** — 알려진 한계 |
| `service.py` | 상태 기계 본체. `upload()`(STEP1~2만) → `run_automatic_ocr()`(일괄 승인 후 OCR+품질 판정) → `_automation_quality()`(OCR 인식률·제목/저자 검출·표 구조화 비율 판정) → 합격 시 `ready_to_ingest`, 실패 시 `ocr_review`에 머무름. **주의**: 실패 사유가 "빈 블록"이 아니라 "블록 자체가 없음"(제목/저자 영역 미검출)이면 되돌릴 블록이 없어 관리자가 볼 `unreviewed` 항목이 0개인 채로 예외 큐에 남는다 — UI의 "검수 대기"/"OCR 품질 예외" 숫자가 어긋나는 근본 원인(§4 참고) |
| `viewer.py` | 서버사이드 HTML+SVG+JS 검수 뷰어. 별도 프론트엔드 프레임워크 없이 문자열 템플릿으로 구현 — 관리자 전용 내부 도구라 최소 의존성을 택함 |
| `api.py` | 위 서비스를 REST로 노출하는 얇은 라우터 |

## 3.5 검색 — `search/`

| 파일 | 핵심 |
| --- | --- |
| `service.py` | `search()`: 정확 매칭(빈도×질의 등장 위치 가중치) 우선 → 실패 시 임베딩 유사도 Top 3 제안. `resolve()`: 대표 논문 점수식(0.5×키워드 점수+0.3×단락 최고 유사도+0.1×제목/초록 등장+0.1×연도 가중치) 계산 후 사전 계산된 `paper_relations`에서 연관 논문 1편을 그대로 조회(실시간 계산 없음 — 응답 속도 확보) |
| `repository.py` | `PostgresSearchRepository`(pgvector 코사인 연산자 `<=>` 사용)와 테스트용 `InMemorySearchRepository` 두 구현이 같은 Protocol을 만족 |
| `excel.py` | 6시트(검색 결과 요약/대표 논문/대표 논문 단락/연관 논문/연관 논문 단락/표 데이터) 생성 |
| `sessions.py` | 유사 키워드 제안 세션(TTL) 저장 |
| `schemas.py` | API 요청/응답 pydantic 모델. `ResultBundle`은 API로 노출되지 않는 내부 계약(엑셀 생성용) |
| `api.py` | `POST /search`, `POST /search/select`, `GET /result/{id}/excel` 라우터. `review.api`의 라우터도 여기서 같은 앱에 포함(`app.include_router(review_router)`) |

## 3.6 UI — `ui/`

| 파일 | 핵심 |
| --- | --- |
| `client.py` | API를 감싸는 얇은 httpx 클라이언트. UI가 DB/리포지토리에 직접 접근하지 않는다는 원칙을 강제하는 유일한 창구 |
| `app.py` | 검색 화면(`_render_search`)과 검수 대시보드(`_render_upload_review`). 대시보드의 "검수 대기"는 블록 단위 `unreviewed` 개수 기준, "레이아웃 단계"/"OCR 품질 예외"는 `document.phase` 기준 — 서로 다른 축이라 숫자가 정확히 일치하지 않을 수 있다 |

## 3.7 임베딩·워커 — `embed/`, `worker/`

| 파일 | 핵심 |
| --- | --- |
| `embed/encoder.py` | `HashEncoder`(결정적 가짜 벡터, 개발/테스트 전용) vs `SentenceTransformerEncoder`(BGE-M3 실제 임베딩, `PAPERRAG_EMBED_ENCODER=st`일 때만 운영 허용) |
| `embed/server.py` | 위 인코더를 감싸는 FastAPI. `lifespan`에서 워밍업 1회 인코딩을 미리 실행해 첫 요청 지연을 없앰 |
| `worker/app.py` | Celery 앱 정의. 태스크 1개(`ingest_review_document`)만 등록되어 있고, 실제 로직은 `ReviewService.ingest`를 그대로 호출 — 아직 작업 제출/상태 조회 API와 연결되지 않아 CLI에서 직접 쓰이지는 않는 상태(guide 10 참고) |

## 3.8 운영 보조 스크립트 — `scripts/`

| 스크립트 | 핵심 |
| --- | --- |
| `preflight.py` | `readiness.py`를 CLI로 실행해 배포 전 구성요소 점검 |
| `download_paddle_models.py` | PaddleX 공식 모델을 `PAPERRAG_PADDLEX_MODEL_SOURCE`(기본 BOS)에서 받아 캐시 디렉터리에 준비 — 폐쇄망 반입 전 필수 |
| `apply_migrations.py` | `db/migrations`의 SQL을 순서대로 적용 |
| `demo_server.py` | DB·LLM·임베딩 서버 없이 인메모리 시드 데이터로만 응답하는 데모 전용 서버 — 운영 검증에는 쓸 수 없음 |
| `prepare_training_data.py` | Colab 파인튜닝용 데이터 준비 |
| `export_ocr_evaluation.py` | 실측 평가 Excel(논문요약/레이아웃_OCR/단락_요약/대표키워드/표_추출 시트) 생성, 사람 교정과 자동 결과를 색상으로 구분 |

# 4단계: 발표 중 자주 나올 법한 헷갈리는 지점

- **"Docling도 쓰나요?"** → 아니다. 운영 적재 경로는 `paddle_backend.py` 하나뿐이고 Docling·simple은
  진단/비교/단위 테스트에서만 쓰인다(ADR-0002, 2026-07-12 폐기 결정).
- **"검수 대기가 12건인데 왜 레이아웃 단계+OCR 품질 예외 합이 다르죠?"** → `ui/app.py`의
  `_filter_review_documents`가 "검수 대기"는 블록 단위(`unreviewed>0`), "레이아웃 단계"/"OCR 품질
  예외"는 문서 phase 단위로 서로 다른 기준을 쓴다. 제목/저자 영역 자체가 검출되지 않아 실패한 문서는
  되돌릴 블록이 없어 phase는 `ocr_review`인데 unreviewed는 0인 채로 남는다(`review/service.py`의
  `_automation_quality`/`run_automatic_ocr` 참고).
- **"LLM이 실패하면 어떻게 되나요?"** → `llm_enrich.py`가 CJK 혼입 등으로 2회 연속 실패하면
  `allow_degraded_results` 설정에 따라 예외를 던지거나(운영 기본값) `PassthroughEnricher`로 원문을
  그대로 통과시킨다. 조용히 틀린 결과를 정상처럼 위장하지 않는다는 게 설계 원칙이다.
- **"모델은 왜 이걸 골랐나요? 다른 문제는 없나요?"** →
  `docs/reports/assessments/2026-07-20-oss-deployment-risk-model-tradeoff.md`에 라이선스 리스크와
  모델별 트레이드오프·적절성 판단이 정리되어 있다. 이 문서가 "높음" 위험으로 지적한 PyMuPDF
  (AGPL-3.0)는 이후 pypdfium2/pdfplumber/pypdf(전부 BSD·Apache-2.0·MIT)로 교체해 해소했다.
- **"실측 품질은 어느 정도인가요?"** → `docs/reports/assessments/2026-07-12-two-paper-ocr-evaluation.md`
  (레이아웃 신뢰도 0.71~0.79, 요약 9.8% 다국어 오염 등 실측치)와
  `docs/reports/benchmarks/2026-07-04-llm-cpu.md`(7B 지연·폴백 실측)를 인용하면 된다.

# 5단계: 발표 대비 자가 점검 질문

아래 질문에 코드를 보지 않고 답할 수 있으면 이 문서를 다 소화한 것이다.

1. 논문 PDF 한 편이 업로드되어 검색 가능해지기까지 STEP 1~8이 각각 어느 파일·함수에서 실행되는가?
2. 운영 경로(paddle)와 진단 전용 경로(simple/docling)를 어떻게 구분하고, 왜 그렇게 나눴는가?
3. 레이아웃 검수와 OCR 검수는 왜 별도 단계로 분리돼 있고, 자동 품질 판정은 무엇을 기준으로 합격/예외를
   가르는가?
4. LLM 호출이 실패했을 때 시스템은 무엇을 하는가? 이 폴백이 실제로 동작한다는 근거는 무엇인가?
5. 검색에서 "정확 매칭"과 "유사 키워드 제안"은 각각 언제 발생하고, 대표/연관 논문은 각각 무슨 점수식으로
   정해지는가?
6. 이 프로젝트의 가장 큰 라이선스·운영 리스크 3가지는 무엇이고, 왜 지금 당장 문제가 되지 않는가(또는 되는가)?
7. "구성요소가 ready"라는 것과 "논문 품질이 production 합격"이라는 것은 왜 다른 주장인가?

## 완료 체크리스트

- [ ] STEP 1~8 각 단계가 어느 파일의 어느 함수에 대응하는지 설명할 수 있다
- [ ] 운영 경로와 진단 전용 경로(Docling/simple)를 구분해 설명할 수 있다
- [ ] 검수 상태 기계(레이아웃→OCR→적재)의 전이 조건과 알려진 UX 공백을 설명할 수 있다
- [ ] 대표/연관 논문 점수식과 그 근거를 설명할 수 있다
- [ ] 알려진 잔여 리스크(라이선스·품질 갭·동시성 제약)를 3가지 이상 말할 수 있다
- [ ] 5단계 자가 점검 질문 7개에 코드를 보지 않고 답할 수 있다
