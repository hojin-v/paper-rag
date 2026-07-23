"""평가셋 15편(`docs/guide/14-evaluation-labeling.md`)의 OCR CER과 표 TEDS를 계산한다.

`scripts/export_ocr_evaluation.py`는 비교 데이터를 엑셀로 나열만 할 뿐 수치를 계산하지
않는다(이미 그 문서 §4에 공백으로 명시돼 있었음) — 이 스크립트가 그 공백을 메운다.

평가 대상은 `ready_to_ingest` 상태(정답 확정)에 도달한 문서만이며, DB 적재(`/ingest`)
여부는 상관없다(평가는 검수 문서 자체의 `effective_text`/`ocr_text`만으로 충분하다).
아직 진행 중인 문서는 건너뛰고 진행 상황만 리포트에 남긴다 — 에러로 죽지 않는다.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from paperrag.config import get_settings
from paperrag.eval.metrics import CerResult, TedsResult, compute_cer, compute_teds
from paperrag.review.models import ReviewDocument
from paperrag.review.store import PostgresReviewStore, ReviewStore

# docs/guide/14-evaluation-labeling.md "1단계"가 지정한 15편(영문 10 + 한글 5)의 document_id.
# 전체 문서 목록에는 과거 자동화 테스트용 소형 픽스처도 섞여 있어, 이 목록만 평가 대상이다.
TARGET_DOCUMENT_IDS: list[str] = [
    # 영문 10편
    "54ff233fef924b85804430fa34f1e856",
    "697b623244b0453b9da4112fdbc2bba9",
    "9ca0c0b1058c4032ba78a033abb62f7a",
    "691d37d52cc245dc8ef3cfdc51022598",
    "b9785bdf3c714b30b9fcb05359eeda96",
    "d41929a6189c4b498e059ec3c043a6ef",
    "efceadeb91da43dba3e6fd7536491d1a",
    "f1dad1a800fa4b309062a14ad0bdf4f2",
    "0a211a5adff744cd888693cb721ce90d",
    "e28e6ef79333415f939d71c9a3252852",
    # 한글 5편
    "b315718c146c498dbe011cfc16f67b6d",
    "f987c8eee13443b5be7b8791fd6b40b3",
    "0679a0f868504f82b1b0951232a4cd19",
    "cc42134c1c8b4bcfbba38360f7714ef5",
    "eaeee89f96034b868c95152e3ee7d7ef",
]

# figure/formula는 recognize_layout이 OCR 자체를 건너뛰어 텍스트가 항상 비므로 평가 대상이
# 아니다(paddle_backend.py::recognize_layout 참고). table은 CER이 아니라 TEDS로 따로 잰다.
_CER_EXCLUDED_TYPES = {"figure", "formula", "table"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="평가셋 15편의 OCR CER과 표 TEDS를 계산해 리포트를 만듭니다."
    )
    parser.add_argument("--output", type=Path, help="생성할 .md 리포트 경로")
    return parser.parse_args()


@dataclass
class DocumentVerification:
    """문서 하나가 실제로 원문 대조 교정을 거쳤는지 나타내는 지표.

    `corrected_text`가 채워져 있어도 `ocr_text`와 값이 같으면(무변화 저장, 또는
    단순 승인) "실제 교정"으로 치지 않는다 — approve-all 경로로 검수를 끝내면
    `effective_text == ocr_text`가 되어 CER이 0%로 나오는데, 이건 OCR이 완벽해서가
    아니라 애초에 대조 자체를 안 했기 때문이다(2026-07-23 실측: 완료 문서 2편 모두
    실제 교정 블록 0개). 이 필드가 없으면 그 사실이 리포트에 조용히 묻힌다.
    """

    document_id: str
    eligible_blocks: int
    verified_blocks: int


@dataclass
class EvaluationReport:
    """`collect_evaluation`의 결과 — 리포트 작성과 테스트가 공유하는 중간 산출물."""

    ready_documents: list[ReviewDocument]
    pending: list[tuple[str, str]]
    cer_result: CerResult
    teds_result: TedsResult
    verification: list[DocumentVerification]


def collect_evaluation(store: ReviewStore, document_ids: Sequence[str]) -> EvaluationReport:
    """대상 문서 중 `ready_to_ingest`인 것만 모아 CER/TEDS를 계산한다.

    아직 `ready_to_ingest`에 도달하지 못한 문서는 예외 없이 `pending`에 담아 건너뛴다 —
    라벨링이 진행형이라는 이 스크립트의 전제(docs/guide/14 §1)를 그대로 반영한다.
    """
    ready_documents: list[ReviewDocument] = []
    pending: list[tuple[str, str]] = []
    for document_id in document_ids:
        document = store.get(document_id)
        if document.phase == "ready_to_ingest":
            ready_documents.append(document)
        else:
            pending.append((document_id, document.phase))

    cer_pairs: list[tuple[str, str]] = []
    teds_pairs: list[tuple[str, str]] = []
    verification: list[DocumentVerification] = []
    for document in ready_documents:
        eligible = 0
        verified = 0
        for block in document.blocks:
            if block.review_status == "rejected":
                continue
            if block.block_type in _CER_EXCLUDED_TYPES:
                if block.block_type == "table":
                    teds_pairs.append((block.effective_text, block.ocr_text))
                continue
            cer_pairs.append((block.effective_text, block.ocr_text))
            eligible += 1
            if block.corrected_text is not None and block.corrected_text != block.ocr_text:
                verified += 1
        verification.append(
            DocumentVerification(
                document_id=document.document_id,
                eligible_blocks=eligible,
                verified_blocks=verified,
            )
        )

    return EvaluationReport(
        ready_documents=ready_documents,
        pending=pending,
        cer_result=compute_cer(cer_pairs),
        teds_result=compute_teds(teds_pairs),
        verification=verification,
    )


def main() -> int:
    args = parse_args()
    settings = get_settings()
    store = PostgresReviewStore(settings.review_dir, settings)
    report = collect_evaluation(store, TARGET_DOCUMENT_IDS)

    output = args.output or (
        settings.result_dir
        / "evaluations"
        / f"{datetime.now(UTC).date().isoformat()}-ocr-cer-teds.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_build_report(report), encoding="utf-8")

    print(f"완료 문서: {len(report.ready_documents)}/{len(TARGET_DOCUMENT_IDS)}")
    print(
        f"CER: {report.cer_result.cer:.2%} "
        f"({report.cer_result.block_count}개 블록, {report.cer_result.total_reference_chars}자)"
    )
    print(f"TEDS: {report.teds_result.teds:.4f} ({report.teds_result.table_count}개 표)")
    unverified = [v for v in report.verification if v.eligible_blocks > 0 and v.verified_blocks == 0]
    if unverified:
        print(
            "경고: 다음 문서는 실제 원문 대조 교정 블록이 0개입니다 — "
            "이 문서의 CER 기여분은 의미 있는 측정치가 아닙니다:"
        )
        for item in unverified:
            print(f"  - {item.document_id}")
    print(output.resolve())
    return 0


def _build_report(report: EvaluationReport) -> str:
    ready_documents = report.ready_documents
    pending = report.pending
    cer_result = report.cer_result
    teds_result = report.teds_result
    unverified = [v for v in report.verification if v.eligible_blocks > 0 and v.verified_blocks == 0]
    lines = [
        "# OCR CER · 표 TEDS 실측",
        "",
        "`docs/guide/14-evaluation-labeling.md`가 지정한 평가셋 15편(영문 10+한글 5) 중 정답",
        "확정(`ready_to_ingest`)된 문서만으로 계산한 잠정 수치다. 나머지 문서 라벨링이",
        "끝나는 대로 이 스크립트를 다시 돌리면 표본이 늘어난다. 목표 합격선은",
        "`docs/design/DESIGN.md` §6(CER ≤ 3%, TEDS ≥ 0.85).",
        "",
    ]
    if unverified:
        lines += [
            "> **주의**: 아래 문서는 대상 블록 중 `ocr_text`와 다른 `corrected_text`가 하나도",
            "> 없다 — 승인만 하고 원문 대조를 하지 않았을 가능성이 높다. 이런 문서는",
            "> `effective_text == ocr_text`가 되어 CER이 자동으로 0%로 나오는데, 이는 OCR",
            "> 정확도가 아니라 \"대조를 안 했다\"는 뜻이다. 아래 전체 수치에 이 문서들이",
            "> 섞여 있으면 실제보다 낮은(더 좋아 보이는) CER로 왜곡된다.",
            ">",
        ]
        for item in unverified:
            lines.append(f"> - `{item.document_id}` — 대상 블록 {item.eligible_blocks}개 중 교정 0개")
        lines.append("")
    lines += [
        "# 1단계: 라벨링 진행 상황",
        "",
        f"- 완료(`ready_to_ingest`): {len(ready_documents)}/{len(TARGET_DOCUMENT_IDS)}편",
    ]
    if pending:
        lines.append("- 미완료:")
        for document_id, phase in pending:
            lines.append(f"  - `{document_id}` — {phase}")
    if report.verification:
        lines += ["", "| document_id | 대상 블록 | 실제 교정 블록 |", "| --- | ---: | ---: |"]
        for item in report.verification:
            lines.append(
                f"| `{item.document_id}` | {item.eligible_blocks} | {item.verified_blocks} |"
            )
    lines += [
        "",
        "# 2단계: 결과",
        "",
        "| 지표 | 값 | 표본 | 합격선 |",
        "| --- | --- | --- | --- |",
        f"| OCR CER | {cer_result.cer:.2%} | 블록 {cer_result.block_count}개"
        f"({cer_result.total_reference_chars}자) | ≤ 3% |",
        f"| 표 TEDS | {teds_result.teds:.4f} | 표 {teds_result.table_count}개 | ≥ 0.85 |",
        "",
    ]
    if cer_result.blocks:
        worst = sorted(cer_result.blocks, key=lambda block: block.cer, reverse=True)[:5]
        lines += ["## CER이 가장 높은 블록 5개", "", "| CER | 정답 | 인식 결과 |", "| ---: | --- | --- |"]
        for block in worst:
            lines.append(
                f"| {block.cer:.1%} | {_truncate(block.reference)} | {_truncate(block.hypothesis)} |"
            )
        lines.append("")
    if teds_result.tables:
        worst_tables = sorted(teds_result.tables, key=lambda table: table.teds)[:3]
        lines += ["## TEDS가 가장 낮은 표", "", "| TEDS | 정답 표 | 인식 표 |", "| ---: | --- | --- |"]
        for table in worst_tables:
            lines.append(
                f"| {table.teds:.3f} | {_truncate(table.reference_html)} | {_truncate(table.hypothesis_html)} |"
            )
        lines.append("")
    lines += [
        "# 3단계: 측정 방법과 한계",
        "",
        "- **CER**: `jiwer.cer`(MIT)로 계산한 누적 편집거리 기반 문자 오류율. 비교 전 두 텍스트",
        "  모두 `paperrag.eval.metrics.normalize_text`로 정규화한다(유니코드 NFKC, 곡선",
        "  따옴표/대시 통일, 공백·줄바꿈 축약) — 사람이 PDF 원문을 복사-붙여넣기해 생기는",
        "  포맷 차이가 가짜 오류로 잡히지 않게 하기 위함이다. 실제 글자·자모 오인식은",
        "  정규화로 흡수되지 않으므로 이 수치에 그대로 반영된다.",
        "- **TEDS**: 표준 TEDS 정의(Tree-Edit-Distance-based Similarity)를 따르는 근사",
        "  구현이다. 저장된 `\"|\"`-구분 표 텍스트를 HTML로 재구성(`pipe_text_to_html`)한 뒤",
        "  트리 편집거리(`apted`, MIT)로 계산한다. **한계**: 병합 셀(colspan/rowspan)",
        "  재구성이 \"인접 셀 값 반복\" 휴리스틱에 의존해, 우연히 같은 값이 반복된 셀을",
        "  병합으로 오인할 수 있다 — 정답값·예측값 양쪽에 동일하게 적용되어 비교의",
        "  공정성은 유지된다.",
        "- figure/formula 블록은 애초에 OCR을 건너뛰므로(레이아웃만 평가 대상) 이 수치에서",
        "  제외했다.",
        "",
        "## 완료 체크리스트",
        "- [ ] 15편 전부 `ready_to_ingest` 상태다.",
        "- [ ] CER ≤ 3% 합격선을 충족했다.",
        "- [ ] 표 TEDS ≥ 0.85 합격선을 충족했다.",
    ]
    return "\n".join(lines) + "\n"


def _truncate(text: str, limit: int = 60) -> str:
    flattened = text.replace("\n", " ⏎ ").replace("|", "/")
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
