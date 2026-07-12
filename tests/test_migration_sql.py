from pathlib import Path
import re

MIGRATION_SQL = (
    Path(__file__).resolve().parents[1] / "db" / "migrations" / "0001_init.sql"
).read_text(encoding="utf-8")
SQL_LOWER = MIGRATION_SQL.lower()
ENRICHMENT_SQL = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "0002_paragraph_keywords.sql"
).read_text(encoding="utf-8").lower()

EXPECTED_TABLES = [
    "papers",
    "paragraphs",
    "keywords",
    "keyword_aliases",
    "paper_keywords",
    "paper_tables",
    "paper_relations",
    "processing_jobs",
    "search_results",
]


def test_all_tables_are_created() -> None:
    for table in EXPECTED_TABLES:
        pattern = rf"create\s+table\s+{re.escape(table)}\s*\("
        assert re.search(pattern, SQL_LOWER), f"missing CREATE TABLE for {table}"


def test_vector_1024_columns_exist() -> None:
    assert "paper_embedding vector(1024)" in SQL_LOWER
    assert SQL_LOWER.count("embedding vector(1024)") >= 3


def test_hnsw_indexes_exist() -> None:
    hnsw_indexes = re.findall(
        r"create\s+index\s+\S+\s+on\s+\S+\s+using\s+hnsw",
        SQL_LOWER,
    )

    assert len(hnsw_indexes) == 3
    for table in ("paragraphs", "keywords", "paper_tables"):
        pattern = rf"on\s+{table}\s+using\s+hnsw\s*\([^)]*vector_cosine_ops"
        assert re.search(pattern, SQL_LOWER), f"missing HNSW vector_cosine_ops index for {table}"


def test_vector_extension_is_created() -> None:
    assert "create extension if not exists vector" in SQL_LOWER


def test_paragraph_keywords_migration_exists() -> None:
    assert "alter table paragraphs" in ENRICHMENT_SQL
    assert "keywords text[]" in ENRICHMENT_SQL
