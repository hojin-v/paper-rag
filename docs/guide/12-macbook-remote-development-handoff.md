# 12. MacBook·Tailscale 원격 개발 인수인계

이 문서는 새 Codex 세션이 저장소를 clone한 직후 현재 구현 상태를 복원하고, MacBook과 기존 Linux
호스트의 자원을 Tailscale 안에서 나눠 쓰기 위한 기준 문서다. Git에는 소스·테스트·문서만 저장한다.
`.env`, 논문 PDF, 모델, PostgreSQL 데이터와 실행 결과는 비밀값·라이선스·용량 문제로 Git에 넣지 않는다.

## 1. 권장 역할 분담

Apple Silicon에서 PaddleOCR/PaddleX 운영 경로를 그대로 재현하는 것은 패키지·모델 호환성 검증이
필요하다. 우선 다음처럼 분담하면 현재 검증된 Linux OCR 경로를 유지하면서 Mac 자원을 활용할 수 있다.

| 호스트 | 역할 |
| --- | --- |
| MacBook | Codex·편집기, Streamlit UI, Ollama, 선택적으로 BGE-M3 임베딩 서버 |
| 기존 Linux 호스트 | FastAPI, Paddle 레이아웃/OCR/표 모델, PostgreSQL+pgvector, Redis, 논문·검수 데이터 |

Linux API가 Mac의 Ollama·임베딩 서버를 호출하도록 Linux의 `.env`에서 아래 주소만 Tailscale DNS
이름으로 바꾼다. 포트는 인터넷에 공개하지 않고 tailnet ACL로 두 호스트 사이만 허용한다.

```dotenv
PAPERRAG_OLLAMA_BASE_URL=http://<mac-tailnet-name>:11434
PAPERRAG_EMBED_BASE_URL=http://<mac-tailnet-name>:8100
```

Mac에서 검색 UI만 실행할 때는 다음처럼 Linux API를 지정한다. 브라우저 iframe도 접근할 수 있도록
public 주소 역시 `localhost`가 아닌 Linux의 Tailscale 이름이어야 한다.

```dotenv
PAPERRAG_API_BASE_URL=http://<linux-tailnet-name>:8000
PAPERRAG_PUBLIC_API_BASE_URL=http://<linux-tailnet-name>:8000
```

## 2. MacBook 소스 준비

```bash
git clone git@github.com:hojin-v/paper-rag.git
cd paper-rag
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,ingest,embed,ui]'
cp .env.example .env
pytest -q
ruff check src tests
```

실제 OCR을 Linux에서 계속 실행한다면 Mac에는 `ingest-full` extra와 Paddle 모델이 필요 없다.
Mac에서 임베딩 서버를 실행하려면 BGE-M3 모델 캐시가 별도로 필요하다.

```bash
PAPERRAG_EMBED_ENCODER=st \
python -m uvicorn paperrag.embed.server:app --host 0.0.0.0 --port 8100

OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

## 3. Git에서 제외된 상태 이전

현재 ignore 대상은 `.env`, `data/`, `models/`, `outputs/`, `.venv/`다. Mac에서도 실제 데이터가
필요한 경우에만 Tailscale SSH로 복사한다. `<linux-tailnet-name>`과 경로는 실제 환경에 맞춘다.

```bash
rsync -a --info=progress2 \
  <linux-tailnet-name>:/home/hojin/00_Projects/paper-rag/models/ ./models/

rsync -a --info=progress2 \
  <linux-tailnet-name>:/home/hojin/00_Projects/paper-rag/data/ ./data/

