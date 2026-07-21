-- paper_tables.embedding과 그 HNSW 인덱스 제거.
-- 표 임베딩은 적재 때마다 계산·저장되고 인덱스까지 유지됐지만 검색 어디에서도
-- 표 벡터 유사도 질의(embedding <=> ...)를 하지 않아, 계산·저장·인덱스 유지 비용을
-- 전부 낭비하고 있었다(2026-07-21 스키마 감사). 표는 항상 paper_id로만 조회한다
-- (search/repository.py tables_of). 나중에 표 의미 검색이 필요해지면 임베딩 컬럼과
-- 인덱스를 다시 추가하는 마이그레이션으로 되살린다.
DROP INDEX IF EXISTS idx_paper_tables_embedding_hnsw;
ALTER TABLE paper_tables DROP COLUMN IF EXISTS embedding;
