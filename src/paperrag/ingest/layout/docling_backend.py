import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from paperrag.ingest.models import BlockType, DocumentLayout, LayoutBlock

TEXT_LABELS = {"text", "paragraph", "list_item"}
HEADER_FOOTER_LABELS = {"page_header", "page_footer", "footnote"}
PICTURE_LABELS = {"picture", "chart"}
ABSTRACT_HEADER_RE = re.compile(r"^\s*(abstract|초록|요약)\b", re.IGNORECASE)
REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)


@dataclass
class _MappingState:
    seen_title: bool = False
    in_abstract: bool = False
    in_references: bool = False


class DoclingBackend:
    def analyze(self, pdf_path: str) -> DocumentLayout:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "Docling이 설치되어 있지 않습니다. `pip install -e \".[ingest-full]\"`로 "
                "전체 수집 의존성을 설치하거나 `--backend simple`을 사용하세요."
            ) from exc

        converter = DocumentConverter()
        result = converter.convert(pdf_path)
        document = getattr(result, "document", result)
        items = _iter_document_items(document)
        state = _MappingState()
        blocks: list[LayoutBlock] = []

        for index, item in enumerate(items):
            previous_item = items[index - 1] if index > 0 else None
            next_item = items[index + 1] if index + 1 < len(items) else None
            block = _map_item(
                item,
                order=len(blocks),
                state=state,
                document=document,
                previous_item=previous_item,
                next_item=next_item,
            )
            if block is not None:
                blocks.append(block)

        return DocumentLayout(source_path=str(Path(pdf_path)), is_scanned=False, blocks=blocks)


def _iter_document_items(document: Any) -> list[Any]:
    iterate_items = getattr(document, "iterate_items", None)
    if not callable(iterate_items):
        return []

    items: list[Any] = []
    for entry in iterate_items():
        item = entry[0] if isinstance(entry, tuple) and entry else entry
        items.append(item)
    return items


def _map_item(
    item: Any,
    *,
    order: int,
    state: _MappingState | None = None,
    document: Any | None = None,
    previous_item: Any | None = None,
    next_item: Any | None = None,
) -> LayoutBlock | None:
    state = state or _MappingState()
    label = _label_value(item)

    if not label and not (_is_table_item(item) or _is_picture_item(item)):
        return None

    block_type: BlockType
    if _is_table_item(item):
        block_type = "table"
        text = _table_text(item, document=document)
    elif _is_picture_item(item):
        block_type = "figure"
        text = ""
    elif label == "title":
        block_type = "title" if not state.seen_title else "text"
        state.seen_title = True
        text = _item_text(item, document=document)
    elif label == "section_header":
        block_type = "section_header"
        text = _item_text(item, document=document)
        if REFERENCE_HEADER_RE.match(text.strip()):
            state.in_references = True
            state.in_abstract = False
        else:
            state.in_abstract = bool(ABSTRACT_HEADER_RE.match(text.strip()))
    elif label in TEXT_LABELS:
        text = _item_text(item, document=document)
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        if REFERENCE_HEADER_RE.match(first_line):
            state.in_references = True
            state.in_abstract = False
        if state.in_references:
            block_type = "reference"
        else:
            block_type = "abstract" if state.in_abstract else "text"
    elif label == "caption":
        block_type = "table_caption" if _is_adjacent_to_table(previous_item, next_item) else "figure_caption"
        text = _item_text(item, document=document)
    elif label == "formula":
        block_type = "formula"
        text = _item_text(item, document=document)
    elif label == "reference":
        block_type = "reference"
        text = _item_text(item, document=document)
        state.in_references = True
        state.in_abstract = False
    elif label in HEADER_FOOTER_LABELS:
        block_type = "header_footer"
        text = _item_text(item, document=document)
    else:
        block_type = "text"
        text = _item_text(item, document=document)

    return LayoutBlock(page=_page_no(item), block_type=block_type, text=text, order=order)


def _label_value(item: Any) -> str:
    label = getattr(item, "label", "")
    value = getattr(label, "value", label)
    return str(value).strip().lower()


def _page_no(item: Any) -> int:
    prov_items = getattr(item, "prov", None) or []
    first = prov_items[0] if prov_items else None
    page_no = first.get("page_no") if isinstance(first, dict) else getattr(first, "page_no", None)
    try:
        return int(page_no)
    except (TypeError, ValueError):
        return 1


def _item_text(item: Any, *, document: Any | None = None) -> str:
    for attr_name in ("text", "orig"):
        value = getattr(item, attr_name, None)
        if value is not None:
            return str(value).strip()

    for method_name in ("export_to_markdown", "export_to_text"):
        text = _call_export(getattr(item, method_name, None), document=document)
        if text:
            return text.strip()

    return ""


def _table_text(item: Any, *, document: Any | None = None) -> str:
    text = _call_export(getattr(item, "export_to_markdown", None), document=document)
    if text:
        return text.strip()

    text = _markdown_from_grid(getattr(getattr(item, "data", None), "grid", None), document=document)
    if text:
        return text

    return _item_text(item, document=document)


def _call_export(method: Any, *, document: Any | None = None) -> str:
    if not callable(method):
        return ""
    if document is not None:
        try:
            return str(method(doc=document))
        except TypeError:
            pass
    try:
        return str(method())
    except TypeError:
        return ""


def _markdown_from_grid(grid: Any, *, document: Any | None = None) -> str:
    if grid is None:
        return ""

    rows: list[list[str]] = []
    for row in grid:
        rows.append([_cell_text(cell, document=document) for cell in row])
    if not rows:
        return ""

    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""
    padded_rows = [row + [""] * (width - len(row)) for row in rows]
    header = padded_rows[0]
    body = padded_rows[1:]
    lines = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |" for row in body)
    return "\n".join(lines)


def _cell_text(cell: Any, *, document: Any | None = None) -> str:
    get_text = getattr(cell, "_get_text", None)
    if callable(get_text):
        try:
            return str(get_text(doc=document)).replace("\n", " ").strip()
        except TypeError:
            return str(get_text()).replace("\n", " ").strip()
    return str(getattr(cell, "text", "")).replace("\n", " ").strip()


def _escape_markdown_cell(text: str) -> str:
    return text.replace("|", r"\|")


def _is_adjacent_to_table(previous_item: Any | None, next_item: Any | None) -> bool:
    return _is_table_item(previous_item) or _is_table_item(next_item)


def _is_table_item(item: Any | None) -> bool:
    if item is None:
        return False
    return "TableItem" in _class_names(item) or _label_value(item) == "table"


def _is_picture_item(item: Any | None) -> bool:
    if item is None:
        return False
    return "PictureItem" in _class_names(item) or _label_value(item) in PICTURE_LABELS


def _class_names(item: Any) -> set[str]:
    return {cast(type, cls).__name__ for cls in type(item).mro()}
