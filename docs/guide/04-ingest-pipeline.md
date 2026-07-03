# 04. 수집 파이프라인

PDF 논문을 레이아웃 분석, 단락 정제, 키워드, 임베딩, 연관도 계산까지 처리해 DB에 적재한다.

```text
data/inbox/*.pdf
└── python -m paperrag.ingest
    ├── STEP 1~4: PDF 판별, 레이아웃, 필터, 단락
    ├── STEP 5~7: LLM 정제, 키워드, 임베딩
    ├── STEP 8: 연관 논문 계산
    └── docs/reports/ingest/YYYY-MM-DD.md
```

# 1단계: 의존성 설치

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 기본 수집 | `pip install -e ".[ingest]"` | PyMuPDF 기반 triage/simple backend |
| 전체 수집 | `pip install -e ".[ingest-full]"` | Docling, PaddleOCR, Kiwi 계열 |
| 개발 테스트 | `pytest` | 외부 서비스 없이 단위 테스트 실행 |

```bash
pip install -e ".[ingest]"
```

검증:
```bash
python -c "import paperrag.ingest.pipeline"
```

> 주의: Docling backend를 사용할 서버는 `pip install -e ".[ingest-full]"`로 별도 준비한다.

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
| backend | `simple` | PyMuPDF 텍스트 블록 기반 폴백 |

```bash
python -m paperrag.ingest data/inbox --backend simple --skip-llm --dry-run
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
python -m paperrag.ingest data/inbox --backend simple
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
| STEP 1 triage | PDF 경로 | `digital/scanned` | `processing_jobs` failed 기록 후 중단 |
| STEP 2 layout | PDF 경로 | `DocumentLayout` | failed 기록 후 중단 |
| STEP 3 filter | layout blocks | 메타, 본문, 표 블록 | failed 기록 후 중단 |
| STEP 4 paragraph | 본문 블록 | `ParagraphDraft` | failed 기록 후 중단 |
| STEP 5 llm_enrich | 단락, 표 | 정제 단락, 표 요약, 대표 키워드 | JSON 실패 1회 재시도 후 passthrough fallback |
| STEP 6 keywords | 키워드 후보 | 정규화 키워드와 점수 | failed 기록 후 중단 |
| STEP 7 embed | 단락/키워드/표/논문 텍스트 | 1024차원 임베딩 저장 | failed 기록 후 중단 |
| STEP 8 relate | 신규 논문, 후보 논문 | `paper_relations` | failed 기록 후 중단 |

| 옵션 | 값 | 설명 |
| --- | --- | --- |
| `--skip-llm` | 사용 | Ollama 없이 passthrough 정제와 단순 키워드 후보 사용 |
| `--skip-llm` | 미사용 | Ollama JSON 응답으로 단락 정제·요약·키워드 추출 |
| `--backend simple` | 기본 | PyMuPDF 텍스트 블록 기반 폴백 |
| `--backend docling` | 선택 | Docling 설치 환경에서 레이아웃 분석 |

```bash
python -m paperrag.ingest --help
```

검증:
```bash
python -c "from paperrag.ingest.layout import get_backend; print(type(get_backend('simple')).__name__)"
```

## 완료 체크리스트
- [ ] 수집 선택 의존성을 설치했다.
- [ ] `data/inbox`에 PDF 입력을 준비했다.
- [ ] dry-run으로 DB 없이 파이프라인을 검증했다.
- [ ] DB 적재 실행 후 주요 테이블 count를 확인했다.
- [ ] 실패 시 `processing_jobs`에서 단계와 원인을 확인할 수 있다.
- [ ] `docs/reports/ingest/YYYY-MM-DD.md` 배치 리포트가 생성된다.
