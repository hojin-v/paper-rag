from typing import Any

import httpx

from paperrag.config import get_settings
from paperrag.search.schemas import KeywordCandidate, PaperSummary, SearchMatched, SearchSuggest
from paperrag.ui.client import ApiClient, ApiUnavailable


def main() -> None:
    import streamlit as st
    import streamlit.components.v1 as components

    _ensure_state(st)
    settings = get_settings()
    client = ApiClient(
        settings.api_base_url,
        timeout_seconds=settings.api_timeout_seconds,
        public_base_url=settings.public_api_base_url,
    )

    st.title("Paper RAG")
    _render_readiness(st, client)
    upload_tab, search_tab = st.tabs(["PDF 자동 분석 모니터", "논문 검색·엑셀"])

    with upload_tab:
        _render_upload_review(st, components, client)

    with search_tab:
        _render_search(st, client)


def _render_readiness(st: Any, client: ApiClient) -> None:
    try:
        report = client.readiness()
    except (ApiUnavailable, httpx.HTTPError, ValueError) as exc:
        st.error(f"서비스 준비 상태를 확인하지 못했습니다: {exc}")
        return
    if report.get("status") == "ready":
        st.success("실제 OCR·임베딩·LLM·DB 구성요소가 준비되었습니다.")
        return
    errors = [str(item) for item in report.get("errors", [])]
    st.error(
        "현재는 실사용 준비가 완료되지 않았습니다. 누락 항목: "
        + (", ".join(errors) if errors else "상세 정보 확인 필요")
    )
    components = report.get("components", {})
    if isinstance(components, dict):
        with st.expander("준비 상태 상세"):
            for name, row in components.items():
                if not isinstance(row, dict):
                    continue
                st.write(f"{name}: {row.get('status')} — {row.get('detail')}")


def _render_search(st: Any, client: ApiClient) -> None:
    st.subheader("논문 검색")

    with st.form("search_form"):
        query = st.text_input("질의", value=st.session_state["query"])
        submitted = st.form_submit_button("검색")

    if submitted:
        st.session_state["query"] = query
        with st.spinner("질의 키워드를 분석하고 논문을 검색하고 있습니다..."):
            _run_search(st, client, query)

    suggestion = st.session_state.get("suggestion")
    if suggestion is not None:
        _render_suggestions(st, client, suggestion)

    result = st.session_state.get("result")
    if result is not None:
        _render_result(st, client, result)


