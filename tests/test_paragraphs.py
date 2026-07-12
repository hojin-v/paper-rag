from paperrag.ingest.models import LayoutBlock
from paperrag.ingest.paragraphs import build_paragraphs


def test_build_paragraphs_merges_short_neighbors_and_keeps_section() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="section_header", text="Introduction", order=1),
        LayoutBlock(page=1, block_type="text", text="짧은 도입.", order=2),
        LayoutBlock(page=1, block_type="text", text="A" * 120, order=3),
        LayoutBlock(page=1, block_type="section_header", text="Methods", order=4),
        LayoutBlock(page=1, block_type="text", text="B" * 130, order=5),
    ]

    paragraphs = build_paragraphs(blocks, min_chars=100, max_chars=1500)

    assert len(paragraphs) == 2
    assert paragraphs[0].section_name == "Introduction"
    assert "짧은 도입." in paragraphs[0].original_text
    assert "A" * 120 in paragraphs[0].original_text
    assert paragraphs[1].section_name == "Methods"
    assert paragraphs[1].paragraph_order == 2


def test_build_paragraphs_splits_long_text_on_sentence_boundary() -> None:
    text = "첫 번째 문장입니다. " * 20
    blocks = [LayoutBlock(page=1, block_type="text", text=text, order=1)]

    paragraphs = build_paragraphs(blocks, min_chars=1, max_chars=80)

    assert len(paragraphs) > 1
    assert all(len(paragraph.original_text) <= 80 for paragraph in paragraphs)
    assert all(paragraph.section_name == "본문" for paragraph in paragraphs)


def test_build_paragraphs_rejoins_column_split_inside_sentence() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="section_header", text="1 Introduction", order=1),
        LayoutBlock(
            page=1,
            block_type="text",
            text="A" * 120 + " many business",
            order=2,
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="applications. " + "B" * 120,
            order=3,
        ),
    ]

    paragraphs = build_paragraphs(blocks, min_chars=100, max_chars=1500)

    assert len(paragraphs) == 1
    assert paragraphs[0].section_name == "1 Introduction"
    assert "many business\n\napplications." in paragraphs[0].original_text


def test_build_paragraphs_does_not_merge_complete_long_paragraphs() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="section_header", text="Results", order=1),
        LayoutBlock(page=1, block_type="text", text="A" * 120 + ".", order=2),
        LayoutBlock(page=1, block_type="text", text="another " + "B" * 120, order=3),
    ]

    paragraphs = build_paragraphs(blocks, min_chars=100, max_chars=1500)

    assert len(paragraphs) == 2
