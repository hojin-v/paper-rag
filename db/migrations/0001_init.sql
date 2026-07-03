CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE papers (
    paper_id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT,
    published_year INTEGER,
    journal TEXT,
    abstract TEXT,
    abstract_summary TEXT,
    full_text_link TEXT,
    source_file_path TEXT,
    paper_embedding VECTOR(1024),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE paragraphs (
    paragraph_id BIGSERIAL PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    section_name TEXT,
    paragraph_order INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    cleaned_text TEXT,
    summary TEXT,
    is_topic_relevant BOOLEAN NOT NULL DEFAULT true,
    embedding VECTOR(1024)
);

CREATE TABLE keywords (
    keyword_id BIGSERIAL PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,
    display_form TEXT NOT NULL,
    frequency INTEGER NOT NULL DEFAULT 1,
    embedding VECTOR(1024)
);

CREATE TABLE keyword_aliases (
    alias TEXT PRIMARY KEY,
    keyword_id BIGINT NOT NULL REFERENCES keywords(keyword_id) ON DELETE CASCADE
);

CREATE TABLE paper_keywords (
    paper_id BIGINT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    keyword_id BIGINT NOT NULL REFERENCES keywords(keyword_id) ON DELETE CASCADE,
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (paper_id, keyword_id)
);

CREATE TABLE paper_tables (
    table_id BIGSERIAL PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    table_title TEXT,
    table_text TEXT NOT NULL,
    table_summary TEXT,
    embedding VECTOR(1024)
);

CREATE TABLE paper_relations (
    source_paper_id BIGINT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    related_paper_id BIGINT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    relation_score DOUBLE PRECISION NOT NULL,
    relation_reason TEXT,
    PRIMARY KEY (source_paper_id, related_paper_id),
    CHECK (source_paper_id <> related_paper_id)
);

CREATE TABLE processing_jobs (
    job_id BIGSERIAL PRIMARY KEY,
    paper_id BIGINT REFERENCES papers(paper_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE search_results (
    result_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    match_type TEXT NOT NULL,
    matched_keyword_id BIGINT REFERENCES keywords(keyword_id),
    primary_paper_id BIGINT REFERENCES papers(paper_id),
    related_paper_id BIGINT REFERENCES papers(paper_id),
    excel_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_paragraphs_embedding_hnsw
    ON paragraphs USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_keywords_embedding_hnsw
    ON keywords USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_paper_tables_embedding_hnsw
    ON paper_tables USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_paragraphs_paper_id ON paragraphs(paper_id);
CREATE INDEX idx_paper_keywords_keyword_id ON paper_keywords(keyword_id);
CREATE INDEX idx_paper_relations_source_paper_id ON paper_relations(source_paper_id);
