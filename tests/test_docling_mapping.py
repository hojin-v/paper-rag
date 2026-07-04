from dataclasses import dataclass
from typing import Any

from paperrag.ingest.layout.docling_backend import _MappingState, _map_item


@dataclass
class FakeProv:
    page_no: int


class FakeItem:
    def __init__(self, label: str, text: str = "content", page_no: int = 3) -> None:
        self.label = label
        self.text = text
        self.prov = [FakeProv(page_no)]


class TableItem:
    label = "table"

    def __init__(self) -> None:
        self.prov = [FakeProv(2)]

    def export_to_markdown(self) -> str:
        return "| Metric | Value |\n| --- | --- |\n| F1 | 0.91 |"


class PictureItem:
    label = "picture"

    def __init__(self) -> None:
        self.prov = [FakeProv(4)]


def test_maps_first_title_only_to_title() -> None:
    state = _MappingState()

    first = _map_item(FakeItem("title", "Paper Title"), order=0, state=state)
    second = _map_item(FakeItem("title", "Running Title"), order=1, state=state)

    assert first is not None
    assert second is not None
    assert first.block_type == "title"
    assert second.block_type == "text"


def test_maps_section_header_and_abstract_following_text() -> None:
    state = _MappingState()

    header = _map_item(FakeItem("section_header", "Abstract"), order=0, state=state)
    abstract = _map_item(FakeItem("paragraph", "This is the abstract."), order=1, state=state)
    next_header = _map_item(FakeItem("section_header", "Introduction"), order=2, state=state)
    body = _map_item(FakeItem("text", "This is body text."), order=3, state=state)

    assert header is not None
    assert abstract is not None
    assert next_header is not None
    assert body is not None
    assert header.block_type == "section_header"
    assert abstract.block_type == "abstract"
    assert next_header.block_type == "section_header"
    assert body.block_type == "text"


def test_maps_text_labels_to_text() -> None:
    for order, label in enumerate(["text", "paragraph", "list_item"]):
        block = _map_item(FakeItem(label, f"{label} text"), order=order, state=_MappingState())

        assert block is not None
        assert block.block_type == "text"
        assert block.text == f"{label} text"


def test_maps_caption_near_table_to_table_caption() -> None:
    block = _map_item(
        FakeItem("caption", "Table 1. Scores"),
        order=0,
        state=_MappingState(),
        next_item=TableItem(),
    )

    assert block is not None
    assert block.block_type == "table_caption"


def test_maps_caption_without_adjacent_table_to_figure_caption() -> None:
    block = _map_item(FakeItem("caption", "Figure 1. Overview"), order=0, state=_MappingState())

    assert block is not None
    assert block.block_type == "figure_caption"


def test_maps_table_item_to_table_with_markdown() -> None:
    block = _map_item(TableItem(), order=7, state=_MappingState())

    assert block is not None
    assert block.page == 2
    assert block.order == 7
    assert block.block_type == "table"
    assert "| Metric | Value |" in block.text


def test_maps_table_grid_when_markdown_export_is_missing() -> None:
    class Cell:
        def __init__(self, text: str) -> None:
            self.text = text

    class Data:
        grid = [[Cell("A"), Cell("B")], [Cell("1"), Cell("2")]]

    class GridOnlyTableItem:
        label = "table"
        data = Data()
        prov = [FakeProv(1)]

    block = _map_item(GridOnlyTableItem(), order=0, state=_MappingState())

    assert block is not None
    assert block.block_type == "table"
    assert block.text == "| A | B |\n| --- | --- |\n| 1 | 2 |"


def test_maps_picture_item_to_figure() -> None:
    block = _map_item(PictureItem(), order=0, state=_MappingState())

    assert block is not None
    assert block.page == 4
    assert block.block_type == "figure"
    assert block.text == ""


def test_maps_formula_to_formula() -> None:
    block = _map_item(FakeItem("formula", "E = mc^2"), order=0, state=_MappingState())

    assert block is not None
    assert block.block_type == "formula"
    assert block.text == "E = mc^2"


def test_maps_header_footer_labels_to_header_footer() -> None:
    for order, label in enumerate(["page_header", "page_footer", "footnote"]):
        block = _map_item(FakeItem(label, label), order=order, state=_MappingState())

        assert block is not None
        assert block.block_type == "header_footer"


def test_maps_unknown_label_to_text_without_discarding() -> None:
    block = _map_item(FakeItem("code", "print('hello')"), order=0, state=_MappingState())

    assert block is not None
    assert block.block_type == "text"
    assert block.text == "print('hello')"


def test_marks_text_after_references_header_as_reference() -> None:
    state = _MappingState()

    header = _map_item(FakeItem("section_header", "References"), order=0, state=state)
    reference = _map_item(FakeItem("paragraph", "A. Author. 2024."), order=1, state=state)

    assert header is not None
    assert reference is not None
    assert header.block_type == "section_header"
    assert reference.block_type == "reference"


def test_preserves_docling_reference_label() -> None:
    block = _map_item(FakeItem("reference", "A. Author. 2024."), order=0, state=_MappingState())

    assert block is not None
    assert block.block_type == "reference"


def test_uses_page_one_when_provenance_is_missing() -> None:
    item: Any = FakeItem("text", "No provenance")
    item.prov = []

    block = _map_item(item, order=0, state=_MappingState())

    assert block is not None
    assert block.page == 1
