"""
논문 수집 CLI 진입점 (`python -m paperrag.collect`).

argparse로 검색어(`--query`) 또는 고정 OpenAlex work ID 목록(`--work-id`, 여러 번 지정 가능)
중 하나를 받아 OpenAlex에서 후보를 찾고, `--dry-run`이 아니면 실제로 PDF를 다운로드한다.
동일한 3편을 재현하는 고정 work-id 목록은 docs/guide/11-paper-collection.md 3단계에 기록돼 있다.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from paperrag.collect.models import PaperCandidate
from paperrag.collect.openalex import OpenAlexClient
from paperrag.collect.service import PaperCollector
from paperrag.config import get_settings


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점. 후보 발견 → (dry-run이면 출력만) → 다운로드 → 결과 요약 출력 순서로 동작한다.

    반환값은 셸 종료 코드로 쓰인다: 실패한 논문이 하나라도 있으면 1, 전부 성공(다운로드 또는
    이미 존재해 스킵)이면 0을 반환해 배치 스크립트가 실패를 감지할 수 있게 한다.
    """
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m paperrag.collect")
    # --query와 --work-id는 동시에 줄 수 없다 — 주제 검색과 고정 논문 재현은 서로 다른 발견
    # 경로이기 때문(mutually_exclusive_group).
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
            # dry-run은 파일을 하나도 저장하지 않고 후보 목록만 사람이 확인할 수 있게 출력한다
            # (docs/guide/11-paper-collection.md 2단계 "OpenAlex 무료 키 설정" 검증 절차).
            print(_format_candidates(candidates))
            return 0
        report = collector.collect_candidates(candidates, output_dir=args.output)
    finally:
        # discovery/download에 쓰인 httpx.Client를 확실히 닫아 커넥션이 남지 않게 한다.
        collector.close()

    for result in report.downloaded:
        print(f"downloaded | {result.candidate.license} | {result.local_path}")
        # 이번에 새로 받은 논문만 자동 적재 큐에 넣는다 — skipped(이미 전에 받은
        # 논문)는 처음 받았을 때 이미 큐에 들어갔을 것이므로 중복 적재를 피한다.
        _enqueue_ingest(result.local_path)
    for result in report.skipped:
        print(f"skipped    | {result.candidate.license} | {result.local_path}")
    for source_id, error in report.failures:
        print(f"failed     | {source_id} | {error}")
    return 1 if report.failures else 0


def _enqueue_ingest(source_path: str) -> None:
    """새로 다운로드한 논문 1편을 STEP 1~8 자동 적재 대기열(Celery)에 넣는다.

    worker 익스트라(Celery)가 설치되어 있지 않거나 브로커(Redis)에 연결할 수
    없어도 수집 자체는 이미 성공적으로 끝났으므로, 여기서는 예외를 삼키고
    경고만 출력한다 — 수집 성공 여부와 적재 큐 등록 성공 여부는 서로 독립적인
    실패 단위여야 하고, 큐 등록 실패 때문에 이미 받아둔 PDF까지 실패로 취급하면
    안 되기 때문이다.
    """
    try:
        from paperrag.worker.app import ingest_collected_paper

        ingest_collected_paper.delay(source_path)
    except Exception as exc:
        print(f"경고: 자동 적재 큐 등록 실패({source_path}): {exc}")


def _format_candidates(candidates: list[PaperCandidate]) -> str:
    """dry-run 출력용으로 후보 목록을 파이프(|) 구분 텍스트 표로 만든다."""
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
