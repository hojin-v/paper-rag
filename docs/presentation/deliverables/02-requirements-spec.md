# 요구사항명세서 — 온프레미스 논문 분석 RAG

상태 표기: ✅ 구현+검증 완료 · 🔶 부분 구현/실측 필요 · 📋 계획만 존재
(전체 구현-검증 매핑은 `docs/design/REQUIREMENTS-TRACE.md` 참고)

## 1. 개요

### 1.1 목적

비정형 PDF 논문을 OCR·레이아웃 분석으로 구조화해 PostgreSQL+pgvector에 저장하고, 자연어 질의에
대해 대표 논문 1편과 연관 논문 1편을 엑셀로 반환하는 온프레미스(폐쇄망) 시스템을 구축한다.

### 1.2 범위

- 포함: PDF 수집·OCR·검수, 단락화·요약·키워드 추출, 임베딩·연관도 계산, 키워드 기반 검색, 엑셀 출력
- 포함(생성): 대표·연관 논문마다 근거 단락 1개에 기반해 "왜 이 논문이 질의와 관련 있는지"를 생성하는
  단답형 관련도 설명(RAG 생성 단계)
- 제외(현재 단계): 대화형(멀티턴) 답변 생성, 다국어(한/영 외) 지원, 실시간 다중 사용자 동시성 보장

### 1.3 용어 정의

| 용어 | 정의 |
| --- | --- |
| 검수(Review) | 자동 레이아웃·OCR 결과를 사람이 확인·교정하는 단계 (`layout_review` → `ocr_review` → `ready_to_ingest`) |
| 대표 논문 | 질의 키워드와 가장 관련성 높은 논문 1편 |
| 연관 논문 | 대표 논문과 사전 계산된 연관도가 가장 높은 논문 1편 |
| 게이트 방식 | 정량 지표가 합격선 미달일 때만 파인튜닝을 수행하는 원칙 (§6, DESIGN.md) |

## 2. 기능 요구사항 (FR)

### 2.1 수집·데이터 구축

| ID | 요구사항 | 우선순위 | 상태 |
| --- | --- | --- | --- |
| FR-01 | 모든 비정형 PDF를 전 페이지 이미지 기반 OCR로 처리한다 | 필수 | ✅ |
| FR-02 | 사전학습 레이아웃·OCR 모델 조합으로 문서를 분석한다 | 필수 | 🔶 |
| FR-03 | 제목/저자/본문/표/참고문헌 등 12개 블록 유형을 구분한다 | 필수 | 🔶 |
| FR-04 | 그림·독립 수식·참고문헌·부록은 저장 대상에서 제외한다 | 필수 | ✅ |
| FR-05 | 본문을 단락 단위로 분리한다(병합·분할·섹션 귀속 포함) | 필수 | ✅ |
| FR-06 | 표 영역을 구조화된 텍스트로 추출하고 요약한다 | 필수 | 🔶 |
| FR-07 | 단락별 원문·정제문·요약·키워드를 생성한다 | 필수 | 🔶 |
| FR-08 | 논문 단위 대표 키워드 3~5개를 추출한다 | 필수 | ✅ |
| FR-09 | 구조화 결과를 RDB(현재 11개 테이블)에 저장한다 | 필수 | ✅ |
| FR-10 | 단락·키워드 임베딩을 Vector 컬럼(HNSW)에 저장한다 | 필수 | 🔶 |
| FR-11 | 검수 화면에서 레이아웃 박스·유형·OCR 텍스트를 사람이 확인·교정한다 | 필수 | ✅ |
| FR-12 | 검수 상태 기계(layout_review→ocr_review→ready_to_ingest)를 API로 전이한다 | 필수 | ✅ |

### 2.2 검색 사용자 시나리오

| ID | 요구사항 | 우선순위 | 상태 |
| --- | --- | --- | --- |
| FR-13 | 자연어 질의에서 핵심 키워드를 추출한다(매 검색 항상 LLM) | 필수 | 🔶 |
| FR-14 | 키워드 정확 매칭을 우선 시도한다(별칭 포함) | 필수 | ✅ |
| FR-15 | 정확 매칭 성공 시 대표 논문 1편 + 연관 논문 1편을 반환한다 | 필수 | ✅ |
| FR-16 | 대표·연관 논문마다 근거 단락 1개 기반 관련도 설명을 생성한다(RAG 생성 단계) | 필수 | ✅ |
| FR-17 | 매칭 실패 시 벡터 유사 키워드 Top-3를 제시하고 사용자가 선택한다 | 필수 | ✅ |
| FR-18 | 검색 결과를 엑셀(설계상 6범주, 실제 최대 9시트)로 출력한다 | 필수 | ✅ |
| FR-19 | 웹 UI에서 질의·결과 확인·엑셀 다운로드를 수행한다 | 필수 | ✅ |

