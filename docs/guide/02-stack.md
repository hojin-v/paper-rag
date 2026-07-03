# 02. docker-compose 스택 구축

Docker Compose로 PostgreSQL+pgvector, Redis, Ollama와 선택 실행 서비스(API/worker/UI)를 준비한다.

```text
paper-rag
├── docker-compose.yml
├── Dockerfile
├── .env
├── postgres:5432
├── redis:6379
└── ollama:11434
```

| 서비스 | 용도 | 기본 실행 |
| --- | --- | --- |
| postgres | 논문 메타데이터, 단락, 키워드, 벡터 저장 | 예 |
| redis | Celery 작업 큐 브로커 | 예 |
| ollama | 로컬 LLM 모델 서빙 | 예 |
| api | FastAPI 검색 API | 예 |
| worker | 수집/분석 배치 워커 | 아니오, `worker` profile |
| ui | Streamlit 사용자 UI | 아니오, `ui` profile |

| 포트 | 서비스 | 설명 |
| --- | --- | --- |
| 5432 | postgres | PostgreSQL 접속 |
| 6379 | redis | Redis 접속 |
| 11434 | ollama | Ollama API |
| 8000 | api | FastAPI HTTP |
| 8501 | ui | Streamlit HTTP |

> 비용/리소스 주의: Ollama 모델은 CPU와 메모리를 지속적으로 사용한다. 운영 전 디스크 여유 공간,
> PostgreSQL 볼륨(`pgdata`), Ollama 모델 볼륨(`ollama_models`) 크기를 먼저 확인한다.

# 1단계: 사전 조건 확인

| 항목 | 값 | 설명 |
| --- | --- | --- |
| Docker | 24.x 이상 권장 | 컨테이너 실행 |
| Docker Compose | v2 권장 | `docker compose` 명령 사용 |

```bash
docker version
docker compose version
```

검증:
```bash
docker compose version
```

# 2단계: .env 준비

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 기준 파일 | `.env.example` | 프로젝트 기본 설정 |
| 대상 파일 | `.env` | compose 실행 시 읽는 환경 파일 |

```bash
cp .env.example .env
```

검증:
```bash
test -f .env
grep '^PAPERRAG_DATABASE_URL=' .env
```

# 3단계: compose up

기본 인프라 서비스만 먼저 올린다. API, worker, UI는 해당 모듈 구현 뒤 별도로 실행한다.

| 명령 | 실행 서비스 | 설명 |
| --- | --- | --- |
| `make up` | postgres, redis, ollama | 기본 로컬 스택 |
| `docker compose up -d api` | api | 검색 API 구현 후 실행 |
| `docker compose --profile worker up -d worker` | worker | 배치 워커 구현 후 실행 |
| `docker compose --profile ui up -d ui` | ui | UI 구현 후 실행 |

```bash
make up
```

검증:
```bash
docker compose ps postgres redis ollama
```

# 4단계: Ollama 모델 pull

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 모델 | `qwen2.5:7b-instruct-q4_K_M` | 설계서 기준 로컬 LLM |
| 저장 위치 | `ollama_models` volume | 컨테이너 재시작 후에도 유지 |

```bash
docker compose exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
```

검증:
```bash
docker compose exec ollama ollama list
```

# 5단계: 서비스 헬스체크

| 서비스 | 확인 명령 | 기대 결과 |
| --- | --- | --- |
| postgres | `docker compose exec postgres pg_isready -U paperrag -d paperrag` | accepting connections |
| redis | `docker compose exec redis redis-cli ping` | PONG |
| ollama | `curl -fsS http://localhost:11434/api/tags` | 모델 목록 JSON |
| api | `curl -fsS http://localhost:8000/docs` | OpenAPI 문서 |
| ui | `curl -fsS http://localhost:8501/_stcore/health` | ok |

```bash
docker compose exec postgres pg_isready -U paperrag -d paperrag
docker compose exec redis redis-cli ping
curl -fsS http://localhost:11434/api/tags
```

검증:
```bash
docker compose ps
```

## 완료 체크리스트
- [ ] Docker와 Docker Compose 버전을 확인했다.
- [ ] `.env.example`을 기준으로 `.env`를 준비했다.
- [ ] postgres, redis, ollama 컨테이너가 실행 중이다.
- [ ] Ollama에 `qwen2.5:7b-instruct-q4_K_M` 모델을 내려받았다.
- [ ] 기본 서비스 헬스체크가 성공했다.
