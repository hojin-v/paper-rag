# 03. DB 스키마 적용

`schema_migrations`로 적용 이력을 관리하며 PostgreSQL+pgvector 초기 스키마를 적용한다.

```text
db
└── migrations
    └── 0001_init.sql
scripts
└── apply_migrations.py
postgres
└── schema_migrations
```

| 테이블 | 역할 |
| --- | --- |
| papers | 논문 메타데이터와 논문 임베딩 |
| paragraphs | 단락 원문, 정제문, 요약, 단락 임베딩 |
| keywords | 정규화 키워드, 빈도, 키워드 임베딩 |
| keyword_aliases | 키워드 별칭과 대표 키워드 연결 |
| paper_keywords | 논문과 키워드의 가중치 연결 |
| paper_tables | 추출 표 본문, 요약, 표 임베딩 |
| paper_relations | 대표 논문과 연관 논문 사전 계산 결과 |
| processing_jobs | 수집 파이프라인 단계별 처리 상태 |
| search_results | 검색 결과와 엑셀 산출물 캐시 |

# 1단계: 마이그레이션 개념 확인

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 이력 테이블 | `schema_migrations` | 적용된 SQL 파일명 기록 |
| 적용 단위 | `db/migrations/*.sql` | 파일명 순서대로 실행 |
| 트랜잭션 | 파일 1개당 1개 | 실패 시 해당 파일 적용 취소 |

```bash
ls -1 db/migrations
```

검증:
```bash
test -f db/migrations/0001_init.sql
```

# 2단계: make migrate 실행

PostgreSQL 컨테이너가 먼저 실행되어 있어야 한다.

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 실행 명령 | `make migrate` | `scripts/apply_migrations.py` 실행 |
| DSN 출처 | `paperrag.config.Settings` | `.env` 또는 기본값 사용 |
| 중복 실행 | skip | 이미 적용한 파일은 건너뜀 |

```bash
make migrate
```

검증:
```bash
python scripts/apply_migrations.py
```

# 3단계: psql로 테이블과 인덱스 확인

| 확인 대상 | 명령 |
| --- | --- |
| 테이블 목록 | `\dt` |
| HNSW 인덱스 | `pg_indexes` 조회 |
| 적용 이력 | `schema_migrations` 조회 |

```bash
docker compose exec postgres psql -U paperrag -d paperrag -c "\dt"
docker compose exec postgres psql -U paperrag -d paperrag -c "SELECT filename, applied_at FROM schema_migrations ORDER BY filename;"
docker compose exec postgres psql -U paperrag -d paperrag -c "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public' AND indexdef ILIKE '%hnsw%' ORDER BY tablename, indexname;"
```

검증:
```bash
docker compose exec postgres psql -U paperrag -d paperrag -c "SELECT COUNT(*) FROM schema_migrations WHERE filename = '0001_init.sql';"
```

# 4단계: 롤백과 재적용

초기 개발 환경에서는 앱 테이블을 삭제하고 해당 마이그레이션 이력을 지운 뒤 다시 적용한다.

| 작업 | 명령 | 설명 |
| --- | --- | --- |
| 롤백 | `DROP TABLE ... CASCADE` | 앱 테이블 삭제 |
| 이력 삭제 | `DELETE FROM schema_migrations` | 특정 파일 재적용 가능 |
| 재적용 | `make migrate` | 남은 미적용분 실행 |

```bash
docker compose exec postgres psql -U paperrag -d paperrag <<'SQL'
DROP TABLE IF EXISTS
    search_results,
    processing_jobs,
    paper_relations,
    paper_tables,
    paper_keywords,
    keyword_aliases,
    paragraphs,
    keywords,
    papers
CASCADE;

DELETE FROM schema_migrations WHERE filename = '0001_init.sql';
SQL

make migrate
```

검증:
```bash
docker compose exec postgres psql -U paperrag -d paperrag -c "\dt"
```

> 주의: 롤백 명령은 저장된 논문, 단락, 키워드, 검색 결과 데이터를 삭제한다. 운영 데이터베이스에서는
> 백업과 승인 절차 없이 실행하지 않는다.

## 완료 체크리스트
- [ ] `schema_migrations`의 역할을 확인했다.
- [ ] `make migrate`로 `0001_init.sql`을 적용했다.
- [ ] 9개 앱 테이블과 `schema_migrations`를 확인했다.
- [ ] HNSW 인덱스 3개를 확인했다.
- [ ] 개발 환경에서만 사용할 롤백·재적용 절차를 이해했다.
