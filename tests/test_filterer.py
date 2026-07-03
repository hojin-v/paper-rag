from paperrag.ingest.filterer import EXCLUDED, split_blocks
from paperrag.ingest.models import LayoutBlock


def test_split_blocks_filters_all_excluded_types_and_groups_meta_body_tables() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="title", text="Title", order=1),
        LayoutBlock(page=1, block_type="author", text="A, B", order=2),
        LayoutBlock(page=1, block_type="abstract", text="Abstract", order=3),
        LayoutBlock(page=1, block_type="section_header", text="Intro", order=4),
        LayoutBlock(page=1, block_type="text", text="Body", order=5),
        LayoutBlock(page=1, block_type="table_caption", text="Table 1", order=6),
        LayoutBlock(page=1, block_type="table", text="a | b", order=7),
        LayoutBlock(page=1, block_type="figure", text="figure", order=8),
        LayoutBlock(page=1, block_type="figure_caption", text="figure caption", order=9),
        LayoutBlock(page=1, block_type="formula", text="E=mc2", order=10),
        LayoutBlock(page=1, block_type="header_footer", text="1", order=11),
        LayoutBlock(page=1, block_type="reference", text="ref", order=12),
        LayoutBlock(page=1, block_type="text", text="after reference block", order=13),
    ]

    meta, body, tables = split_blocks(blocks)

    assert set(EXCLUDED) == {"figure", "figure_caption", "formula", "header_footer", "reference"}
    assert [block.text for block in meta["title"]] == ["Title"]
    assert [block.text for block in meta["author"]] == ["A, B"]
    assert [block.text for block in meta["abstract"]] == ["Abstract"]
    assert [block.text for block in body] == ["Intro", "Body"]
    assert [block.text for block in tables] == ["Table 1", "a | b"]


def test_split_blocks_excludes_everything_after_reference_section_header() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="section_header", text="Results", order=1),
        LayoutBlock(page=1, block_type="text", text="result text", order=2),
        LayoutBlock(page=2, block_type="section_header", text="참고문헌", order=3),
        LayoutBlock(page=2, block_type="text", text="must be excluded", order=4),
        LayoutBlock(page=3, block_type="table", text="also excluded", order=5),
    ]

    _, body, tables = split_blocks(blocks)

    assert [block.text for block in body] == ["Results", "result text"]
    assert tables == []
