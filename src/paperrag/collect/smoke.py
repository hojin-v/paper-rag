"""
CPU smoke test용 합성(축소) PDF 생성기.

수집된 논문은 보통 11~19페이지라 CPU에서 전 페이지 OCR을 돌리면 시간이 오래 걸린다
(docs/guide/11-paper-collection.md 4단계). 이 모듈은 이미 다운로드된 원본 PDF의 앞
`paper_smoke_pages`페이지만 잘라 별도 디렉터리(`data/inbox/smoke`)에 저장해, "파이프라인이
배선대로 동작하는지"만 빠르게 확인할 수 있게 한다. smoke 출력은 원본의 파생본임을 manifest에
명시(`derived_from_sha256`, `purpose=pipeline-smoke-test-only`)하고, 실제 논문 품질 지표로
사용하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from paperrag.config import Settings, get_settings


def prepare_smoke_set(settings: Settings | None = None) -> list[Path]:
    """수집 논문의 앞부분만 복제해 빠른 OCR 경로 점검용 PDF를 만든다.

    `paper_collection_dir`의 manifest를 읽어 각 원본 PDF에서 앞 `paper_smoke_pages`페이지만
    잘라낸 새 PDF를 `paper_smoke_dir`에 만들고, 파생 관계를 기록한 별도 manifest를 함께 쓴다.
    부수효과: 파일 시스템에 PDF와 manifest를 새로 쓴다(원본은 건드리지 않음).
    """
    configured = settings or get_settings()
    if configured.paper_smoke_pages < 1:
        raise ValueError("PAPERRAG_PAPER_SMOKE_PAGES는 1 이상이어야 합니다.")

    # pymupdf는 선택적 의존성(.[ingest])이라 함수 내부에서 지연 임포트한다 — 코어 패키지는 이
    # 무거운 의존성 없이도 임포트 가능해야 한다(CLAUDE.md 코드 규칙).
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
            # 논문이 smoke 페이지 수보다 짧을 수 있으므로 min()으로 실제 페이지 수를 넘지 않게 한다.
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
                # 원본 파일의 체크섬을 남겨 "이 smoke 파일이 어느 원본에서 파생됐는지" 추적 가능하게
                # 한다. purpose 필드는 이 파일을 실제 품질 지표로 오인하지 않도록 하는 명시적 표식.
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
    """`python -m paperrag.collect.smoke` 진입점: smoke PDF를 생성하고 경로를 한 줄씩 출력한다."""
    for path in prepare_smoke_set():
        print(path)
    return 0


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    """JSONL manifest를 줄 단위로 파싱한다. 손상된 줄이나 필수 출처 필드 누락은 즉시 예외로 알린다."""
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
    """manifest를 임시 파일에 먼저 쓴 뒤 원자적으로 교체해, 쓰기 도중 프로세스가 죽어도
    기존 manifest가 반쪽짜리 내용으로 덮이지 않게 한다."""
    temporary_path = path.with_suffix(path.suffix + ".part")
    with temporary_path.open("w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary_path.replace(path)


def _sha256(path: Path) -> str:
    """파일 전체를 청크 단위(1MB)로 읽어 SHA-256을 계산한다. 큰 PDF도 메모리에 한 번에 올리지 않는다."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
