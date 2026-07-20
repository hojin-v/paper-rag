"""STEP 4 paragraph — 본문 블록을 섹션에 귀속시켜 단락으로 분리·병합한다.

DESIGN.md §3 STEP 4 근거: 섹션 헤더로 section_name을 부여하고, 100자 미만인
짧은 단락은 이웃과 병합, 1,500자를 넘는 단락은 문장 경계에서 분할한다.
결과는 `paragraph_order`가 매겨진 `ParagraphDraft` 목록이며, 이후 STEP 5에서
단락당 LLM 호출 1회의 입력 단위가 된다(너무 잘게 쪼개면 LLM 호출 수가
과도해지고, 너무 길면 문맥이 뒤섞여 요약 품질이 떨어지는 것을 막기 위한 절충값).
"""

import re
from collections.abc import Sequence

from paperrag.ingest.models import LayoutBlock, ParagraphDraft

# 문장 경계 후보: 마침표/느낌표/물음표/구두점 뒤 공백, 또는 한국어 종결어미 "다."
SENTENCE_BOUNDARY_RE = re.compile(r"[.!?。]\s+|다\.")
# 텍스트 끝이 이미 문장으로 종료되었는지 판정(따옴표·괄호가 뒤따라도 인정).
SENTENCE_END_RE = re.compile(r"[.!?。][\"')\]}]*$")


def build_paragraphs(
    blocks: Sequence[LayoutBlock],
    *,
    min_chars: int = 100,
    max_chars: int = 1500,
    default_section: str = "본문",
) -> list[ParagraphDraft]:
    """필터링된 본문 블록을 섹션별 단락 목록으로 변환한다(STEP 4의 진입점).

    처리 순서: ① order 기준 정렬 후 section_header를 만나면 이후 블록들의
    section_name을 갱신 ② 문단 구분(빈 줄)으로 1차 분리 ③ min_chars 미만인
    이웃 단락을 병합 ④ max_chars 초과 단락을 문장 경계에서 분할.
    반환값의 paragraph_order는 1부터 순서대로 다시 매겨진다.
    """
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
    """줄 단위로 연속 공백을 축약하고 빈 줄을 제거해 OCR 텍스트를 다듬는다."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _split_initial_paragraphs(text: str) -> list[str]:
    """빈 줄(문단 구분)을 기준으로 1차 분리한다. 빈 줄이 없으면 통째로 1개 단락."""
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]
    return chunks or [text.strip()]


def _merge_short_neighbors(
    paragraphs: Sequence[tuple[str, str]],
    *,
    min_chars: int,
) -> list[tuple[str, str]]:
    """같은 섹션 내에서 너무 짧은 단락을 바로 앞 단락과 합친다.

    min_chars(기본 100자) 미만인 단락은 그 자체로는 LLM 정제·요약·키워드
    추출에 충분한 문맥을 주기 어렵다고 보고 이웃과 합친다. 길이 조건이 아니어도
    앞 문장이 마침표 없이 끝나 뒤 단락 첫 글자가 소문자로 이어지거나 하이픈으로
    줄바꿈된 경우(`_continues_sentence`)는 레이아웃상 같은 문장이 쪼개진 것으로
    보고 병합한다.
    """
    merged: list[tuple[str, str]] = []
    for section, text in paragraphs:
        if not merged:
            merged.append((section, text))
            continue

        previous_section, previous_text = merged[-1]
        should_merge = (
            previous_section == section
            and (
                len(previous_text) < min_chars
                or len(text) < min_chars
                or _continues_sentence(previous_text, text)
            )
        )
        if should_merge:
            merged[-1] = (previous_section, f"{previous_text}\n\n{text}")
        else:
            merged.append((section, text))
    return merged


def _continues_sentence(previous_text: str, next_text: str) -> bool:
    """앞 단락이 문장 중간에서 끊겼는지 휴리스틱으로 판정한다.

    앞 텍스트가 문장 종결부호로 끝나지 않았고, 다음 텍스트가 소문자로 시작하거나
    앞 텍스트가 줄바꿈 하이픈으로 끝나면(단어가 잘린 경우) 같은 문장의 연속으로 본다.
    """
    previous = previous_text.rstrip()
    following = next_text.lstrip()
    if not previous or not following or SENTENCE_END_RE.search(previous):
        return False
    return following[0].islower() or previous.endswith(("-", "‐", "‑"))


def _split_long_text(text: str, *, max_chars: int) -> list[str]:
    """max_chars(기본 1,500자)를 초과하는 단락을 문장 경계 위주로 잘라낸다.

    한 단락이 너무 길면 LLM 컨텍스트 낭비와 요약 품질 저하로 이어지므로,
    가능한 한 문장 경계(`_best_boundary`)에서 자르고 적절한 경계를 찾지 못하면
    max_chars 위치에서 강제로 자른다(정보 손실보다 처리 가능성을 우선).
    """
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
    """[start, limit] 범위 안에서 max_chars 제한을 넘지 않는 가장 뒤쪽 문장 경계를 찾는다."""
    best = -1
    for match in SENTENCE_BOUNDARY_RE.finditer(text, start, min(limit + 1, len(text))):
        if match.end() <= limit:
            best = match.end()
    return best
