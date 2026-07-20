# 01. 개발 환경 구축

WSL2(Ubuntu) 위에 Python 개발 환경과 컨테이너 실행 환경을 준비한다.

```
개발 PC (Windows + WSL2 Ubuntu)
├─ Python 3.12 + venv          코드 실행·테스트
├─ Docker Desktop (WSL 통합)    PostgreSQL/Redis/Ollama 스택
├─ Codex CLI                    기계적 코드 작성 위임
└─ paper-rag 저장소             git 관리
```

# 1단계: 필수 도구 확인

| 도구 | 최소 버전 | 확인 명령 |
| --- | --- | --- |
| Python | 3.12 | `python3 --version` |
| git | 2.x | `git --version` |
| Docker | 24.x | `docker --version` |
| Codex CLI (선택) | 0.140+ | `codex --version` |

> **Docker Desktop WSL 통합**: WSL 안에서 `docker`가 "could not be found in this WSL 2 distro"로 실패하면
> Windows의 Docker Desktop → Settings → Resources → **WSL Integration**에서 사용 중인 배포판을 켜야 한다.
> 통합 전에는 컨테이너 스택(02 문서)을 기동할 수 없다 — 코드 작업(테스트는 전부 오프라인 설계)은 가능.

# 2단계: 저장소와 가상환경

```bash
cd ~/Projects/paper-rag
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # 코어 + pytest/ruff
pip install -e ".[ingest]"       # PDF 처리(pymupdf) 추가 시
```

검증:

```bash
pytest -q          # 전부 통과 (외부 서비스 불필요)
ruff check src tests
```

> **재현 가능한 설치(권장)**: `requirements-lock.txt`가 dev/ingest/embed/worker/ui
> extra의 전이 의존성까지 정확한 버전으로 고정한다. 위 `pip install -e ".[...]"` 대신
> `pip install -e . -c requirements-lock.txt`로 설치하면 매번 같은 버전 조합이
> 재현된다(`-c`는 이미 요구하는 패키지의 버전만 제약하고, 락파일에 있다고 없는
> 패키지를 새로 설치하지 않는다). ocr/ingest-full extra(paddlepaddle, paddleocr,
> docling, kiwipiepy)는 이 락파일에 포함돼 있지 않다 — paddlepaddle/paddleocr는
> pyproject.toml에 이미 정확한 버전(`==`)으로 고정돼 있고, 이 macOS 개발 머신에서는
> paddlepaddle==3.3.0의 배포판을 PyPI에서 찾지 못해(Linux 컨테이너 전용 배포판)
> pip-compile 해석 자체가 실패한다 — 실제 배포 대상인 Linux 빌드 환경에서 별도로
> 락파일을 만들어야 정확하다(아직 하지 않음, requirements-lock.txt 상단 주석 참고).
> 갱신: `pip install pip-tools` 후
> `pip-compile --extra dev --extra ingest --extra embed --extra worker --extra ui --output-file=requirements-lock.txt --strip-extras pyproject.toml`.

# 3단계: 환경 변수

```bash
cp .env.example .env
```

| 키 | 기본값 | 설명 |
| --- | --- | --- |
| `PAPERRAG_RUNTIME_MODE` | `production` | 준비 점검과 운영 정책 적용 |
| `PAPERRAG_ALLOW_DEGRADED_RESULTS` | `false` | LLM 실패 결과 대체 차단 |
| `PAPERRAG_ALLOW_DIAGNOSTIC_BACKENDS` | `false` | OCR 없는 backend 운영 차단 |
| `PAPERRAG_DATABASE_URL` | `postgresql+psycopg://paperrag:paperrag@localhost:5432/paperrag` | Cloud/로컬 PostgreSQL |
| `PAPERRAG_OLLAMA_BASE_URL` | `http://localhost:11434` | 로컬 LLM 서버 |
| `PAPERRAG_LLM_MODEL` | `qwen2.5:7b-instruct-q4_K_M` | 요약·키워드용 경량 LLM |
| `PAPERRAG_EMBED_BASE_URL` | `http://localhost:8100` | BGE-M3 임베딩 서버 |
| `PAPERRAG_EMBED_DIM` | `1024` | 스키마 VECTOR 차원과 일치해야 함 |

> `.env`는 커밋 금지(.gitignore 처리됨). 값 변경 시 스키마의 VECTOR(1024)와 임베딩 차원이
> 어긋나면 적재가 실패하므로 `PAPERRAG_EMBED_DIM`은 임의로 바꾸지 않는다.

# 4단계: 무거운 의존성 (선택 설치)

| extra | 포함 패키지 | 필요한 시점 |
| --- | --- | --- |
| `.[ingest]` | pymupdf | PDF 페이지 렌더링·simple 진단 백엔드 |
| `.[ingest-full]` | docling, paddleocr, kiwipiepy | 실제 레이아웃 분석·OCR·형태소 정규화 |
| `.[worker]` | celery[redis] | 대량 배치 병렬화 |
| `.[ui]` | streamlit | 검색 UI |

> 핵심 Paddle 레이아웃·OCR 모델 3종은 약 42MB이며 BGE-M3와 Ollama 모델이 전체 저장공간의 대부분을
> 차지한다. 폐쇄망 배포 전에는 설치 wheel과 `models/`·모델 캐시를 별도 번들로 준비한다.

## 완료 체크리스트

- [ ] `pytest -q` 통과
- [ ] `docker --version` 정상 (WSL 통합 활성)
- [ ] `.env` 생성 완료
- [ ] (선택) `codex --version` 정상
- [ ] `python scripts/preflight.py`의 실패 항목을 확인했다.
- [ ] (배포용) `pip install -e . -c requirements-lock.txt`로 설치했다.
