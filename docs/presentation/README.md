# 발표자료 구성

`PRESENTATION.md`는 **최종 발표 가이드의 8단계 구조**(①기획개요 ②생성형AI핵심기술 ③아키텍처·설계도
④오픈소스화 ⑤프로젝트 핵심 체크포인트 ⑥형상관리 ⑦실시간 솔루션 시연 ⑧향후 발전방향·소회)를
따른다. 25장, 10분 이내 발표 기준.

| 파일 | 용도 |
| --- | --- |
| `PRESENTATION.md` | 화면에 표시하는 25장 발표 슬라이드(8단계 구조) |
| `SPEAKER_NOTES_AND_QA.md` | 슬라이드별 발표자 설명과 평가자 예상 질문·답변(슬라이드 번호 1:1 대응) |
| `video-script.md` | 5분 이내 프로젝트 소개 영상 촬영 구성안(타임 버짓·콘티·내레이션) — 25장 전체가 아니라 압축된 별도 흐름 |
| `assets/architecture-user.svg` | **사용자 아키텍처** — 등록·검수/검색 두 사용자 흐름, 흰 배경 인포그래픽 양식 |
| `assets/architecture-ai.svg` | **AI 아키텍처** — 레이아웃·OCR·LLM·임베딩 모델 파이프라인 + 검색시점 RAG 생성 단계 |
| `assets/architecture-system.svg` | **시스템 아키텍처** — 클라이언트·서비스·저장/모델 서빙 3계층 |
| `assets/architecture.svg`, `assets/user-flows.svg`, `assets/model-routing.svg` | (구버전, 어두운 배경) — 더 이상 `PRESENTATION.md`/`video-script.md` 어디에서도 쓰지 않음. 이력 보존용으로만 남김 |

## 산출물 (`deliverables/`)

| 파일 | 용도 |
| --- | --- |
| `deliverables/01-research.md` | 자료조사 — 모델·저장소·데이터 소스 비교 및 선정 근거 |
| `deliverables/02-requirements-spec.md` | 요구사항명세서 (기능/비기능/외부 인터페이스/제약사항) |
| `deliverables/03-wbs.md` | WBS — 작업분류체계 표 + 간트 요약 |
| `deliverables/04-sequence-diagram.md` | 시퀀스 다이어그램 — 등록·검수 흐름 / 검색 흐름 |
| `deliverables/05-deployment-diagram.md` | 배치 다이어그램 — docker-compose 서비스 + CD 파이프라인 |
| `deliverables/06-evaluation-metrics.md` | 평가지표 자료 — 합격선, 현재 측정 상태, 실측 사례 |

`architecture-*.svg` 3종은 지정 인포그래픽 스타일(흰 배경, 카드형 박스, 01/02/03 pill,
실선=요청/점선=회신)을 따르며, **슬라이드·영상 양쪽 모두 이 3종으로 통일**했다(2026-07-22).
`architecture-ai.svg`는 "매 검색 항상 LLM 키워드 추출 + 대표/연관 논문 관련도 설명 생성(RAG)"까지
반영한다(과거의 kiwipiepy/LLM 이중 토글은 제거됨). 구버전 어두운 배경 세트는 더 이상 참조되지 않는다.

## 사용 원칙

- 발표 화면에는 `PRESENTATION.md`만 사용한다.
- 긴 설명은 슬라이드에 추가하지 않고 `SPEAKER_NOTES_AND_QA.md`에서 발표자가 말한다.
- 평가자가 기능 범위, 모델 근거, CPU 가능성, 품질 검증을 질문하면 해당 슬라이드의 Q&A를 사용한다.
- 현재 구현과 다음 목표를 섞어 말하지 않는다. `현재와 목표·우선순위` 슬라이드에서 명시적으로 구분한다.

## 렌더링

`PRESENTATION.md`는 Marp 형식이다. Marp를 지원하는 편집기에서 열면 16:9 슬라이드로 발표하거나
PDF/PPTX로 내보낼 수 있다. Marp가 없어도 일반 Markdown 문서로 내용과 이미지를 확인할 수 있다.
