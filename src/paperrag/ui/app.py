from typing import Any

import httpx

from paperrag.config import get_settings
from paperrag.search.schemas import KeywordCandidate, PaperSummary, SearchMatched, SearchSuggest
from paperrag.ui.client import ApiClient, ApiUnavailable


def main() -> None:
    import streamlit as st

    _ensure_state(st)
    settings = get_settings()
    client = ApiClient(settings.api_base_url)

    st.title("논문 검색")

    with st.form("search_form"):
        query = st.text_input("질의", value=st.session_state["query"])
        submitted = st.form_submit_button("검색")

    if submitted:
        st.session_state["query"] = query
        _run_search(st, client, query)

    suggestion = st.session_state.get("suggestion")
    if suggestion is not None:
        _render_suggestions(st, client, suggestion)

    result = st.session_state.get("result")
    if result is not None:
        _render_result(st, client, result)


def _ensure_state(st: Any) -> None:
    defaults = {
        "query": "",
        "result": None,
        "suggestion": None,
        "suggest_session_id": None,
        "excel_result_id": None,
        "excel_bytes": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _run_search(st: Any, client: ApiClient, query: str) -> None:
    normalized_query = query.strip()
    if not normalized_query:
        st.warning("검색할 질의를 입력하세요.")
        return

    try:
        response = client.search(normalized_query)
    except ApiUnavailable as exc:
        st.error(str(exc))
        return
    except httpx.HTTPError as exc:
        st.error(f"API 요청에 실패했습니다: {exc}")
        return

    if isinstance(response, SearchMatched):
        _set_result(st, response)
        return

    _set_suggestion(st, response)


def _render_suggestions(st: Any, client: ApiClient, suggestion: SearchSuggest) -> None:
    if not suggestion.candidates:
        st.info("선택할 유사 키워드가 없습니다.")
        return

    st.subheader("유사 키워드")
    candidates_by_id = {candidate.keyword_id: candidate for candidate in suggestion.candidates}
    keyword_id = st.radio(
        "유사 키워드",
        list(candidates_by_id),
        format_func=lambda item: _format_candidate(candidates_by_id[item]),
    )

    if st.button("이 키워드로 검색"):
        try:
            session_id = st.session_state.get("suggest_session_id") or suggestion.session_id
            result = client.select(session_id, keyword_id)
        except ApiUnavailable as exc:
            st.error(str(exc))
            return
        except httpx.HTTPError as exc:
            st.error(f"API 요청에 실패했습니다: {exc}")
            return
        _set_result(st, result)


def _render_result(st: Any, client: ApiClient, result: SearchMatched) -> None:
    st.subheader("검색 결과")
    _render_paper_card(st, "대표 논문", result.primary_paper, result.matched_keyword)

    if result.related_paper is not None:
        _render_paper_card(st, "연관 논문", result.related_paper, result.matched_keyword)
    else:
        st.info("연관 논문이 없습니다.")

    _render_download(st, client, result.result_id)


def _render_paper_card(
    st: Any,
    label: str,
    paper: PaperSummary,
    matched_keyword: str,
) -> None:
    with st.container(border=True):
        st.markdown(f"#### {label}")
        st.markdown(f"**{paper.title}**")
        rows = [
            ("저자", paper.authors or "-"),
            ("연도", str(paper.published_year) if paper.published_year is not None else "-"),
            ("저널", paper.journal or "-"),
            ("매칭 키워드", matched_keyword),
            ("논문 키워드", ", ".join(paper.keywords) if paper.keywords else "-"),
            ("점수", f"{paper.score:.3f}"),
            ("선정 사유", paper.reason),
        ]
        for name, value in rows:
            st.markdown(f"- **{name}**: {value}")


def _render_download(st: Any, client: ApiClient, result_id: str) -> None:
    if st.session_state.get("excel_result_id") != result_id:
        st.session_state["excel_result_id"] = result_id
        st.session_state["excel_bytes"] = None

    if st.session_state["excel_bytes"] is None:
        try:
            st.session_state["excel_bytes"] = client.download_excel(result_id)
        except ApiUnavailable as exc:
            st.error(str(exc))
            return
        except httpx.HTTPError as exc:
            st.error(f"엑셀 다운로드 준비에 실패했습니다: {exc}")
            return

    st.download_button(
        "엑셀 다운로드",
        data=st.session_state["excel_bytes"],
        file_name=f"paper-search-{result_id}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _set_result(st: Any, result: SearchMatched) -> None:
    st.session_state["result"] = result
    st.session_state["suggestion"] = None
    st.session_state["suggest_session_id"] = None
    st.session_state["excel_result_id"] = None
    st.session_state["excel_bytes"] = None


def _set_suggestion(st: Any, suggestion: SearchSuggest) -> None:
    st.session_state["result"] = None
    st.session_state["suggestion"] = suggestion
    st.session_state["suggest_session_id"] = suggestion.session_id
    st.session_state["excel_result_id"] = None
    st.session_state["excel_bytes"] = None


def _format_candidate(candidate: KeywordCandidate) -> str:
    return f"{candidate.keyword} ({candidate.similarity:.3f})"


if __name__ == "__main__":
    main()
