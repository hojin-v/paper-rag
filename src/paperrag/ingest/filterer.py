import re
from collections.abc import Sequence

from paperrag.ingest.models import LayoutBlock

EXCLUDED = {"figure", "figure_caption", "formula", "header_footer", "reference"}
META_TYPES = {"title", "author", "abstract"}
TABLE_TYPES = {"table", "table_caption"}
REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)


def split_blocks(
    blocks: Sequence[LayoutBlock],
) -> tuple[dict[str, list[LayoutBlock]], list[LayoutBlock], list[LayoutBlock]]:
    meta_blocks: dict[str, list[LayoutBlock]] = {block_type: [] for block_type in META_TYPES}
    body_blocks: list[LayoutBlock] = []
    table_blocks: list[LayoutBlock] = []
    after_references = False

    for block in sorted(blocks, key=lambda item: item.order):
        if after_references:
            continue
        if block.block_type == "section_header" and REFERENCE_HEADER_RE.match(block.text.strip()):
            after_references = True
            continue
        if block.block_type in EXCLUDED:
            if block.block_type == "reference":
                after_references = True
            continue
        if block.block_type in META_TYPES:
            meta_blocks[block.block_type].append(block)
            continue
        if block.block_type in TABLE_TYPES:
            table_blocks.append(block)
            continue
        if block.block_type in {"section_header", "text"}:
            body_blocks.append(block)

    return meta_blocks, body_blocks, table_blocks