def _render_upload_review(st: Any, components: Any, client: ApiClient) -> None:
    st.subheader("비정형 PDF 자동 구조화 과정")
    st.caption(
        "운영 품질 모니터입니다. 레이아웃 검출, 영역별 OCR, 자동 품질 판정의 처리 근거를 "
        "확인하며 일반 검색 사용자는 이 과정에 개입하지 않습니다."
    )
    try:
        recent_documents = client.list_documents()
    except (ApiUnavailable, httpx.HTTPError):
        recent_documents = []
    if recent_documents:
        labels = {
            document.document_id: (
                f"{document.filename} · {document.status} · {document.created_at:%Y-%m-%d %H:%M}"
            )
            for document in recent_documents
        }
        selected_document = st.selectbox(
            "이전 검수 문서 다시 열기",
            [""] + list(labels),
            format_func=lambda value: labels.get(value, "선택하지 않음"),
        )
        if selected_document and selected_document != st.session_state.get("review_document_id"):
            st.session_state["review_document_id"] = selected_document
    uploaded = st.file_uploader("PDF 논문", type=["pdf"], key="review_pdf")
    st.info(
        "자동 처리: 페이지 이미지화 → 레이아웃 검출·중복 제거 → 영역 crop OCR·표 OCR → "
        "품질 합격 문서 자동 적재 후보 / 실패 문서 관리자 예외 대기열"
    )
    if st.button("업로드 후 자동 구조화 실행", disabled=uploaded is None, type="primary"):
        try:
            with st.spinner("레이아웃 검출, 영역별 OCR과 자동 품질 판정을 진행하고 있습니다..."):
                document = client.upload_document(uploaded.name, uploaded.getvalue(), "paddle")
                st.session_state["review_document_id"] = document.document_id
                st.session_state["review_document"] = document
                document = client.run_automatic_ocr(document.document_id)
        except (ApiUnavailable, httpx.HTTPError) as exc:
            st.error(f"자동 구조화에 실패했습니다: {exc}")
        else:
            st.session_state["review_document_id"] = document.document_id
            st.session_state["review_document"] = document

    document_id = st.session_state.get("review_document_id")
    if not document_id:
        return
    try:
        document = client.get_document(document_id)
    except (ApiUnavailable, httpx.HTTPError) as exc:
        st.error(f"검수 문서를 불러오지 못했습니다: {exc}")
        return

    columns = st.columns(4)
    phase_labels = {
        "layout_review": "1. 자동 레이아웃 결과",
        "ocr_review": "2. 품질 예외 대기",
        "ready_to_ingest": "3. 자동 처리 합격",
    }
    columns[0].metric("현재 단계", phase_labels.get(document.phase, document.phase))
    columns[1].metric("분석 모델", document.backend)
    columns[2].metric("페이지", len(document.pages))
    columns[3].metric("검출 영역", len(document.blocks))
    for warning in document.warnings:
        st.warning(warning)

    layout_quality = document.layout_quality
    if layout_quality is not None:
        layout_columns = st.columns(4)
        layout_columns[0].metric(
            "텍스트 검출선",
            layout_quality.detected_text_lines,
        )
        layout_columns[1].metric(
            "초기 레이아웃 커버리지",
            f"{layout_quality.initial_text_coverage:.1%}",
        )
        layout_columns[2].metric("자동 확장 박스", layout_quality.expanded_blocks)
        layout_columns[3].metric("자동 추가 본문", layout_quality.added_text_blocks)

    if document.phase == "layout_review":
        st.info(
            "레이아웃 모델이 자동 검출하고 중복을 제거한 영역입니다. 아직 OCR 텍스트는 없으며, "
            "다음 단계에서 표시된 각 박스를 그대로 crop해 OCR합니다."
        )
    elif document.phase == "ocr_review":
        st.error(
            "자동 OCR은 완료됐지만 품질 기준을 통과하지 못해 DB 적재에서 격리했습니다. "
            "일반 사용자가 아닌 관리자가 반복 오류를 분석해 모델 개선 데이터로 사용합니다."
        )
    else:
        st.success("자동 레이아웃·OCR 품질 기준을 통과해 DB·Vector DB 적재가 가능합니다.")

    quality = document.automation_quality
    if quality is not None:
        quality_columns = st.columns(4)
        quality_columns[0].metric("자동 판정", "합격" if quality.status == "passed" else "예외")
        quality_columns[1].metric("OCR 영역 인식률", f"{quality.ocr_coverage:.1%}")
        title_quality = quality.title_detected and quality.title_consistent
        quality_columns[2].metric("제목 구조 판정", "성공" if title_quality else "의심")
        quality_columns[3].metric(
            "표 구조화",
            f"{quality.tables_structured}/{quality.tables_detected}",
        )
        for reason in quality.reasons:
            st.warning(reason)

    st.caption("색상 영역을 클릭하면 자동 유형·좌표와 그 박스에서 추출한 OCR 결과를 확인할 수 있습니다.")
    components.iframe(
        client.viewer_url(document.document_id, editable=False),
        height=900,
        scrolling=True,
    )

    if document.phase == "layout_review":
        if st.button("다음 자동화 단계: 영역별 OCR·품질 판정 실행", type="primary"):
            try:
                with st.spinner("일반 영역 OCR, 표 구조 OCR과 자동 품질 판정을 실행합니다..."):
                    client.run_automatic_ocr(document.document_id)
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"자동 OCR 실행에 실패했습니다: {exc}")
            else:
                st.success("자동 OCR과 품질 판정을 완료했습니다.")
                st.rerun()

    with st.expander("관리자 진단·모델 개선 도구"):
        st.caption(
            "자동 품질 예외의 원인을 확인하고 정답 데이터를 만드는 운영자 전용 기능입니다. "
            "일반 논문 검색 사용자의 처리 단계가 아닙니다."
        )
        st.link_button(
            "관리자 교정 화면 열기",
            client.viewer_url(document.document_id, editable=True),
        )
        if document.phase == "layout_review" and st.button("자동 중복 박스 다시 정리"):
            try:
                client.deduplicate_layout(document.document_id)
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"중복 박스 정리에 실패했습니다: {exc}")
            else:
                st.rerun()
        if document.phase == "ocr_review":
            if st.button("관리자 확인 결과를 적재 가능 상태로 승인"):
                try:
                    client.approve_all_blocks(document.document_id)
                    client.confirm_document_ocr(document.document_id)
                except (ApiUnavailable, httpx.HTTPError) as exc:
                    st.error(f"관리자 승인에 실패했습니다: {exc}")
                else:
                    st.rerun()
            if st.button("레이아웃 분석부터 다시 실행 준비"):
                try:
                    client.return_to_layout_review(document.document_id)
                except (ApiUnavailable, httpx.HTTPError) as exc:
                    st.error(f"레이아웃 단계로 되돌리지 못했습니다: {exc}")
                else:
                    st.rerun()

    ingest_column, export_column = st.columns(2)
    with ingest_column:
        if st.button(
            "자동 구조화 결과를 DB·Vector DB에 적재",
            disabled=document.status == "ingested" or document.phase != "ready_to_ingest",
            type="primary",
        ):
            try:
                with st.spinner("LLM 정제, 임베딩, DB 적재를 진행하고 있습니다..."):
                    result = client.ingest_document(document.document_id)
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"적재에 실패했습니다: {exc}")
            else:
                st.success(f"paper_id={result.paper_id}로 적재했습니다: {result.totals}")
    with export_column:
        if st.button("검수 완료 학습데이터 준비"):
            try:
                st.session_state["training_zip"] = client.download_training_data()
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"학습데이터 생성에 실패했습니다: {exc}")
        training_zip = st.session_state.get("training_zip")
        if training_zip:
            st.download_button(
                "Colab용 학습데이터 ZIP 다운로드",
                data=training_zip,
                file_name="paperrag-training-data.zip",
                mime="application/zip",
            )


