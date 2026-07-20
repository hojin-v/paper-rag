from collections.abc import Iterable
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from paperrag.search.schemas import (
    PaperInfo,
    ParagraphInfo,
    ResultBundle,
    SectionInfo,
    TableInfo,
)

HEADER_FILL = PatternFill(fill_type="solid", fgColor="D9EAF7")
HEADER_FONT = Font(bold=True)


def build_excel(data: ResultBundle, out_path: str | Path) -> str:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "검색 결과 요약"
    _write_summary(summary_sheet, data)

    primary_sheet = workbook.create_sheet("대표 논문 정보")
    _write_paper_info(primary_sheet, data.primary_info)

    primary_section_sheet = workbook.create_sheet("대표 논문 섹션")
    _write_sections(primary_section_sheet, data.primary_sections)

    primary_paragraph_sheet = workbook.create_sheet("대표 논문 단락")
    _write_paragraphs(primary_paragraph_sheet, data.primary_paragraphs)

    related_sheet = workbook.create_sheet("연관 논문 정보")
    _write_paper_info(
        related_sheet,
        data.related_info,
        relation_score=data.related_paper.score if data.related_paper else None,
        relation_reason=data.related_paper.reason if data.related_paper else None,
    )

    related_section_sheet = workbook.create_sheet("연관 논문 섹션")
    _write_sections(related_section_sheet, data.related_sections)

    related_paragraph_sheet = workbook.create_sheet("연관 논문 단락")
    _write_paragraphs(related_paragraph_sheet, data.related_paragraphs)

    tables_sheet = workbook.create_sheet("표 데이터")
    _write_tables(tables_sheet, data.tables)

    table_cells_sheet = workbook.create_sheet("표 셀")
    _write_table_cells(table_cells_sheet, data.tables)

    for sheet in workbook.worksheets:
        _style_sheet(sheet)
    for sheet_name in (
        "대표 논문 섹션",
        "대표 논문 단락",
        "연관 논문 섹션",
        "연관 논문 단락",
    ):
        _style_paragraph_sheet(workbook[sheet_name])
    _style_table_sheet(tables_sheet)

    workbook.save(path)
    return str(path)


def _write_summary(sheet: Worksheet, data: ResultBundle) -> None:
    _append_row(
        sheet,
        [
            "질의",
            "질의 추출 키워드",
            "매칭 키워드",
            "매칭 방식",
            "검색 설명",
            "대표 논문 제목",
            "연관 논문 제목",
            "대표 RAG 점수",
            "대표 선정 사유",
            "연관 관계 점수",
            "연관 선정 사유",
            "생성 일시",
        ],
    )
    related_title = data.related_paper.title if data.related_paper else ""
    _append_row(
        sheet,
        [
            data.query,
            ", ".join(data.query_keywords),
            data.matched_keyword,
            data.match_type,
            data.explanation,
            data.primary_paper.title,
            related_title,
            data.primary_paper.score,
            data.primary_paper.reason,
            data.related_paper.score if data.related_paper else None,
            data.related_paper.reason if data.related_paper else None,
            data.created_at.isoformat(timespec="seconds"),
        ],
    )


def _write_paper_info(
    sheet: Worksheet,
    paper: PaperInfo | None,
    *,
    relation_score: float | None = None,
    relation_reason: str | None = None,
) -> None:
    headers = [
        "논문 ID",
        "제목",
        "저자",
        "연도",
        "저널",
        "초록 원문",
        "초록 요약",
        "전문 링크",
        "키워드",
    ]
    include_relation = relation_score is not None or relation_reason is not None
    if include_relation:
        headers.extend(["연관 점수", "연관 사유"])
    _append_row(sheet, headers)
    if paper is None:
        return
    row: list[Any] = [
        paper.paper_id,
        paper.title,
        paper.authors,
        paper.published_year,
        paper.journal,
        paper.abstract,
        paper.abstract_summary,
        paper.full_text_link,
        ", ".join(paper.keywords),
    ]
    if include_relation:
        row.extend([relation_score, relation_reason])
    _append_row(sheet, row)


def _write_sections(sheet: Worksheet, sections: Iterable[SectionInfo]) -> None:
    _append_row(
        sheet,
        ["섹션 순서", "섹션명", "단락 수", "섹션 원문", "섹션 정제문", "섹션 요약", "키워드"],
    )
    for section in sections:
        _append_row(
            sheet,
            [
                section.section_order,
                section.section_name,
                section.paragraph_count,
                section.original_text,
                section.cleaned_text,
                section.summary,
                ", ".join(section.keywords),
            ],
        )


def _write_paragraphs(sheet: Worksheet, paragraphs: Iterable[ParagraphInfo]) -> None:
    _append_row(sheet, ["단락 번호", "섹션명", "원문", "정제문", "요약", "키워드"])
    for paragraph in paragraphs:
        _append_row(
            sheet,
            [
                paragraph.paragraph_order,
                paragraph.section_name,
                paragraph.original_text,
                paragraph.cleaned_text,
                paragraph.summary,
                ", ".join(paragraph.keywords),
            ],
        )


def _write_tables(sheet: Worksheet, tables: Iterable[TableInfo]) -> None:
    _append_row(sheet, ["구분", "표 제목", "표 내용", "표 요약"])
    for table in tables:
        _append_row(
            sheet,
            [table.role, table.table_title, table.table_text, table.table_summary],
        )


def _write_table_cells(sheet: Worksheet, tables: Iterable[TableInfo]) -> None:
    _append_row(sheet, ["구분", "표 번호", "표 제목", "행", "열", "셀 값"])
    for table_index, table in enumerate(tables, start=1):
        for row_index, row in enumerate(_parse_table_rows(table.table_text), start=1):
            for column_index, value in enumerate(row, start=1):
                _append_row(
                    sheet,
                    [
                        table.role,
                        table_index,
                        table.table_title,
                        row_index,
                        column_index,
                        value,
                    ],
                )


def _parse_table_rows(table_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw_line in table_text.splitlines():
        line = raw_line.strip().strip("|").strip()
        if not line:
            continue
        if "|" in line:
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) >= 2:
                rows.append(cells)
            continue
        if line.upper().startswith("TABLE "):
            break
        if rows:
            rows[-1][0] = f"{rows[-1][0]} {line}".strip()
    return rows


def _append_row(sheet: Worksheet, values: list[Any]) -> None:
    sheet.append(["" if value is None else value for value in values])


def _style_sheet(sheet: Worksheet) -> None:
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    _auto_width(sheet)


def _style_paragraph_sheet(sheet: Worksheet) -> None:
    for column in ("C", "D"):
        sheet.column_dimensions[column].width = 60


def _style_table_sheet(sheet: Worksheet) -> None:
    sheet.column_dimensions["C"].width = 60


def _auto_width(sheet: Worksheet) -> None:
    for column_index, column_cells in enumerate(sheet.columns, start=1):
        max_length = 0
        for cell in column_cells:
            value = str(cell.value or "")
            longest_line = max((len(line) for line in value.splitlines()), default=0)
            max_length = max(max_length, longest_line)
        sheet.column_dimensions[get_column_letter(column_index)].width = min(max_length + 2, 40)
