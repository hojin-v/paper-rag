# paper-rag

온프레미스 RAG 기반 논문 분석 시스템.
비정형 PDF 논문을 OCR·레이아웃 분석으로 구조화(단락/표/요약/키워드)해 PostgreSQL+pgvector에 저장하고,
자연어 질의를 키워드 정확 매칭 → 유사 키워드 추천으로 처리해 **대표 논문 1편 + 연관 논문 1편**을
단락별 원문/요약/표 요약이 담긴 **엑셀**로 반환한다.

## 문서 맵

| 문서 | 내용 |
| --- | --- |
| [docs/design/DESIGN.md](docs/design/DESIGN.md) | 통합 설계서 (아키텍처, 스키마, 파인튜닝·데이터 계획) |
| [docs/guide/](docs/guide/) | 단계별 구축 가이드 (환경 → 스택 → DB → 파이프라인 → 검색 API) |
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
```

상세 절차는 [docs/guide/01-environment.md](docs/guide/01-environment.md)부터 순서대로.
