# Codex 작업 규칙 (paper-rag)

이 프로젝트는 온프레미스 논문 분석 RAG 시스템이다. 전체 설계는 `docs/design/DESIGN.md`,
문서 형식은 `docs/guide/README.md`를 따른다.

## 반드시 지킬 것

1. **git 명령 금지** — add/commit/push 하지 않는다. 파일 작성/수정만 수행한다.
2. **네트워크 사용 금지** — 패키지 설치(pip/npm) 시도하지 않는다. 파일 작성만으로 태스크를 완료한다.
3. **Python 3.12 + 타입 힌트 필수**, 패키지 루트는 `src/paperrag/`.
4. 무거운 의존성(docling, paddleocr, sentence-transformers, kiwipiepy)은 **함수/메서드 내부 지연 임포트**
   또는 try-import로 처리 — 코어 패키지는 fastapi/sqlalchemy/httpx/openpyxl 수준만으로 임포트돼야 한다.
5. 설정값은 전부 `paperrag.config.Settings`(pydantic-settings) 경유. 하드코딩 금지.
6. 테스트는 pytest, 외부 서비스 없이 실행 가능해야 한다 (DB는 페이크 리포지토리, LLM/임베딩은 목).
7. 주석은 코드로 알 수 없는 제약만. 한국어 docstring 허용.
8. 태스크 스펙에 명시된 파일 외의 기존 파일을 수정하지 않는다 (특히 CLAUDE.md, docs/design/, docs/adr/).
