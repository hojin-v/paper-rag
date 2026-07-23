"""OCR 텍스트 정확도(CER)와 표 구조 정확도(TEDS)를 표준 방식으로 계산한다.

`jiwer`/`apted`는 이 모듈이 실제로 호출될 때만 필요하므로 함수 내부에서 지연 import한다
(CLAUDE.md 코드 규칙 — 코어 패키지는 경량 의존성만으로 임포트 가능해야 함). 두 라이브러리
모두 `eval` extra로 설치한다.

설계 배경(2026-07-23 대화 정리):
- 라벨링 시 PDF 원문 복사-붙여넣기 등으로 생기는 줄바꿈·따옴표·공백 차이를 사람이 완벽히
  맞추기보다, 이 모듈의 `normalize_text`가 정규화해 흡수한다(CER/WER 계산의 표준 관행).
  단, 실제 글자가 틀렸는지·자모 유사 오인식 같은 진짜 인식 오류는 정규화로 흡수되지
  않으므로 여전히 사람이 정확히 봐야 한다.
- 표 구조 품질은 기존 `paddle_backend.py::_table_structure_quality`가 "TEDS를 대체하는
  임시 지표"로만 문서화돼 있었다. 이 모듈은 표준 TEDS 정의(Tree-Edit-Distance-based
  Similarity, PubTabNet 논문)를 따르는 근사 구현으로 대체한다 — 원 논문 구현을 그대로
  가져온 것은 아니고(셀 텍스트를 문자 단위로 트리에 다시 펼치는 대신, 셀 텍스트 간
  정규화된 편집거리를 rename 비용으로 직접 쓴다), 같은 알고리즘적 정의(트리 편집거리 /
  최대 노드 수)를 따른다.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

_QUOTE_DASH_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)


def normalize_text(text: str) -> str:
    """CER 비교 전 텍스트를 정규화한다.

    NFKC 정규화(호환 유니코드 통일) → 곡선 따옴표/엔·엠대시를 직선 표기로 통일 →
    모든 공백(줄바꿈 포함)을 단일 스페이스로 축약 → 앞뒤 공백 제거. PDF 리더마다
    다른 인코딩 관례(예: 스마트 따옴표, 하이픈 자동 병합 여부)나 크롭 이미지의 줄바꿈
    위치 차이가 실제 인식 오류가 아닌데도 편집거리에 잡히는 것을 막는다.
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(_QUOTE_DASH_MAP)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


@dataclass
class BlockCer:
    """블록 하나의 정규화 후 텍스트 쌍과 개별 CER(리포트의 "가장 틀린 블록" 표시용)."""

    reference: str
    hypothesis: str
    cer: float


@dataclass
class CerResult:
    """여러 블록에 걸친 누적(aggregate) CER 결과.

    `cer`는 블록별 CER의 평균이 아니라 전체 편집거리 합 / 전체 참조 문자 수 합이다
    (블록 길이가 제각각인데 평균을 내면 짧은 블록의 오류가 과대평가된다 — jiwer가
    리스트를 넘기면 이 방식으로 집계한다).
    """

    cer: float
    block_count: int
    total_reference_chars: int
    blocks: list[BlockCer] = field(default_factory=list)


def compute_cer(pairs: list[tuple[str, str]]) -> CerResult:
    """`(참조 텍스트, 예측 텍스트)` 쌍 목록의 누적 CER을 계산한다.

    참조가 빈 문자열인 쌍(예: 실제로 텍스트가 없는 영역)은 jiwer의 집계 대상에서
    제외한다 — 빈 참조는 CER 정의(편집거리/참조 길이) 자체가 성립하지 않는다.
    """
    import jiwer

    normalized_pairs = [
        (normalize_text(ref), normalize_text(hyp)) for ref, hyp in pairs
    ]
    non_empty = [(ref, hyp) for ref, hyp in normalized_pairs if ref]
    if not non_empty:
        return CerResult(cer=0.0, block_count=0, total_reference_chars=0, blocks=[])

    references = [ref for ref, _ in non_empty]
    hypotheses = [hyp for _, hyp in non_empty]
    aggregate_cer = jiwer.cer(references, hypotheses)
    blocks = [
        BlockCer(reference=ref, hypothesis=hyp, cer=jiwer.cer(ref, hyp))
        for ref, hyp in non_empty
    ]
    return CerResult(
        cer=aggregate_cer,
        block_count=len(non_empty),
        total_reference_chars=sum(len(ref) for ref in references),
        blocks=blocks,
    )


