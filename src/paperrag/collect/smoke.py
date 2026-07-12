from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from paperrag.config import Settings, get_settings


def prepare_smoke_set(settings: Settings | None = None) -> list[Path]:
    """수집 논문의 앞부분만 복제해 빠른 OCR 경로 점검용 PDF를 만든다."""
    configured = settings or get_settings()
    if configured.paper_smoke_pages < 1:
        raise ValueError("PAPERRAG_PAPER_SMOKE_PAGES는 1 이상이어야 합니다.")

    import pymupdf  # type: ignore[import-not-found]

    source_manifest = (
        configured.paper_collection_dir / configured.paper_collection_manifest_name
    )
    if not source_manifest.is_file():
        raise FileNotFoundError(source_manifest)
    configured.paper_smoke_dir.mkdir(parents=True, exist_ok=True)
    output_records: list[dict[str, Any]] = []
    output_paths: list[Path] = []
    for record in _read_manifest(source_manifest):
        source_path = Path(str(record["local_path"]))
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        source = pymupdf.open(source_path)
        destination = pymupdf.open()
        try:
            page_count = min(configured.paper_smoke_pages, len(source))
            destination.insert_pdf(source, from_page=0, to_page=page_count - 1)
            output_path = configured.paper_smoke_dir / (
                f"{record['source_id']}-smoke-{page_count}p.pdf"
            )
            destination.save(output_path)
        finally:
            destination.close()
            source.close()
        output_paths.append(output_path)
        output_records.append(
            {
                **record,
                "local_path": str(output_path),
                "sha256": _sha256(output_path),
                "byte_size": output_path.stat().st_size,
                "derived_from_sha256": record["sha256"],
                "page_range": f"1-{page_count}",
                "purpose": "pipeline-smoke-test-only",
            }
        )
    _write_manifest(
        configured.paper_smoke_dir / configured.paper_collection_manifest_name,
        output_records,
    )
    return output_paths


def main() -> int:
    for path in prepare_smoke_set():
        print(path)
    return 0


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest {line_number}행이 올바르지 않습니다.") from exc
        if not isinstance(value, dict) or "source_id" not in value or "sha256" not in value:
            raise ValueError(f"manifest {line_number}행에 출처 필드가 없습니다.")
        records.append(value)
    return records


def _write_manifest(path: Path, records: list[dict[str, Any]]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".part")
    with temporary_path.open("w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary_path.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