### 2.3 운영

| ID | 요구사항 | 우선순위 | 상태 |
| --- | --- | --- | --- |
| FR-20 | 대량 배치 처리 파이프라인과 작업 상태 기록(`processing_jobs`)을 제공한다 | 중요 | 🔶 |
| FR-21 | 구현 과정을 단계별 가이드 문서로 남긴다 | 중요 | 🔶 |

## 3. 비기능 요구사항 (NFR)

| ID | 요구사항 | 근거/기준 |
| --- | --- | --- |
| NFR-01 | 폐쇄망(온프레미스) 환경에서 전 기능이 동작해야 한다 | docker-compose 이미지 tar + 모델 파일 번들 반입 |
| NFR-02 | CPU 우선 실행 — GPU 없이 전 기능 동작 | Paddle mobile, Ollama Q4, BGE-M3 CPU 추론 확인 |
| NFR-03 | 상용 이용 가능한 라이선스만 운영 경로에 사용 | Apache-2.0/MIT/BSD만 채택, CC BY-NC·AGPL·GPL 배제 |
| NFR-04 | 레이아웃 분류 정확도 | 클래스별 mAP@0.5 ≥ 0.85 |
| NFR-05 | 읽기 순서 복원 정확도 | 단락 순서 일치율 ≥ 0.95 |
| NFR-06 | OCR 인식 정확도 | CER(LLM 정제 후 잔존 기준) ≤ 3% |
| NFR-07 | 표 구조 인식 정확도 | TEDS ≥ 0.85 |
| NFR-08 | 종단 단락 추출 정확도 | F1 ≥ 0.90 |
| NFR-09 | 측정 없이 파인튜닝하지 않는다(게이트 방식) | DESIGN.md §6 |
| NFR-10 | 학습/평가 데이터는 논문 단위로 분리한다(데이터 누수 차단) | DESIGN.md §7 |
| NFR-11 | 저장·응답 실패는 `/ready`로 명시적으로 드러나야 한다 | 모델·DB·LLM 누락 시 실패 응답 |

## 4. 외부 인터페이스 요구사항

### 4.1 검수 API

| API | 용도 |
| --- | --- |
| `POST /documents` | PDF 업로드 및 레이아웃 분석 시작 |
| `GET /documents/{document_id}` | 문서 상태·블록 목록 조회 |
| `GET /documents/{document_id}/viewer` | 검수 화면(읽기 전용/교정 가능 모드) |
| `POST /documents/{document_id}/blocks` | 누락 레이아웃 영역 추가 |
| `PUT /documents/{document_id}/blocks/{block_id}` | 블록 유형·좌표·OCR 텍스트 교정 |
| `DELETE /documents/{document_id}/blocks/{block_id}` | 오검출 블록 삭제 |
| `POST /documents/{document_id}/run-ocr` | 승인된 레이아웃 기준 OCR 실행 |
| `POST /documents/{document_id}/auto-ocr` | 사람 승인 없이 OCR + 자동 품질 판정 |
| `POST /documents/{document_id}/approve-all` | 남은 미검수 블록 일괄 승인 |
| `POST /documents/{document_id}/confirm-ocr` | OCR 검수 완료, `ready_to_ingest` 전이 |
| `POST /documents/{document_id}/ingest` | DB·pgvector 적재 |

### 4.2 검색 API

| API | 요청 | 응답 |
| --- | --- | --- |
| `POST /search` | `{query}` | `matched`(결과) 또는 `suggest`(유사 키워드 3개 + `session_id`) |
| `POST /search/select` | `{session_id, keyword_id}` | 최종 결과 |
| `GET /result/{result_id}/excel` | - | xlsx 다운로드 |

## 5. 제약사항

- 라이선스 배제: LayoutLMv3(CC BY-NC), Surya/Marker(상용 조건부), MinerU/DocLayout-YOLO(AGPL, 벤치마크 전용)
- 저작권 검토 미완료: 원문 단락 엑셀 재출력에 대한 연구목적 이용 범위·TDM 허용 여부·출처 표기 방식은
  법무 확정 전제(착수 전 외부 의존)
- 현재 하드웨어(RAM 7.5GiB급)에서 Paddle+BGE-M3+7B LLM 동시 상주 시 swap 포화 확인 — 단계별 모델
  수명 관리 필요(`docs/reports/assessments/2026-07-12-two-paper-ocr-evaluation.md` §4)

## 6. 추적성

전체 21개 기능 요구사항의 구현 위치·검증 방법·상태는 `docs/design/REQUIREMENTS-TRACE.md`에서
1:1로 추적한다. 본 문서의 FR 번호는 추적표의 항목 번호와 대응한다.
