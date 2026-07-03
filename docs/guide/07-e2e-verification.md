# 07. 실 DB E2E 검증

pgserver 기반 PostgreSQL+pgvector로 수집, 검색, 엑셀, API 흐름을 종단 검증한다.

```text
pytest tests/integration
├── pgserver PostgreSQL+pgvector
├── scripts/apply_migrations.py
├── 합성 PDF 3편
├── IngestPipeline STEP 1~8
└── SearchService + FastAPI + xlsx
```

# 1단계: pgserver 설치

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 패키지 | `pgserver` | Docker 없이 테스트용 PostgreSQL을 실행한다. |
| 확장 | pgvector 포함 Postgres | `CREATE EXTENSION vector` 마이그레이션을 적용한다. |
| 용도 | 개발·검증 | 운영 DB 대체가 아니라 통합 테스트 전용이다. |

```bash
pip install pgserver
```

검증:
```bash
python - <<'PY'
import pgserver
print(pgserver.get_server)
PY
```

> 주의: pgserver는 개발·검증용이다. 운영은 `docker-compose.yml`의 PostgreSQL+pgvector 스택을 사용한다.

# 2단계: 통합 테스트 실행

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 테스트 경로 | `tests/integration` | 실 DB 시나리오 테스트 위치 |
| Python 경로 | `PYTHONPATH=src` | editable 설치가 없을 때 필요 |
| 실행기 | `.venv/bin/pytest` | 프로젝트 가상환경의 pytest 사용 |

```bash
PYTHONPATH=src .venv/bin/pytest tests/integration -q
```

검증:
```bash
PYTHONPATH=src .venv/bin/pytest -q
```

# 3단계: 검증 항목

| 시나리오 | 입력 | 기대 결과 |
| --- | --- | --- |
| 정확 매칭 | `스마트팩토리에서 이상탐지...` | `matched`, `exact`, 대표 paper1, 연관 paper2, 6시트 엑셀 |
| 유사 제안·선택 | `예지보전 관련 논문` | `suggest` 후보에 `예측 유지보수`, 선택 후 `matched`, 엑셀 생성 |
| 참고문헌 제외 | paper1 합성 PDF의 `References` 이후 텍스트 | `paragraphs.original_text`에 참고문헌 내용 없음 |
| API 레벨 | `POST /search`, `POST /search/select`, `GET /result/{id}/excel` | HTTP 200, xlsx content-type, 6시트 로드 가능 |

검증:
```bash
PYTHONPATH=src .venv/bin/pytest tests/integration/test_e2e_scenario.py -q
```

# 4단계: Docker 스택으로 동일 검증

| 항목 | 값 | 설명 |
| --- | --- | --- |
| DB | `docker compose up -d postgres` | 운영과 같은 PostgreSQL+pgvector 서비스 |
| 마이그레이션 | `python scripts/apply_migrations.py` | `PAPERRAG_DATABASE_URL` 대상에 적용 |
| 테스트 범위 | 수집·검색·API | pgserver 대신 compose DB를 바라보게 구성해 같은 흐름을 검증 |

```bash
docker compose up -d postgres
PAPERRAG_DATABASE_URL=postgresql+psycopg://paperrag:paperrag@localhost:5432/paperrag \
  python scripts/apply_migrations.py
PYTHONPATH=src PAPERRAG_DATABASE_URL=postgresql+psycopg://paperrag:paperrag@localhost:5432/paperrag \
  .venv/bin/pytest tests/integration -q
```

검증:
```bash
docker compose ps
PYTHONPATH=src .venv/bin/pytest tests/integration -q
```

> 주의: compose DB로 검증할 때는 기존 개발 데이터와 충돌하지 않도록 빈 DB 또는 테스트 전용 DB를 사용한다.

## 완료 체크리스트
- [ ] `pgserver`를 설치하고 import를 확인했다.
- [ ] `tests/integration` 테스트가 통과했다.
- [ ] 정확 매칭, 유사 제안·선택, 참고문헌 제외, API 엑셀 다운로드를 확인했다.
- [ ] 운영 검증은 pgserver가 아니라 compose 스택으로 수행한다는 제약을 확인했다.
