from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from paperrag.collect.smoke import prepare_smoke_set
from paperrag.config import Settings


def test_prepares_traced_single_page_smoke_pdf(tmp_path: Path) -> None:
    collection_dir = tmp_path / "collected"
    smoke_dir = tmp_path / "smoke"
    collection_dir.mkdir()
    source_path = collection_dir / "W123-paper.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((30, 40), "Page one")
    document.new_page().insert_text((30, 40), "Page two")
    document.save(source_path)
    document.close()
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    (collection_dir / "collection-manifest.jsonl").write_text(
        json.dumps(
            {
                "source_id": "W123",
                "title": "Test Paper",
                "license": "cc-by",
                "local_path": str(source_path),
                "sha256": source_sha,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        paper_collection_dir=collection_dir,
        paper_smoke_dir=smoke_dir,
        paper_smoke_pages=1,
    )

    paths = prepare_smoke_set(settings)

    assert len(paths) == 1
    with pymupdf.open(paths[0]) as smoke_document:
        assert len(smoke_document) == 1
        assert "Page one" in smoke_document[0].get_text()
    record = json.loads(
        (smoke_dir / "collection-manifest.jsonl").read_text(encoding="utf-8")
    )
    assert record["derived_from_sha256"] == source_sha
    assert record["purpose"] == "pipeline-smoke-test-only"
