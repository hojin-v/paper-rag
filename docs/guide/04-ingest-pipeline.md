# 04. 수집 파이프라인

PDF 논문을 레이아웃 분석, 단락 정제, 키워드, 임베딩, 연관도 계산까지 처리해 DB에 적재한다.

```text
data/inbox/*.pdf
└── python -m paperrag.ingest
    ├── 업로드: 레이아웃 검수 → 확정 영역 OCR 검수
    ├── STEP 1~4: 검수 레이아웃, 필터, 단락
    ├── STEP 5~7: LLM 정제, 키워드, 임베딩
    ├── STEP 8: 연관 논문 계산
    └── docs/reports/ingest/YYYY-MM-DD.md
```

# 1단계: 의존성 설치

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 운영 수집 | `pip install -e ".[ingest-full]"` | PaddleOCR, pypdfium2, Kiwi 계열 |
| 진단 전용 | `pip install -e ".[ingest]"` | OCR 없는 simple backend 테스트 |
| 개발 테스트 | `pytest` | 외부 서비스 없이 단위 테스트 실행 |

```bash
pip install -e ".[ingest-full]"
PYTHONPATH=src ./scripts/with_paddle_runtime.sh \
  .venv/bin/python scripts/download_paddle_models.py
```

검증:
```bash
python -c "import paperrag.ingest.pipeline"
```

> 운영 정책은 PDF 텍스트 레이어를 읽지 않고 모든 페이지를 이미지 기반 OCR로 처리하는 것이다.
> `simple`과 `docling` backend는 비교·장애 진단용으로만 남겨 둔다.

| OCR 설정 | 기본값 | 설명 |
| --- | ---: | --- |
| `PAPERRAG_OCR_RENDER_DPI` | 200 | 전 페이지 OCR 입력 이미지 해상도 |

기본 모델은 `PP-DocLayout-M`, `PP-OCRv5_mobile_det`,
`korean_PP-OCRv5_mobile_rec`이다. 레이아웃의 `table` 영역은 `PP-LCNet_x1_0_table_cls`로 유선/무선 표를
분류해 `SLANeXt_wired` 또는 `SLANet_plus`로 전달한다. 1차 구조 결과의 행·열 밀도가 기준 미달이면 다른
표 모델 결과와 비교해 더 나은 결과를 선택한다. 두 구조 모델이 모두 실패한 일반 좌표 OCR 결과는 보존하되
자동 품질 합격으로 인정하지 않는다. Excel은 pipe 행을 셀 단위 시트로 정규화한다.

레이아웃 모델이 반환한 픽셀 좌표는 텍스트 검출 좌표와 먼저 대조한다. 겹친 텍스트 선까지 박스를 자동
확장하고 어떤 레이아웃에도 포함되지 않은 텍스트 선은 열·행 간격으로 묶어 본문 박스로 추가한다. 이후 PDF
좌표로 환산하며 OCR은 보정된 crop에만 실행한다. 품질 모니터에는 초기 텍스트 커버리지, 자동 확장 수,
자동 추가 본문 수를 표시한다.

# 2단계: 입력 준비

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 입력 디렉터리 | `data/inbox` | 배치 대상 PDF 위치 |
| 입력 형식 | `*.pdf` | 파일 또는 디렉터리 단위 실행 가능 |
| 결과 리포트 | `docs/reports/ingest` | 배치 종료 시 Markdown append |

```bash
mkdir -p data/inbox
ls -1 data/inbox
```

검증:
```bash
find data/inbox -maxdepth 1 -name "*.pdf" -type f
```

# 3단계: dry-run 실행

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 저장소 | `InMemoryIngestRepository` | DB 연결 없이 결과 요약만 출력 |
| LLM 생략 | `--skip-llm` | 단락 원문을 그대로 저장하고 앞 200자를 요약 |
| backend | `paddle` | 모든 페이지를 PP-StructureV3 OCR로 처리 |

```bash
python -m paperrag.ingest data/inbox --backend paddle --skip-llm --dry-run
```

검증:
```bash
test -f docs/reports/ingest/$(date +%F).md
```

# 4단계: DB 적재 실행

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 저장소 | `PostgresIngestRepository` | `paperrag.config.Settings.database_url` 사용 |
| LLM | Ollama `/api/chat` | `Settings.ollama_base_url`, `Settings.llm_model` 사용 |
| 임베딩 | `{embed_base_url}/embed` | `Settings.embed_base_url` 사용 |

```bash
python -m paperrag.ingest data/inbox --backend paddle
```

검증:
```bash
docker compose exec postgres psql -U paperrag -d paperrag -c "SELECT COUNT(*) FROM papers;"
docker compose exec postgres psql -U paperrag -d paperrag -c "SELECT COUNT(*) FROM paragraphs;"
```

> 주의: Ollama가 기동되어 있지 않으면 `Connection refused` 또는 `/api/chat` 연결 오류가 발생한다.

