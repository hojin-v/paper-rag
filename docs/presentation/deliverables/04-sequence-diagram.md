# 시퀀스 다이어그램 — 온프레미스 논문 분석 RAG

실제 라우트 함수명(`src/paperrag/review/api.py`, `src/paperrag/search/api.py`)과 Celery 태스크명
(`src/paperrag/worker/app.py`)을 그대로 사용한다.

## 1. 논문 등록·검수 흐름

```mermaid
sequenceDiagram
    actor R as 검수자
    participant API as review/api.py
    participant SVC as ReviewService
    participant LB as PaddleLayoutBackend
    participant STORE as PostgresReviewStore
    participant REDIS as Redis (broker)
    participant W as Celery worker
    participant DB as PostgreSQL+pgvector

    R->>API: POST /documents (PDF)
    API->>SVC: upload_document()
    SVC->>LB: 전 페이지 레이아웃 검출
    LB-->>SVC: 블록 좌표·유형·confidence
    SVC->>STORE: ReviewDocument 저장 (phase=layout_review)
    STORE-->>API: 저장 완료
    API-->>R: document_id, phase

    loop 레이아웃 검수
        R->>API: PUT /documents/{id}/blocks/{block_id}
        API->>STORE: bbox·block_type 갱신
        R->>API: DELETE /documents/{id}/blocks/{block_id}
        API->>STORE: 오검출 블록 삭제
    end

    R->>API: POST /documents/{id}/run-ocr/async
    API->>SVC: submit_reviewed_ocr()
    SVC->>REDIS: run_reviewed_ocr_task.delay(document_id)
    REDIS-->>W: 태스크 전달
    API-->>R: TaskSubmitted(task_id)
    R->>API: GET /jobs/{task_id}
    API-->>R: JobStatus(pending/started)

    W->>LB: 영역별 crop OCR 실행
    LB-->>W: ocr_text, confidence
    W->>STORE: phase=ocr_review 갱신
    Note over W,STORE: async 실패 시 API가 동기 경로(run_reviewed_ocr)로 폴백

    loop OCR 검수
        R->>API: PUT /documents/{id}/blocks/{block_id} (corrected_text)
        API->>STORE: 저장
        R->>API: POST /documents/{id}/approve-all
        API->>STORE: 남은 unreviewed 일괄 승인
    end

    R->>API: POST /documents/{id}/confirm-ocr
    API->>STORE: phase=ready_to_ingest
    API-->>R: ReviewDocument

    R->>API: POST /documents/{id}/ingest
    API->>SVC: ingest_document()
    SVC->>DB: papers/paragraphs/keywords/tables INSERT + 임베딩
    DB-->>SVC: paper_id
    SVC-->>R: IngestedDocument(paper_id, totals)
```

## 2. 검색 흐름

질의 키워드 추출은 매 검색마다 항상 LLM(Qwen2.5-7B)으로 한다(형태소 분석은 LLM 실패 시의 내부
폴백일 뿐 사용자 선택 경로가 아니다). 대표/연관 논문이 정해지면 각 논문의 근거 단락 1개를 기반으로
"왜 이 논문이 질의와 관련 있는지" 짧은 설명을 LLM으로 생성한다(RAG 생성 단계, `relevance_summary`).

```mermaid
sequenceDiagram
    actor U as 검색 사용자
    participant UI as Streamlit UI
    participant API as search/api.py
    participant LLM as Ollama (Qwen2.5-7B)
    participant EMB as embedder (BGE-M3)
    participant DB as PostgreSQL+pgvector

    U->>UI: 자연어 질의 입력
    UI->>API: POST /search {query}
    API->>LLM: 질의 핵심 키워드 추출 (매 검색)
    LLM-->>API: keywords[]
    API->>DB: find_keyword_exact (keywords + keyword_aliases)

    alt 정확 매칭 성공
        DB-->>API: 매칭 keyword_id
        Note over API,DB: 기본 뷰는 keyword_result_cache 우선 조회<br/>(있으면 점수·생성·엑셀 재계산 생략)
        API->>EMB: 매칭 키워드 임베딩
        EMB-->>API: 질의 벡터(1024)
        API->>DB: 대표 논문 점수 계산(0.5 키워드+0.3 단락유사+0.1 제목/초록+0.1 연도)<br/>+ paper_relations 조회
        DB-->>API: 대표 논문 1편 + 연관 논문 1편
        loop 대표·연관 논문 각각 (RAG 생성)
            API->>DB: top_matching_paragraph (근거 단락 1개)
            API->>LLM: 관련도 설명 생성 (근거 단락 기반)
            LLM-->>API: relevance_summary
        end
        API->>DB: 엑셀 생성·result_id 저장 (기본 뷰면 캐시에도 저장)
        API-->>UI: SearchMatched (대표/연관 + relevance_summary)
    else 매칭 실패
        API->>EMB: 질의 임베딩
        EMB-->>API: 질의 벡터(1024)
        API->>DB: 키워드 임베딩 코사인 유사도 Top-3
        DB-->>API: 후보 키워드 3개
        API-->>UI: SearchSuggest(session_id, candidates)
        UI->>U: 유사 키워드 선택 요청
        U->>UI: 키워드 선택
        UI->>API: POST /search/select {session_id, keyword_id}
        Note over API,LLM: 이후 대표/연관 선정·관련도 설명 생성은<br/>정확 매칭 경로와 동일
        API-->>UI: SearchMatched (대표/연관 + relevance_summary)
    end

    UI-->>U: 결과 카드 표시 (관련도 설명 포함)
    U->>UI: 엑셀 다운로드 클릭
    UI->>API: GET /result/{result_id}/excel
    API-->>UI: xlsx (최대 9시트)
    UI-->>U: 파일 다운로드
```