scp <linux-tailnet-name>:/home/hojin/00_Projects/paper-rag/.env .env
chmod 600 .env
```

논문 원본과 모델은 GitHub를 거치지 않는다. `.env`에는 OpenAlex 키 등이 있으므로 신뢰하는 tailnet
내부에서만 전송한다. PostgreSQL 데이터는 `data/` 복사로 이동하지 않는다. DB까지 Mac으로 옮겨야 할
때는 Linux에서 `pg_dump`를 만들고 Mac의 동일 마이그레이션 버전 DB에 `pg_restore`한다. 원격 개발만
목적이면 DB는 Linux에 유지하는 편이 안전하다.

## 4. 현재 구현 상태

- 모든 PDF는 텍스트 레이어를 사용하지 않고 페이지 이미지 기반 OCR 경로로 처리한다.
- 레이아웃은 `PP-DocLayout-M`, 텍스트 검출은 `PP-OCRv5_mobile_det`, OCR은
  `korean_PP-OCRv5_mobile_rec`을 사용한다.
- 레이아웃 박스와 텍스트 검출선을 대조해 잘린 박스를 확장하고 누락 본문 영역을 추가한다.
- 표는 `PP-LCNet_x1_0_table_cls`로 wired/wireless를 분류한 뒤 `SLANeXt_wired` 또는
  `SLANet_plus`를 사용하며, 구조 품질이 낮으면 반대 모델도 실행해 더 나은 결과를 선택한다.
- 운영 화면은 별도 발표 페이지가 아니라 자동 처리 품질 모니터다. 관리자 교정은 예외 데이터와
  재학습 정답을 만드는 보조 기능이다.
- 검색은 정확 키워드, 유사 키워드 제안, 대표 논문 1편+연관 논문 1편, 설명과 6개 시트 Excel 출력
  흐름으로 구현돼 있다.
- OpenAlex 기반 CC 라이선스 논문 수집기와 CPU smoke PDF 생성기가 있다.
- 전체 테스트는 2026-07-13 기준 `103 passed`, Ruff 통과 상태다.

## 5. 최근 LayoutLMv2 실측 맥락

테스트 문서 `LayoutLMv2: Multi-modal Pre-training for Visually-rich Document Understanding`은 전체
13쪽 원문과 별도로 원본 1쪽·6쪽만 묶은 2쪽 대표본을 사용했다. 대표본에서 `1 Introduction` 다음에
`3.3 Results`가 보이는 것은 OCR 누락이 아니라 입력이 중간 페이지를 포함하지 않기 때문이다.

최근 2쪽 재실측 결과:

| 항목 | 결과 |
| --- | ---: |
| 레이아웃 영역 | 27 |
| 텍스트 검출선 | 220 |
| 초기 레이아웃 포괄률 | 90% |
| 자동 확장 / 누락 보완 | 21 / 4 |
| 영역 OCR 포괄률 | 91.7% |
| 표 구조화 | 1/1 |
| CPU 영역 OCR | 약 7분 6초 |
| 최대 관찰 worker RSS | 약 1.75GB |

초록 앞부분이 별도 본문으로 떨어지던 문제는 인접 초록 박스 병합으로 해결했다. 오른쪽 열의
`applications...`가 `abstract`로 오분류되던 문제는 섹션 시작 이후의 초록 라벨을 본문으로 교정해
해결했다. 왼쪽 열의 `many business`와 오른쪽 열의 `applications.`처럼 문장 중간에서 열이 바뀌는
경우에는 최종 단락 생성 단계에서 다시 연결한다. 하단 저자 각주는 페이지 위치·높이·폭 조건으로
본문에서 제외한다.

## 6. 숨기지 않는 잔여 문제

- 저자 줄이 `author`가 아닌 일반 `text`로 분류되는 실제 사례가 있어 저자 메타데이터가 비어 있다.
- 저자·소속 부근에 겹친 작은 영역이 남는 사례가 있다.
- `doc-uments`, `differ ent` 같은 줄바꿈·OCR 단어 분절 정규화가 더 필요하다.
- 각주 제외는 기하 휴리스틱이므로 여러 조판의 정답셋으로 오탐률을 측정해야 한다.
- 현재 자동 품질 판정은 저자 누락을 합격 조건으로 사용하지 않는다.
- 레이아웃 박스는 기하학적 OCR 단위이며 의미 단락이 아니다. 섹션 귀속·읽기 순서·문장 재결합을
  통과한 뒤에만 RAG 단락으로 저장해야 한다.
- CPU 순차 OCR과 표 모델 이중 비교는 정확도에는 유리하지만 대량 적재 처리량이 낮다. 배치 OCR,
  비동기 작업 큐와 단계별 처리시간 계측이 production 전 필요하다.
- 전체 13쪽 LayoutLMv2 논문의 최신 보정 경로 종단 실측은 아직 하지 않았다.

## 7. 새 Codex 세션 시작 프롬프트

새 세션에서는 대화 기록 자체가 자동 이전되지 않는다. 다음 순서로 문맥을 복원한다.

```text
이 저장소의 AGENTS.md, docs/design/DESIGN.md,
docs/guide/12-macbook-remote-development-handoff.md를 먼저 읽어라.
현재 목표는 비정형 논문 PDF의 자동 레이아웃→영역 OCR→표 구조화→섹션·단락 재조립→
RDB/pgvector 적재→키워드 검색→대표 1편+연관 1편 Excel 출력이다.
인수인계 문서의 '숨기지 않는 잔여 문제'를 완료로 간주하지 말고, 변경 전 테스트와 실측 근거를 확인하라.
```

새 작업을 시작하기 전에 `python scripts/preflight.py`, `pytest -q`, `ruff check src tests`를 실행하고,
Git에서 제외된 모델·데이터·환경변수가 어느 호스트에 있는지 먼저 확인한다.
