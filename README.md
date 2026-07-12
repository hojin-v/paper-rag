# paper-rag

온프레미스 RAG 기반 논문 분석 시스템.
비정형 PDF 논문을 OCR·레이아웃 분석으로 구조화(단락/표/요약/키워드)해 PostgreSQL+pgvector에 저장하고,
자연어 질의를 키워드 정확 매칭 → 유사 키워드 추천으로 처리해 **대표 논문 1편 + 연관 논문 1편**을
단락별 원문/요약/표 요약이 담긴 **엑셀**로 반환한다.

논문 등록 화면에서는 PDF 페이지 위에 레이아웃 분석 영역을 표시하고, 영역을 클릭해 OCR 원문·유형·
신뢰도를 확인하거나 교정할 수 있다. 검수 결과는 DB·pgvector 적재와 Colab 레이아웃/OCR 학습데이터로
공통 사용한다.

## 문서 맵

| 문서 | 내용 |
| --- | --- |
| [docs/design/DESIGN.md](docs/design/DESIGN.md) | 통합 설계서 (아키텍처, 스키마, 파인튜닝·데이터 계획) |
| [docs/guide/](docs/guide/) | 단계별 구축 가이드 (환경 → 스택 → DB → 파이프라인 → 검색 API) |
| [docs/guide/09-upload-review-colab-training.md](docs/guide/09-upload-review-colab-training.md) | PDF 클릭 검수와 버튼형 Colab 학습 |
| [docs/guide/10-production-readiness.md](docs/guide/10-production-readiness.md) | 실사용 준비 점검과 숨기지 않는 잔여 위험 |
| [docs/guide/11-paper-collection.md](docs/guide/11-paper-collection.md) | CC 라이선스 논문 API 수집과 OCR smoke set |
| [docs/guide/12-macbook-remote-development-handoff.md](docs/guide/12-macbook-remote-development-handoff.md) | MacBook·Tailscale 원격 개발 구성과 현재 작업 인수인계 |
| [docs/adr/](docs/adr/) | 설계 결정 기록 |
| [docs/dev-log/](docs/dev-log/) | 작업 일지 |
| [CLAUDE.md](CLAUDE.md) | git·문서화·작업 방식 규칙 |

## 빠른 시작

```bash
# 1. 스택 기동 (PostgreSQL+pgvector, Redis, Ollama)
docker compose up -d

# 2. 스키마 적용
make migrate

# 3. 개발 환경
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest

# 4. API 서버
uvicorn paperrag.search.api:app --reload

# 5. 실제 모델·DB·LLM 준비 상태 확인
python scripts/preflight.py
```

상세 절차는 [docs/guide/01-environment.md](docs/guide/01-environment.md)부터 순서대로.
