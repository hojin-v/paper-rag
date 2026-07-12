# 08. 임베딩 HTTP 서버

BGE-M3 임베딩 서버를 실제 sentence-transformers 모델로 실행하고 hash 테스트 벡터가 운영에 섞이지 않게 검증한다.

```text
수집/검색 파이프라인
  └─ HttpEmbeddingClient
       └─ POST http://localhost:8100/embed
            ├─ st: BAAI/bge-m3 sentence-transformers CPU 인코더 (운영)
            └─ hash: 모델 없이 1024차원 결정적 벡터 (단위 테스트 전용)
```

# 1단계: hash 테스트 모드 구분

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 앱 | `paperrag.embed.server:app` | FastAPI 임베딩 서버 엔트리포인트 |
| 모드 | `PAPERRAG_EMBED_ENCODER=hash` | 모델 파일 없이 단위 테스트용 결정적 벡터를 생성한다. 의미 검색에는 사용할 수 없다. |
| 차원 | `PAPERRAG_EMBED_DIM=1024` | 설계서의 BGE-M3 벡터 차원과 맞춘다. |
| 포트 | `8100` | `Settings.embed_base_url` 기본값과 같은 포트다. |

```bash
PAPERRAG_EMBED_ENCODER=hash \
uvicorn paperrag.embed.server:app --host 0.0.0.0 --port 8100
```

검증:
```bash
curl -s http://localhost:8100/health | python -m json.tool
```

기대 응답:
```json
{
  "status": "ok",
  "encoder": "hash",
  "model": "BAAI/bge-m3",
  "dim": 1024,
  "production_ready": false
}
```

> hash 벡터는 길이와 결정성만 검증한다. 검색 정확도를 나타내지 않으며 `/ready`는 이를 오류로 판정한다.

# 2단계: curl로 임베딩 검증

`HttpEmbeddingClient`와 같은 요청 형식으로 `texts` 배열을 보낸다.

```bash
curl -s -X POST http://localhost:8100/embed \
  -H 'Content-Type: application/json' \
  -d '{"texts":["온프레미스 RAG","BGE-M3 embedding"]}' \
  | python -m json.tool
```

검증:
```bash
curl -s -X POST http://localhost:8100/embed \
  -H 'Content-Type: application/json' \
  -d '{"texts":[]}' \
  | python -m json.tool
```

기대 응답:
```json
{
  "embeddings": []
}
```

| 응답 필드 | 값 | 설명 |
| --- | --- | --- |
| `embeddings` | `list[list[float]]` | `texts`와 같은 순서의 벡터 목록 |
| 벡터 차원 | `1024` | `PAPERRAG_EMBED_DIM` 값 |
| 빈 입력 | `[]` | 400이 아니라 빈 벡터 목록을 반환한다. |

# 3단계: 운영 모드 전환

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 선택 의존성 | `.[embed]` | `sentence-transformers`를 설치한다. |
| 모드 | `PAPERRAG_EMBED_ENCODER=st` | 실제 BAAI/bge-m3 모델을 사용한다. |
| 모델 | `PAPERRAG_EMBED_MODEL_NAME=BAAI/bge-m3` | 기본 운영 모델명 |
| 실행 장치 | CPU | 서버 내부에서 CPU로 모델을 로드한다. |

```bash
pip install -e ".[embed]"
PAPERRAG_EMBED_ENCODER=st \
PAPERRAG_EMBED_MODEL_NAME=BAAI/bge-m3 \
uvicorn paperrag.embed.server:app --host 0.0.0.0 --port 8100
```

검증:
```bash
curl -s http://localhost:8100/health | python -m json.tool
```

`encoder=st`, `dim=1024`, `production_ready=true`인지 확인한다.

> 주의: `st` 모드 최초 실행 시 BGE-M3 모델 다운로드가 발생하며 약 2GB의 모델 파일 공간이 필요하다. 폐쇄망 운영 환경에서는 모델 캐시를 사전에 반입한다.

> 주의: 인코더 또는 모델을 변경하면 기존 저장 벡터와 호환되지 않는다. `docs/design/DESIGN.md` §6의 임베딩 파인튜닝 절차와 동일하게 전체 벡터를 재임베딩해야 한다.

# 4단계: compose embedder 서비스 사용

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 서비스 | `embedder` | 동일 이미지에서 임베딩 서버만 실행한다. |
| command | `uvicorn paperrag.embed.server:app --host 0.0.0.0 --port 8100` | compose 서비스 명령 |
| 기본 모드 | `PAPERRAG_EMBED_ENCODER=st` | compose가 운영 모델을 강제한다. |
| DB 의존성 | 없음 | PostgreSQL 기동 상태와 무관하게 실행 가능하다. |

```bash
docker compose up embedder
```

검증:
```bash
docker compose ps embedder
curl -s http://localhost:8100/health
```

# 5단계: 파이프라인 연동

수집 파이프라인과 검색 서비스는 `PAPERRAG_EMBED_BASE_URL`을 통해 임베딩 서버를 호출한다.

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 로컬 실행 | `PAPERRAG_EMBED_BASE_URL=http://localhost:8100` | 기본값 |
| compose 내부 | `PAPERRAG_EMBED_BASE_URL=http://embedder:8100` | 다른 서비스에서 compose 네트워크로 접근 |
| 호출 경로 | `/embed` | `HttpEmbeddingClient`가 사용하는 endpoint |

```bash
PAPERRAG_EMBED_BASE_URL=http://localhost:8100 \
python -m paperrag.ingest data/inbox --backend paddle
```

검증:
```bash
PAPERRAG_EMBED_BASE_URL=http://localhost:8100 \
python -c "from paperrag.ingest.embeddings import HttpEmbeddingClient; print(len(HttpEmbeddingClient().embed(['test'])[0]))"
```

## 완료 체크리스트
- [ ] hash 모드가 `production_ready=false`이며 운영 `/ready`에서 거부된다.
- [ ] `/embed`가 `{"texts": [...]}` 요청에 `{"embeddings": [...]}`로 응답한다.
- [ ] 빈 `texts` 요청이 빈 `embeddings`를 반환한다.
- [ ] 운영 모드 전환에 필요한 `.[embed]` 의존성과 `PAPERRAG_EMBED_ENCODER=st` 설정을 확인했다.
- [ ] compose `embedder` 서비스가 8100 포트를 노출한다.
- [ ] 수집/검색 서비스의 `PAPERRAG_EMBED_BASE_URL`이 임베딩 서버를 가리킨다.
