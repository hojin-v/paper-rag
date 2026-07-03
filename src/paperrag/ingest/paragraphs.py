import re
from collections.abc import Sequence

from paperrag.ingest.models import LayoutBlock, ParagraphDraft

SENTENCE_BOUNDARY_RE = re.compile(r"[.!?。]\s+|다\.")


def build_paragraphs(
    blocks: Sequence[LayoutBlock],
    *,
    min_chars: int = 100,
    max_chars: int = 1500,
    default_section: str = "본문",
) -> list[ParagraphDraft]:
    section_name = default_section
    paragraph_items: list[tuple[str, str]] = []

    for block in sorted(blocks, key=lambda item: item.order):
        text = _normalize_text(block.text)
        if not text:
            continue
        if block.block_type == "section_header":
            section_name = text
            continue
        if block.block_type not in {"text", "abstract"}:
            continue
        for chunk in _split_initial_paragraphs(text):
            paragraph_items.append((section_name, chunk))

    merged = _merge_short_neighbors(paragraph_items, min_chars=min_chars)
    split_items: list[tuple[str, str]] = []
    for section, text in merged:
        for chunk in _split_long_text(text, max_chars=max_chars):
            split_items.append((section, chunk))

    return [
        ParagraphDraft(section_name=section, paragraph_order=index + 1, original_text=text)
        for index, (section, text) in enumerate(split_items)
    ]


def _normalize_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _split_initial_paragraphs(text: str) -> list[str]:
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]
    return chunks or [text.strip()]


def _merge_short_neighbors(
    paragraphs: Sequence[tuple[str, str]],
    *,
    min_chars: int,
) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for section, text in paragraphs:
        if not merged:
            merged.append((section, text))
            continue

        previous_section, previous_text = merged[-1]
        should_merge = (
            previous_section == section
            and (len(previous_text) < min_chars or len(text) < min_chars)
        )
        if should_merge:
            merged[-1] = (previous_section, f"{previous_text}\n\n{text}")
        else:
            merged.append((section, text))
    return merged


def _split_long_text(text: str, *, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        if len(text) - start <= max_chars:
            chunks.append(text[start:].strip())
            break

        boundary = _best_boundary(text, start=start, limit=start + max_chars)
        if boundary <= start:
            boundary = start + max_chars
        chunks.append(text[start:boundary].strip())
        start = boundary
        while start < len(text) and text[start].isspace():
            start += 1

    return [chunk for chunk in chunks if chunk]


def _best_boundary(text: str, *, start: int, limit: int) -> int:
    best = -1
    for match in SENTENCE_BOUNDARY_RE.finditer(text, start, min(limit + 1, len(text))):
        if match.end() <= limit:
            best = match.end()
    return best
