import argparse
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from paperrag.config import get_settings
from paperrag.ingest.embeddings import FakeEmbeddingClient, HttpEmbeddingClient
from paperrag.ingest.layout import get_backend
from paperrag.ingest.llm_enrich import OllamaClient, PassthroughEnricher
from paperrag.ingest.models import IngestReport
from paperrag.ingest.pipeline import IngestPipeline
from paperrag.ingest.repository import InMemoryIngestRepository, PostgresIngestRepository


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m paperrag.ingest")
    parser.add_argument("path", help="PDF 파일 또는 PDF 디렉터리")
    parser.add_argument(
        "--backend",
        choices=["paddle", "simple", "docling"],
        default=settings.ingest_backend,
        help="운영 기본값은 모든 PDF를 OCR 처리하는 paddle입니다.",
    )
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.skip_llm and not (args.dry_run or settings.allow_degraded_results):
        parser.error(
            "--skip-llm은 dry-run 또는 PAPERRAG_ALLOW_DEGRADED_RESULTS=true에서만 허용됩니다."
        )
    if args.backend != "paddle" and not (
        args.dry_run or settings.allow_diagnostic_backends
    ):
        parser.error(
            "운영 적재는 paddle만 허용합니다. 진단 backend는 "
            "PAPERRAG_ALLOW_DIAGNOSTIC_BACKENDS=true에서만 사용하세요."
        )

    pdf_paths = _resolve_pdf_paths(Path(args.path))
    repo = InMemoryIngestRepository() if args.dry_run else PostgresIngestRepository()
    layout_backend = get_backend(args.backend)
    llm = PassthroughEnricher() if args.skip_llm else OllamaClient()
    embedder = FakeEmbeddingClient() if args.dry_run else HttpEmbeddingClient()
    pipeline = IngestPipeline(repo, layout_backend, llm, embedder)

    reports: list[IngestReport] = []
    failures: list[tuple[str, str]] = []
    for pdf_path in pdf_paths:
        try:
            reports.append(pipeline.run(str(pdf_path)))
        except Exception as exc:
            failures.append((str(pdf_path), str(exc)))

    print(_format_reports(reports, failures))
    _append_batch_report(reports, failures)
    return 1 if failures else 0


def _resolve_pdf_paths(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(item for item in path.iterdir() if item.suffix.lower() == ".pdf")
    return [path]


def _format_reports(reports: Sequence[IngestReport], failures: Sequence[tuple[str, str]]) -> str:
    rows = [["source", "paper_id", "paragraphs", "keywords", "tables", "relations", "status"]]
    for report in reports:
        status = "failed" if report.errors else "done"
        rows.append(
            [
                report.source_path,
                str(report.paper_id or ""),
                str(report.totals.get("paragraphs", 0)),
                str(report.totals.get("keywords", 0)),
                str(report.totals.get("tables", 0)),
                str(report.totals.get("relations", 0)),
                status,
            ]
        )
    for source, error in failures:
        rows.append([source, "", "0", "0", "0", "0", f"failed: {error}"])

    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    lines = []
    for row_index, row in enumerate(rows):
        lines.append(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
        if row_index == 0:
            lines.append("-+-".join("-" * width for width in widths))
    return "\n".join(lines)


def _append_batch_report(
    reports: Sequence[IngestReport],
    failures: Sequence[tuple[str, str]],
) -> None:
    report_dir = Path("docs/reports/ingest")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{datetime.now().date().isoformat()}.md"
    stage_counts: Counter[str] = Counter()
    stage_failures: Counter[str] = Counter()

    for report in reports:
        for stage, stage_report in report.stages.items():
            if stage_report.status == "done":
                stage_counts[stage] += 1
            elif stage_report.status == "failed":
                stage_failures[stage] += 1

    lines = [
        "",
        f"## {datetime.now().isoformat(timespec='seconds')} 배치",
        "",
        f"- 처리 건수: {len(reports)}",
        f"- 실패 건수: {len(failures)}",
        "",
        "| 단계 | 성공 | 실패 |",
        "| --- | ---: | ---: |",
    ]
    for stage in sorted(set(stage_counts) | set(stage_failures)):
        lines.append(f"| {stage} | {stage_counts[stage]} | {stage_failures[stage]} |")
    if failures:
        lines.extend(["", "| 파일 | 실패 원인 |", "| --- | --- |"])
        for source, error in failures:
            lines.append(f"| `{source}` | {error.replace('|', '/')} |")
    report_path.write_text(report_path.read_text(encoding="utf-8") + "\n".join(lines) + "\n", encoding="utf-8") if report_path.exists() else report_path.write_text("\n".join(lines).lstrip() + "\n", encoding="utf-8")
