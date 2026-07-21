-- papers.status 제거.
-- 이 컬럼은 항상 상수 'ingested'로만 쓰이고 다른 값으로 전이하지도, 로직에서 읽히지도
-- 않는 흔적 컬럼이었다(2026-07-21 스키마 감사). 논문 처리 상태는 processing_jobs(단계별)와
-- review_documents.status(검수 문서)가 이미 관리하므로 papers.status는 중복이다.
ALTER TABLE papers DROP COLUMN IF EXISTS status;