def pipe_text_to_html(text: str) -> str:
    """`"|"`-구분 표 텍스트를 HTML `<table>`로 재구성한다.

    `paddle_backend.py::_html_table_to_pipe_text`의 역변환이다. 그 함수는 colspan/rowspan
    병합 셀을 "같은 텍스트를 여러 칸에 반복"하는 방식으로 평면 텍스트에 담으므로
    (`_TableHtmlParser` 참고), 이 함수는 그 반복 패턴을 감지해 다시 colspan/rowspan
    속성으로 되돌린다 — 열 방향(같은 행 안 연속 반복)은 colspan, 행 방향(같은 열 위치의
    연속 행 반복)은 rowspan으로 판정한다.

    **한계**: 우연히 같은 값이 반복된 인접 셀(예: 빈 칸이 여러 개 이어지는 경우)도 병합으로
    오인할 수 있는 휴리스틱이다. 다만 예측값(ocr_text)과 정답값(corrected_text) 양쪽에
    동일한 규칙을 적용하므로 TEDS 비교의 공정성 자체는 유지된다.
    """
    if not text.strip():
        return "<table></table>"
    rows = [row.split(" | ") for row in text.split("\n") if row]
    if not rows:
        return "<table></table>"
    num_cols = max(len(row) for row in rows)
    rows = [row + [""] * (num_cols - len(row)) for row in rows]

    consumed = [[False] * num_cols for _ in rows]
    rowspan = [[1] * num_cols for _ in rows]
    for col in range(num_cols):
        row_index = 0
        while row_index < len(rows):
            if consumed[row_index][col]:
                row_index += 1
                continue
            value = rows[row_index][col]
            span = 1
            while (
                value != ""
                and row_index + span < len(rows)
                and not consumed[row_index + span][col]
                and rows[row_index + span][col] == value
            ):
                span += 1
            rowspan[row_index][col] = span
            for offset in range(1, span):
                consumed[row_index + offset][col] = True
            row_index += span

    html_rows: list[str] = []
    for row_index, row in enumerate(rows):
        cells: list[str] = []
        col = 0
        while col < num_cols:
            if consumed[row_index][col]:
                col += 1
                continue
            value = row[col]
            span = rowspan[row_index][col]
            colspan = 1
            while (
                value != ""
                and col + colspan < num_cols
                and not consumed[row_index][col + colspan]
                and rowspan[row_index][col + colspan] == span
                and row[col + colspan] == value
            ):
                colspan += 1
            attrs = ""
            if colspan > 1:
                attrs += f' colspan="{colspan}"'
            if span > 1:
                attrs += f' rowspan="{span}"'
            escaped = value.replace("{", "(").replace("}", ")")
            cells.append(f"<td{attrs}>{escaped}</td>")
            col += colspan
        html_rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>" + "".join(html_rows) + "</table>"


@dataclass
class TableTeds:
    """표 하나의 TEDS 점수(리포트의 "가장 구조가 어긋난 표" 표시용)."""

    reference_html: str
    hypothesis_html: str
    teds: float


@dataclass
class TedsResult:
    """여러 표에 걸친 TEDS 결과. `teds`는 표별 점수의 평균이다(TEDS는 표 하나당
    [0, 1]로 정규화된 지표라 평균 내는 것이 관례)."""

    teds: float
    table_count: int
    tables: list[TableTeds] = field(default_factory=list)