# 5단계: 실패 재시작 확인

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 상태 테이블 | `processing_jobs` | 단계별 `running/done/failed` 기록 |
| 실패 원인 | `error` | 예외 메시지 저장 |
| 재시작 기준 | 실패 stage | 해당 단계부터 재처리하도록 운영 작업에서 확인 |

```bash
docker compose exec postgres psql -U paperrag -d paperrag <<'SQL'
SELECT paper_id, stage, status, error, started_at, finished_at
FROM processing_jobs
WHERE status = 'failed'
ORDER BY finished_at DESC NULLS LAST
LIMIT 20;
SQL
```

검증:
```bash
docker compose exec postgres psql -U paperrag -d paperrag -c "SELECT stage, status, COUNT(*) FROM processing_jobs GROUP BY stage, status ORDER BY stage, status;"
```

# 6단계: 적재 검증

| 확인 대상 | SQL |
| --- | --- |
| 논문 | `SELECT COUNT(*) FROM papers;` |
| 단락 | `SELECT COUNT(*) FROM paragraphs;` |
| 키워드 | `SELECT COUNT(*) FROM keywords;` |
| 표 | `SELECT COUNT(*) FROM paper_tables;` |
| 연관도 | `SELECT COUNT(*) FROM paper_relations;` |

```bash
docker compose exec postgres psql -U paperrag -d paperrag <<'SQL'
SELECT 'papers' AS table_name, COUNT(*) FROM papers
UNION ALL SELECT 'paragraphs', COUNT(*) FROM paragraphs
UNION ALL SELECT 'keywords', COUNT(*) FROM keywords
UNION ALL SELECT 'paper_tables', COUNT(*) FROM paper_tables
UNION ALL SELECT 'paper_relations', COUNT(*) FROM paper_relations;
SQL
```

검증:
```bash
docker compose exec postgres psql -U paperrag -d paperrag -c "SELECT paper_id, title, status, created_at FROM papers ORDER BY paper_id DESC LIMIT 5;"
```

# 7단계: 단계와 선택지 확인

| 단계 | 입력 | 출력 | 실패 시 동작 |
| --- | --- | --- | --- |
| STEP 1 source check | PDF 경로 | `full_ocr` 정책 확인 | 모든 입력을 OCR 경로로 전달 |
| STEP 2 staged layout/OCR | 전 페이지 이미지와 검수 좌표 | 확정 좌표의 `DocumentLayout` + OCR | 검수 단계 복귀 또는 failed 기록 |
| STEP 3 filter | layout blocks | 메타, 본문, 표 블록 | failed 기록 후 중단 |
| STEP 4 paragraph | 본문 블록 | `ParagraphDraft` | failed 기록 후 중단 |
| STEP 5 llm_enrich | 단락, 표 | 정제 단락, 표 요약, 대표 키워드 | JSON 실패 1회 재시도 후 운영은 실패, 개발 허용 시에만 fallback |
| STEP 6 keywords | 키워드 후보 | 정규화 키워드와 점수 | failed 기록 후 중단 |
| STEP 7 embed | 단락/키워드/표/논문 텍스트 | 1024차원 임베딩 저장 | failed 기록 후 중단 |
| STEP 8 relate | 신규 논문, 후보 논문 | `paper_relations` | failed 기록 후 중단 |

| 옵션 | 값 | 설명 |
| --- | --- | --- |
| `--skip-llm` | 사용 | dry-run 또는 명시적 degraded 개발 모드에서만 사용 |
| `--skip-llm` | 미사용 | Ollama JSON 응답으로 단락 정제·요약·키워드 추출 |
| `--backend paddle` | 운영 기본 | 모든 PDF에 PP-StructureV3 레이아웃·OCR·표 영역 처리 |
| `--backend simple` | 진단 전용 | OCR 없이 pdfplumber 텍스트 줄 확인 |
| `--backend docling` | 비교 전용 | 모델 비교 및 장애 원인 분석 |

> 주의: 운영 적재에서 `simple`이나 `docling`을 사용하면 전체 OCR 정책을 위반한다.
> `PAPERRAG_ALLOW_DEGRADED_RESULTS=false`인 운영 모드에서는 LLM 오류를 앞부분 잘라내기나 규칙 기반
> 키워드로 대체하지 않고 해당 단계를 실패 처리한다.

```bash
python -m paperrag.ingest --help
```

검증:
```bash
python -c "from paperrag.ingest.layout import get_backend; print(type(get_backend('paddle')).__name__)"
```

## 완료 체크리스트
- [ ] 수집 선택 의존성을 설치했다.
- [ ] `data/inbox`에 PDF 입력을 준비했다.
- [ ] 기본 backend가 `paddle`인지 확인했다.
- [ ] dry-run으로 DB 없이 파이프라인을 검증했다.
- [ ] DB 적재 실행 후 주요 테이블 count를 확인했다.
- [ ] 실패 시 `processing_jobs`에서 단계와 원인을 확인할 수 있다.
- [ ] `docs/reports/ingest/YYYY-MM-DD.md` 배치 리포트가 생성된다.
