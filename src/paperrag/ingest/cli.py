"""수집 파이프라인 CLI 진입점: python -m paperrag.ingest.

PDF 파일/디렉터리를 받아 STEP 1~8 IngestPipeline을 파일마다 실행하고, 콘솔 표
출력과 docs/reports/ingest/YYYY-MM-DD.md 배치 리포트를 남긴다. dry-run/skip-llm/
backend 옵션에 대한 운영 안전장치(운영 모드에서 진단 backend·LLM 생략 금지)도
여기서 강제한다(DESIGN.md §3, docs/guide/04-ingest-pipeline.md 7단계).
"""

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
    """CLI 인자를 파싱해 대상 PDF들에 대해 IngestPipeline을 순차 실행한다.

    --skip-llm과 진단 backend(simple/docling)는 운영 오적재를 막기 위해 dry-run
    이거나 명시적으로 허용 환경변수(PAPERRAG_ALLOW_DEGRADED_RESULTS,
    PAPERRAG_ALLOW_DIAGNOSTIC_BACKENDS)가 켜져 있을 때만 허용한다. 파일 하나가
    실패해도 나머지 파일 처리를 계속하고, 마지막에 실패 건수가 있으면 종료 코드
    1을 반환해 배치 스크립트가 실패를 감지할 수 있게 한다.
    """
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
    """입력 경로가 디렉터리면 그 안의 *.pdf 전체(정렬), 파일이면 그 파일 하나만 반환."""
    if path.is_dir():
        return sorted(item for item in path.iterdir() if item.suffix.lower() == ".pdf")
    return [path]


def _format_reports(reports: Sequence[IngestReport], failures: Sequence[tuple[str, str]]) -> str:
    """실행 결과를 사람이 읽기 좋은 고정폭 표 문자열로 만들어 콘솔에 출력한다."""
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
    """이번 배치 실행 결과를 docs/reports/ingest/YYYY-MM-DD.md에 append 형식으로 남긴다.

    같은 날짜에 여러 번 배치를 돌려도 파일을 덮어쓰지 않고 이어 붙이며(운영에서
    하루에 여러 배치를 실행할 수 있음을 고려), 단계별 성공/실패 건수와 실패 파일별
    원인을 표로 기록한다.
    """
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
