from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from paperrag.collect.models import PaperCandidate
from paperrag.collect.openalex import OpenAlexClient
from paperrag.collect.service import PaperCollector
from paperrag.config import get_settings


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m paperrag.collect")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--query", default=settings.paper_collection_query)
    source.add_argument("--work-id", action="append", dest="work_ids")
    parser.add_argument("--limit", type=int, default=settings.paper_collection_limit)
    parser.add_argument("--output", type=Path, default=settings.paper_collection_dir)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    discovery = OpenAlexClient(settings)
    collector = PaperCollector(discovery, settings)
    try:
        candidates = (
            discovery.get_works(args.work_ids)
            if args.work_ids
            else discovery.search(args.query, args.limit)
        )
        if args.dry_run:
            print(_format_candidates(candidates))
            return 0
        report = collector.collect_candidates(candidates, output_dir=args.output)
    finally:
        collector.close()

    for result in report.downloaded:
        print(f"downloaded | {result.candidate.license} | {result.local_path}")
    for result in report.skipped:
        print(f"skipped    | {result.candidate.license} | {result.local_path}")
    for source_id, error in report.failures:
        print(f"failed     | {source_id} | {error}")
    return 1 if report.failures else 0


def _format_candidates(candidates: list[PaperCandidate]) -> str:
    lines = ["source_id | license | year | title | pdf_url"]
    for candidate in candidates:
        lines.append(
            " | ".join(
                (
                    candidate.source_id,
                    candidate.license,
                    str(candidate.publication_year or ""),
                    candidate.title,
                    candidate.pdf_url,
                )
            )
        )
    return "\n".join(lines)
