-- 검수 문서(ReviewDocument) 구조화 메타데이터 저장 테이블 — 로컬 review.json 파일
-- (FileReviewStore)에서 이전. 목적은 여러 API replica가 동시에 같은 문서를 읽고
-- 수정하고 쓸 때 생기는 경쟁 조건(lost update)을 트랜잭션 UPDATE로 막는 것이다
-- (review/store.py 참고). phase/status/paper_id는 목록 필터링·조인에 쓰는
-- 질의용 컬럼이고, document 컬럼이 ReviewDocument 전체를 담는 원본(source of
-- truth)이다 — 두 값은 매 저장마다 함께 갱신되어야 어긋나지 않는다.
--
-- 원본 PDF·페이지 PNG 같은 바이너리 자산은 이 마이그레이션 대상이 아니며
-- review_dir 아래 파일로 그대로 남는다 — 생성 시 한 번만 쓰이고 이후 수정되지
-- 않아 파일 기반 저장의 경쟁 조건 위험이 애초에 없기 때문이다.
CREATE TABLE review_documents (
    document_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    paper_id BIGINT REFERENCES papers(paper_id) ON DELETE SET NULL,
    document JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_review_documents_phase ON review_documents(phase);
CREATE INDEX idx_review_documents_created_at ON review_documents(created_at DESC);