def _ensure_state(st: Any) -> None:
    defaults = {
        "query": "",
        "result": None,
        "suggestion": None,
        "suggest_session_id": None,
        "excel_result_id": None,
        "excel_bytes": None,
        "review_document_id": None,
        "review_document": None,
        "training_zip": None,
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
    except (httpx.HTTPError, ValueError) as exc:
        st.error(f"API 요청에 실패했습니다: {exc}")
        return

    if isinstance(response, SearchMatched):
        _set_result(st, response)
        return

    _set_suggestion(st, response)


def _render_suggestions(st: Any, client: ApiClient, suggestion: SearchSuggest) -> None:
    if suggestion.query_keywords:
        st.caption("질의 추출 키워드: " + ", ".join(suggestion.query_keywords))
    if suggestion.explanation:
        st.info(suggestion.explanation)
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
    match_label = "정확히 일치" if result.match_type == "exact" else "사용자가 선택한 유사 키워드"
    st.success(
        f"검색 질의를 '{result.matched_keyword}' 키워드로 해석했습니다. "
        f"매칭 방식은 {match_label}입니다."
    )
    if result.query_keywords:
        st.caption("질의 추출 키워드: " + ", ".join(result.query_keywords))
    if result.explanation:
        st.info(result.explanation)
    _render_paper_card(st, "대표 논문", result.primary_paper, result.matched_keyword)

    if result.related_paper is not None:
        _render_paper_card(st, "연관 논문", result.related_paper, result.matched_keyword)
    else:
        st.info("연관 논문이 없습니다.")

    st.caption(
        "메타데이터, 섹션별 원문·요약, 단락별 원문·요약·키워드, 표 셀은 아래 엑셀에서 확인할 수 있습니다."
    )
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
        if paper.full_text_link:
            st.link_button("논문 원문 링크", paper.full_text_link)


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
