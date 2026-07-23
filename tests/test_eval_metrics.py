"""paperrag.eval.metrics 단위 테스트 — DB 없이 오프라인으로 실행된다."""

from paperrag.eval.metrics import (
    compute_cer,
    compute_teds,
    normalize_text,
    pipe_text_to_html,
)


def test_normalize_text_unifies_quotes_dashes_and_whitespace() -> None:
    text = "It’s a “test”\n\nwith  extra   spaces – and dashes—here"
    assert normalize_text(text) == "It's a \"test\" with extra spaces - and dashes-here"


def test_normalize_text_nfkc_normalizes_compatibility_characters() -> None:
    # 반각/호환 문자(예: ｆｉ 유사 폭 문자)가 표준 형태로 정규화되어야 한다.
    assert normalize_text("ﬁle") == "file"


def test_compute_cer_identical_texts_is_zero() -> None:
    result = compute_cer([("동일한 문장입니다", "동일한 문장입니다")])
    assert result.cer == 0.0
    assert result.block_count == 1


def test_compute_cer_one_character_difference() -> None:
    result = compute_cer([("abcde", "abXde")])
    assert result.cer == 1 / 5
    assert result.blocks[0].cer == 1 / 5


def test_compute_cer_aggregates_across_blocks_not_averages() -> None:
    # 짧은 블록 1글자 오류 + 긴 블록 오류 없음 -> 전체 편집거리/전체 참조 길이로 집계돼야
    # 한다(블록별 CER의 단순 평균이면 다른 값이 나온다).
    result = compute_cer([("abc", "abX"), ("abcdefghij", "abcdefghij")])
    assert result.cer == 1 / 13


def test_compute_cer_ignores_blank_reference_blocks() -> None:
    result = compute_cer([("", "무언가 인식됨"), ("실제 정답", "실제 정답")])
    assert result.cer == 0.0
    assert result.block_count == 1


def test_compute_cer_whitespace_only_difference_normalizes_to_zero() -> None:
    # PDF 복사-붙여넣기로 줄바꿈 위치만 다른 경우 -> 정규화 후에는 오류가 아니어야 한다.
    result = compute_cer([("첫 줄\n둘째 줄", "첫 줄 둘째 줄")])
    assert result.cer == 0.0


def test_pipe_text_to_html_simple_grid_has_no_spans() -> None:
    html = pipe_text_to_html("a | b\nc | d")
    assert html == "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"


def test_pipe_text_to_html_reconstructs_colspan() -> None:
    # tests/test_paddle_mapping.py의 colspan 테스트 케이스와 정확히 대응(정방향의 역변환).
    text = "# | Model Comparison | Model Comparison\n1 | CAT | 0.6751"
    html = pipe_text_to_html(text)
    assert '<td colspan="2">Model Comparison</td>' in html
    assert html.count("<tr>") == 2
    assert "<td>#</td>" in html
    assert "<td>1</td><td>CAT</td><td>0.6751</td>" in html


def test_pipe_text_to_html_reconstructs_rowspan() -> None:
    # tests/test_paddle_mapping.py의 rowspan 테스트 케이스와 정확히 대응.
    text = "Group A | x | 1\nGroup A | y | 2\nGroup B | z | 3"
    html = pipe_text_to_html(text)
    assert '<td rowspan="2">Group A</td><td>x</td><td>1</td>' in html
    assert "<td>y</td><td>2</td>" in html
    assert "<td>Group B</td><td>z</td><td>3</td>" in html
    # Group A는 두 번째 행에서 rowspan으로 흡수되어 별도 <td>로 다시 나오면 안 된다.
    assert html.count("Group A") == 1


def test_compute_teds_identical_tables_is_one() -> None:
    table = "a | b\nc | d"
    result = compute_teds([(table, table)])
    assert result.teds == 1.0
    assert result.table_count == 1


def test_compute_teds_structurally_different_tables_is_less_than_one() -> None:
    reference = "# | Model Comparison | Model Comparison\n1 | CAT | 0.6751"
    # colspan이 무시돼 열이 밀린(2026-07-22 재현 버그와 같은 패턴) 잘못된 표.
    hypothesis = "# | Model | Comparison\n1 | CAT | 0.6751"
    result = compute_teds([(reference, hypothesis)])
    assert 0.0 <= result.teds < 1.0


def test_compute_teds_empty_pairs_returns_zero() -> None:
    result = compute_teds([])
    assert result.teds == 0.0
    assert result.table_count == 0