def compute_teds(pairs: list[tuple[str, str]]) -> TedsResult:
    """`(참조 표 텍스트, 예측 표 텍스트)` 쌍 목록의 평균 TEDS를 계산한다."""
    if not pairs:
        return TedsResult(teds=0.0, table_count=0, tables=[])
    tables = [
        TableTeds(
            reference_html=ref,
            hypothesis_html=hyp,
            teds=_table_teds(pipe_text_to_html(ref), pipe_text_to_html(hyp)),
        )
        for ref, hyp in pairs
    ]
    mean_teds = sum(table.teds for table in tables) / len(tables)
    return TedsResult(teds=mean_teds, table_count=len(tables), tables=tables)


def _table_teds(reference_html: str, hypothesis_html: str) -> float:
    """HTML 표 두 개의 TEDS를 계산한다: `1 - 트리편집거리 / max(두 트리 노드 수)`."""
    from apted import APTED, Config
    from apted.helpers import Tree

    reference_tree = Tree.from_text(_html_to_bracket(reference_html))
    hypothesis_tree = Tree.from_text(_html_to_bracket(hypothesis_html))

    class _TedsConfig(Config):
        """구조 노드(table/tr)는 이름 일치 여부로, 셀 노드는 텍스트 유사도로 비용을 매긴다.

        원 TEDS 논문은 셀 텍스트를 문자 단위로 트리에 펼쳐 넣지만, 여기서는 같은 효과를
        셀 텍스트 간 정규화된 편집거리(0=완전 일치, 1=완전 불일치)를 rename 비용으로 직접
        계산해 얻는다 — 완전히 다른 셀도 부분적으로 비슷하면 페널티가 덜 붙는다는 TEDS의
        핵심 성질은 그대로 유지된다.
        """

        valuecls = float

        def children(self, node: object) -> list[object]:
            return node.children  # type: ignore[attr-defined]

        def insert(self, node: object) -> float:
            return 1.0

        def delete(self, node: object) -> float:
            return 1.0

        def rename(self, node1: object, node2: object) -> float:
            name1, name2 = node1.name, node2.name  # type: ignore[attr-defined]
            is_cell1, is_cell2 = name1.startswith("td:"), name2.startswith("td:")
            if is_cell1 != is_cell2:
                return 1.0
            if not is_cell1:
                return 0.0 if name1 == name2 else 1.0
            text1, text2 = name1[3:], name2[3:]
            if text1 == text2:
                return 0.0
            ratio = difflib.SequenceMatcher(None, text1, text2).ratio()
            return 1.0 - ratio

    distance = APTED(reference_tree, hypothesis_tree, _TedsConfig()).compute_edit_distance()
    max_nodes = max(_count_nodes(reference_tree), _count_nodes(hypothesis_tree))
    if max_nodes == 0:
        return 1.0
    return max(0.0, 1.0 - distance / max_nodes)


def _count_nodes(tree: object) -> int:
    return 1 + sum(_count_nodes(child) for child in tree.children)  # type: ignore[attr-defined]


def _html_to_bracket(html: str) -> str:
    """`pipe_text_to_html`이 만든 단순 HTML(`<table><tr><td ...>텍스트</td></tr></table>`)을
    `apted.helpers.Tree.from_text`가 요구하는 괄호 표기(`{table{tr{td:텍스트}}}`)로 바꾼다.

    셀 텍스트 안의 `{`/`}`는 괄호 표기를 깨뜨리므로 `pipe_text_to_html`이 이미 `(`/`)`로
    치환해 넘긴다는 전제로 동작한다(별도 이스케이프 불필요).
    """
    rows: list[str] = []
    for row_match in re.finditer(r"<tr>(.*?)</tr>", html, flags=re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_match.group(1), flags=re.DOTALL)
        cell_nodes = "".join(f"{{td:{cell}}}" for cell in cells)
        rows.append(f"{{tr{cell_nodes}}}")
    return "{table" + "".join(rows) + "}"
