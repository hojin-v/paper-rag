from datetime import UTC, datetime
from pathlib import Path

from openpyxl import load_workbook

from paperrag.search.excel import build_excel
from paperrag.search.schemas import (
    PaperInfo,
    PaperSummary,
    ParagraphInfo,
    ResultBundle,
    TableInfo,
)


def test_build_excel_writes_six_sheets_and_core_values(tmp_path: Path) -> None:
    out_path = tmp_path / "result.xlsx"

    excel_path = build_excel(_bundle(), out_path)

    workbook = load_workbook(excel_path)
    assert workbook.sheetnames == [
        "검색 결과 요약",
        "대표 논문 정보",
        "대표 논문 단락",
        "연관 논문 정보",
        "연관 논문 단락",
        "표 데이터",
    ]

    summary = workbook["검색 결과 요약"]
    assert summary.freeze_panes == "A2"
    assert summary["A1"].font.bold
    assert summary["A1"].value == "질의"
    assert summary["A2"].value == "RAG 관련 논문"
    assert summary["B2"].value == "RAG"
    assert summary["D2"].value == "Primary RAG Paper"
    assert summary["E2"].value == "Related OCR Paper"

    primary_info = workbook["대표 논문 정보"]
    assert primary_info["A1"].value == "논문 ID"
    assert primary_info["B2"].value == "Primary RAG Paper"
    assert primary_info["H2"].value == "RAG, 검색"

    related_info = workbook["연관 논문 정보"]
    assert related_info["I1"].value == "연관 점수"
    assert related_info["I2"].value == 0.77
    assert related_info["J2"].value == "겹치는 키워드: RAG"

    primary_paragraphs = workbook["대표 논문 단락"]
    assert primary_paragraphs["A1"].value == "단락 번호"
    assert primary_paragraphs["C2"].value == "원문 단락"
    assert primary_paragraphs["D2"].value == "정제 단락"
    assert primary_paragraphs.column_dimensions["C"].width == 60
    assert primary_paragraphs.column_dimensions["D"].width == 60

    tables = workbook["표 데이터"]
    assert tables["A1"].value == "구분"
    assert tables["A2"].value == "대표"
    assert tables["C2"].value == "metric | value\nf1 | 0.90"


def _bundle() -> ResultBundle:
    return ResultBundle(
        result_id="r-20260704-abcdef12",
        query="RAG 관련 논문",
        matched_keyword="RAG",
        match_type="exact",
        primary_paper=PaperSummary(
            paper_id=10,
            title="Primary RAG Paper",
            authors="Kim; Lee",
            published_year=2025,
            journal="Journal",
            full_text_link="https://example.test/primary",
            keywords=["RAG", "검색"],
            score=0.89,
            reason="대표 점수=0.890",
        ),
        related_paper=PaperSummary(
            paper_id=30,
            title="Related OCR Paper",
            authors="Choi",
            published_year=2026,
            journal="Related",
            full_text_link="https://example.test/related",
            keywords=["OCR"],
            score=0.77,
            reason="겹치는 키워드: RAG",
        ),
        primary_info=PaperInfo(
            paper_id=10,
            title="Primary RAG Paper",
            authors="Kim; Lee",
            published_year=2025,
            journal="Journal",
            abstract_summary="대표 초록 요약",
            full_text_link="https://example.test/primary",
            keywords=["RAG", "검색"],
        ),
        related_info=PaperInfo(
            paper_id=30,
            title="Related OCR Paper",
            authors="Choi",
            published_year=2026,
            journal="Related",
            abstract_summary="연관 초록 요약",
            full_text_link="https://example.test/related",
            keywords=["OCR"],
        ),
        primary_paragraphs=[
            ParagraphInfo(
                paragraph_order=1,
                section_name="Introduction",
                original_text="원문 단락",
                cleaned_text="정제 단락",
                summary="요약",
                keywords=["RAG"],
            )
        ],
        related_paragraphs=[
            ParagraphInfo(
                paragraph_order=1,
                section_name="Related",
                original_text="연관 원문",
                cleaned_text="연관 정제",
                summary="연관 요약",
                keywords=["OCR"],
            )
        ],
        tables=[
            TableInfo(
                role="대표",
                table_title="Table 1",
                table_text="metric | value\nf1 | 0.90",
                table_summary="표 요약",
            )
        ],
        created_at=datetime(2026, 7, 4, 2, 30, tzinfo=UTC),
    )

