# paper-rag — 온프레미스 논문 분석 RAG 시스템

비정형 PDF 논문을 OCR·레이아웃 분석으로 구조화해 PostgreSQL+pgvector에 저장하고,
자연어 질의 → 키워드 매칭 → 대표/연관 논문을 엑셀로 반환하는 시스템.
전체 설계: `docs/design/DESIGN.md`

## Git 규칙 (필수 준수)

- **Conventional Commits** prefix 사용: `feat:` `fix:` `docs:` `chore:` `refactor:` `test:` `perf:` `build:`
- 제목은 한국어 명령형, 50자 이내 (예: `feat: 검색 API 키워드 정확 매칭 추가`)
- **작업 단위별 커밋**: 하나의 커밋 = 하나의 논리적 변경.
  스캐폴드/스키마/파이프라인처럼 성격이 다른 변경을 한 커밋에 섞지 않는다
- 본문(선택): 무엇을/왜 요약. 코드로 알 수 없는 맥락만 기록
- 브랜치: 현재 단독 개발이므로 `main` 직접 커밋.
  협업 시작 시 `feat/...`, `fix/...` 브랜치 + PR로 전환
- 커밋 전 확인: 비밀값(.env, 키) 미포함, 생성물(data/, *.xlsx, __pycache__) 미포함

## 문서화 규칙

- 모든 구현 단계는 `docs/guide/NN-주제.md`에 **단계별(N단계) 형식**으로 문서화한다
  (형식 정의: `docs/guide/README.md`)
- 설계 결정은 `docs/adr/NNNN-제목.md`에 기록 (배경 → 결정 → 근거 → 영향)
- 작업 일지는 `docs/dev-log/YYYY-MM-DD.md`에 수행 작업·검증 결과·다음 할 일 기록

## 코드 규칙

- Python 3.12, 타입 힌트 필수, 패키지 루트: `src/paperrag/`
- 설정은 환경변수(.env) → `paperrag.config.Settings` 로만 접근 (하드코딩 금지)
- 무거운 의존성(docling, paddleocr, sentence-transformers)은 optional import —
  코어 패키지는 경량 의존성만으로 임포트 가능해야 한다
- 테스트: pytest. 외부 서비스(DB/LLM/임베딩)는 페이크/목으로 대체해 오프라인 실행 가능하게
