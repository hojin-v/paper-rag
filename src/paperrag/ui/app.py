from dataclasses import dataclass
from typing import Any

import httpx

from paperrag.config import get_settings
from paperrag.review.models import ReviewBlock, ReviewDocument
from paperrag.review.viewer import BLOCK_LABELS
from paperrag.search.schemas import KeywordCandidate, PaperSummary, SearchMatched, SearchSuggest
from paperrag.ui.client import ApiClient, ApiUnavailable


@dataclass(frozen=True, slots=True)
class _LayoutQualityMetrics:
    detected_text_lines: int
    initial_text_coverage: float
    final_text_coverage: float
    uncovered_text_lines: int
    expanded_blocks: int
    added_text_blocks: int
    split_section_headings: int
    recovered_title_blocks: int
    recovered_author_blocks: int


@dataclass(frozen=True, slots=True)
class _ReviewProgress:
    unreviewed: int
    approved: int
    corrected: int
    rejected: int
    changed_from_detection: int


def _layout_quality_metrics(layout_quality: Any) -> _LayoutQualityMetrics:
    detected_text_lines = int(getattr(layout_quality, "detected_text_lines", 0))
    initial_text_coverage = float(getattr(layout_quality, "initial_text_coverage", 0.0))
    final_text_coverage = max(
        initial_text_coverage,
        float(getattr(layout_quality, "final_text_coverage", initial_text_coverage)),
    )
    default_uncovered = max(
        0,
        detected_text_lines - round(detected_text_lines * final_text_coverage),
    )
    uncovered_text_lines = int(getattr(layout_quality, "uncovered_text_lines", default_uncovered))
    if final_text_coverage < 1.0 and uncovered_text_lines == 0:
        uncovered_text_lines = default_uncovered
    return _LayoutQualityMetrics(
        detected_text_lines=detected_text_lines,
        initial_text_coverage=initial_text_coverage,
        final_text_coverage=final_text_coverage,
        uncovered_text_lines=uncovered_text_lines,
        expanded_blocks=int(getattr(layout_quality, "expanded_blocks", 0)),
        added_text_blocks=int(getattr(layout_quality, "added_text_blocks", 0)),
        split_section_headings=int(getattr(layout_quality, "split_section_headings", 0)),
        recovered_title_blocks=int(getattr(layout_quality, "recovered_title_blocks", 0)),
        recovered_author_blocks=int(getattr(layout_quality, "recovered_author_blocks", 0)),
    )


def _ocr_block_count(document: ReviewDocument) -> int:
    return sum(bool(block.ocr_text.strip()) for block in document.blocks)


def _review_progress(document: ReviewDocument) -> _ReviewProgress:
    statuses = [block.review_status for block in document.blocks]
    changed_from_detection = sum(
        block.detected_bbox is None
        or (block.detected_block_type is not None and block.block_type != block.detected_block_type)
        or (
            block.detected_bbox is not None
            and block.bbox is not None
            and block.bbox != block.detected_bbox
        )
        or (block.corrected_text is not None and block.corrected_text != block.ocr_text)
        for block in document.blocks
    )
    return _ReviewProgress(
        unreviewed=statuses.count("unreviewed"),
        approved=statuses.count("approved"),
        corrected=statuses.count("corrected"),
        rejected=statuses.count("rejected"),
        changed_from_detection=changed_from_detection,
    )


def _document_label(document: ReviewDocument) -> str:
    phase_labels = {
        "layout_review": "레이아웃",
        "ocr_review": "OCR 예외",
        "ready_to_ingest": "OCR 완료",
    }
    progress = _review_progress(document)
    return (
        f"{document.filename} · {phase_labels.get(document.phase, document.phase)} · "
        f"{len(document.pages)}쪽 · {len(document.blocks)}영역 · "
        f"OCR {_ocr_block_count(document)} · 미검수 {progress.unreviewed}"
    )


def _default_document_id(documents: list[ReviewDocument]) -> str | None:
    if not documents:
        return None
    return max(documents, key=lambda document: document.created_at).document_id


def _filter_review_documents(
    documents: list[ReviewDocument],
    scope: str,
) -> list[ReviewDocument]:
    if scope == "pending":
        return [
            document
            for document in documents
            if document.status != "ingested" and _review_progress(document).unreviewed > 0
        ]
    if scope == "layout":
        return [document for document in documents if document.phase == "layout_review"]
    if scope == "ocr_exception":
        return [document for document in documents if document.phase == "ocr_review"]
    if scope == "completed":
        return [
            document
            for document in documents
            if document.status == "ingested" or _review_progress(document).unreviewed == 0
        ]
    return documents


def main() -> None:
    import streamlit as st

    st.set_page_config(layout="wide")
    _ensure_state(st)
    settings = get_settings()
    client = ApiClient(
        settings.api_base_url,
        timeout_seconds=settings.api_timeout_seconds,
        public_base_url=settings.public_api_base_url,
    )

    st.title("Paper RAG")
    _render_readiness(st, client)
    upload_tab, search_tab = st.tabs(["레이아웃·OCR 검수", "RAG 검색·엑셀"])

    with upload_tab:
        _render_upload_review(st, client)

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


def _render_upload_review(st: Any, client: ApiClient) -> None:
    st.subheader("비정형 PDF 레이아웃·OCR 검수")
    st.caption(
        "운영 품질 모니터입니다. 레이아웃 검출, 영역별 OCR, 자동 품질 판정의 처리 근거를 "
        "확인하며 일반 검색 사용자는 이 과정에 개입하지 않습니다."
    )
    try:
        recent_documents = client.list_documents()
    except (ApiUnavailable, httpx.HTTPError):
        recent_documents = []
    if recent_documents:
        pending_documents = _filter_review_documents(recent_documents, "pending")
        exception_documents = _filter_review_documents(recent_documents, "ocr_exception")
        suite_columns = st.columns(4)
        suite_columns[0].metric("분석 문서", len(recent_documents))
        suite_columns[1].metric("검수 대기", len(pending_documents))
        suite_columns[2].metric("OCR 품질 예외", len(exception_documents))
        suite_columns[3].metric(
            "원본 대비 변경 영역",
            sum(_review_progress(document).changed_from_detection for document in recent_documents),
        )
        scope_labels = {
            "pending": f"검수 대기 ({len(pending_documents)})",
            "layout": (
                f"레이아웃 단계 ({len(_filter_review_documents(recent_documents, 'layout'))})"
            ),
            "ocr_exception": f"OCR 품질 예외 ({len(exception_documents)})",
            "completed": (
                f"승인·적재 완료 ({len(_filter_review_documents(recent_documents, 'completed'))})"
            ),
            "all": f"전체 ({len(recent_documents)})",
        }
        review_scope = st.segmented_control(
            "문서 범위",
            list(scope_labels),
            default="pending",
            format_func=scope_labels.__getitem__,
            key="review_scope",
        )
        visible_documents = _filter_review_documents(
            recent_documents,
            str(review_scope or "pending"),
        )
        if not visible_documents:
            st.info("선택한 범위에 해당하는 검수 문서가 없습니다.")
            visible_documents = recent_documents
        labels = {document.document_id: _document_label(document) for document in visible_documents}
        document_ids = list(labels)
        current_document_id = st.session_state.get("review_document_id")
        if current_document_id not in labels:
            current_document_id = _default_document_id(visible_documents)
            st.session_state["review_document_id"] = current_document_id
        selected_document = st.selectbox(
            "레이아웃·OCR 결과 문서",
            document_ids,
            index=document_ids.index(current_document_id),
            format_func=labels.__getitem__,
        )
        if selected_document != st.session_state.get("review_document_id"):
            st.session_state["review_document_id"] = selected_document
    with st.expander("새 PDF 분석 실행"):
        uploaded = st.file_uploader("PDF 논문", type=["pdf"], key="review_pdf")
        st.info(
            "1차 실행은 페이지 이미지화와 레이아웃 검출·누락 보정까지만 수행합니다. "
            "레이아웃을 확인한 뒤 선택 문서의 영역별 OCR·품질 판정을 실행합니다."
        )
        if st.button(
            "업로드 후 레이아웃 분석 시작",
            disabled=uploaded is None,
            type="primary",
        ):
            try:
                with st.spinner("전체 페이지의 레이아웃과 텍스트 누락을 분석하고 있습니다..."):
                    document = client.upload_document(uploaded.name, uploaded.getvalue(), "paddle")
                    st.session_state["review_document_id"] = document.document_id
                    st.session_state["review_document"] = document
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"레이아웃 분석에 실패했습니다: {exc}")
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
    progress = _review_progress(document)
    progress_columns = st.columns(5)
    progress_columns[0].metric("미검수", progress.unreviewed)
    progress_columns[1].metric("승인", progress.approved)
    progress_columns[2].metric("텍스트 교정", progress.corrected)
    progress_columns[3].metric("제외", progress.rejected)
    progress_columns[4].metric("원본 대비 변경", progress.changed_from_detection)
    for warning in document.warnings:
        st.warning(warning)

    layout_quality = document.layout_quality
    if layout_quality is not None:
        metrics = _layout_quality_metrics(layout_quality)
        layout_columns = st.columns(7)
        layout_columns[0].metric(
            "텍스트 검출선",
            metrics.detected_text_lines,
        )
        layout_columns[1].metric(
            "초기 커버리지",
            f"{metrics.initial_text_coverage:.1%}",
        )
        layout_columns[2].metric(
            "최종 커버리지",
            f"{metrics.final_text_coverage:.1%}",
            delta=f"미포함 {metrics.uncovered_text_lines}줄",
            delta_color="inverse",
        )
        layout_columns[3].metric("자동 확장 박스", metrics.expanded_blocks)
        layout_columns[4].metric("자동 추가 본문", metrics.added_text_blocks)
        layout_columns[5].metric("분리된 섹션 제목", metrics.split_section_headings)
        layout_columns[6].metric(
            "복구된 제목/저자",
            f"{metrics.recovered_title_blocks}/{metrics.recovered_author_blocks}",
        )

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

    _render_layout_ocr_tabs(
        st,
        client,
        document,
        context_key="review",
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
            "레이아웃·OCR 관리자 교정 열기",
            client.viewer_url(document.document_id, editable=True),
            help=(
                "레이아웃 단계에서는 영역 유형·좌표·누락 박스를 교정하고, "
                "OCR 단계에서는 인식 원문과 검수 상태를 교정합니다."
            ),
        )
        if document.phase == "layout_review" and st.button(
            "겹친 자동 레이아웃 박스 재정리",
            help=(
                "동일 페이지에서 거의 같은 위치에 중복 검출된 자동 박스와 "
                "여러 하위 영역을 감싸는 큰 컨테이너 박스를 다시 제거합니다."
            ),
        ):
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

    with st.expander("RAG 검색 DB 적재"):
        if document.status == "ingested":
            st.info(f"현재 문서는 RAG 검색 DB에 적재됐습니다. paper_id={document.paper_id}")
        elif document.phase != "ready_to_ingest":
            st.info(
                "레이아웃·OCR 품질 판정을 통과하거나 관리자 승인을 완료해야 적재할 수 있습니다."
            )
        if st.button(
            "현재 문서를 RAG 검색 DB에 적재",
            disabled=(document.status == "ingested" or document.phase != "ready_to_ingest"),
            type="primary",
            help=(
                "검수된 OCR 텍스트를 LLM으로 정제·요약하고 임베딩한 뒤 "
                "PostgreSQL과 pgvector에 저장해 검색·엑셀 결과에 포함합니다."
            ),
        ):
            try:
                with st.spinner("LLM 정제, 임베딩, DB 적재를 진행하고 있습니다..."):
                    result = client.ingest_document(document.document_id)
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"적재에 실패했습니다: {exc}")
            else:
                st.success(f"paper_id={result.paper_id}로 적재했습니다: {result.totals}")


def _render_layout_ocr_tabs(
    st: Any,
    client: ApiClient,
    document: ReviewDocument,
    *,
    context_key: str,
) -> None:
    ocr_count = _ocr_block_count(document)
    display_mode = st.segmented_control(
        "검수 화면",
        ["layout", "ocr"],
        default="layout",
        format_func=lambda value: (
            f"레이아웃 오버레이 ({len(document.blocks)})"
            if value == "layout"
            else f"OCR 결과 ({ocr_count})"
        ),
        key=f"{context_key}_display_mode_{document.document_id}",
    )
    if display_mode == "layout":
        st.iframe(
            client.viewer_url(document.document_id, editable=False),
            height=900,
        )
    else:
        _render_ocr_results(st, document, context_key=context_key)


def _render_ocr_results(
    st: Any,
    document: ReviewDocument,
    *,
    context_key: str,
) -> None:
    ocr_blocks = [block for block in document.blocks if block.ocr_text.strip()]
    if not ocr_blocks:
        st.info("이 문서는 아직 레이아웃 단계이며 OCR 결과가 없습니다.")
        return

    page_options = [0] + sorted({block.page for block in ocr_blocks})
    page = st.selectbox(
        "페이지",
        page_options,
        format_func=lambda value: "전체" if value == 0 else f"{value}쪽",
        key=f"{context_key}_ocr_page_{document.document_id}",
    )
    page_blocks = [block for block in ocr_blocks if page == 0 or block.page == page]
    block_types = sorted({block.block_type for block in page_blocks})
    selected_types = st.multiselect(
        "영역 유형",
        block_types,
        default=block_types,
        format_func=lambda value: BLOCK_LABELS.get(value, value),
        key=f"{context_key}_ocr_types_{document.document_id}_{page}",
    )
    filtered_blocks = [block for block in page_blocks if block.block_type in selected_types]
    if not filtered_blocks:
        st.info("선택한 조건에 해당하는 OCR 영역이 없습니다.")
        return

    rows = [
        {
            "페이지": block.page,
            "순서": block.order,
            "영역 유형": BLOCK_LABELS.get(block.block_type, block.block_type),
            "OCR 엔진": block.ocr_engine or "-",
            "신뢰도": round(block.confidence, 3) if block.confidence is not None else None,
            "모델 OCR 원문": block.ocr_text,
        }
        for block in filtered_blocks
    ]
    st.dataframe(rows, hide_index=True, width="stretch", height=420)

    blocks_by_id = {block.block_id: block for block in filtered_blocks}
    block_ids = list(blocks_by_id)
    block_key = f"{context_key}_ocr_block_{document.document_id}"
    if st.session_state.get(block_key) not in blocks_by_id:
        st.session_state[block_key] = block_ids[0]
    selected_block_id = st.selectbox(
        "OCR 영역 원문 상세",
        block_ids,
        format_func=lambda block_id: _ocr_block_label(blocks_by_id[block_id]),
        key=block_key,
    )
    selected_block = blocks_by_id[selected_block_id]
    detail_columns = st.columns(4)
    detail_columns[0].metric("페이지", selected_block.page)
    detail_columns[1].metric(
        "영역 유형",
        BLOCK_LABELS.get(selected_block.block_type, selected_block.block_type),
    )
    detail_columns[2].metric("OCR 엔진", selected_block.ocr_engine or "-")
    detail_columns[3].metric(
        "신뢰도",
        f"{selected_block.confidence:.3f}" if selected_block.confidence is not None else "-",
    )
    st.text_area(
        "모델 OCR 원문",
        selected_block.ocr_text,
        height=260,
        disabled=True,
        key=f"{context_key}_ocr_text_{document.document_id}_{selected_block.block_id}",
    )
    if selected_block.corrected_text is not None:
        st.text_area(
            "검수·교정 텍스트",
            selected_block.corrected_text,
            height=220,
            disabled=True,
            key=(f"{context_key}_corrected_text_{document.document_id}_{selected_block.block_id}"),
        )


def _ocr_block_label(block: ReviewBlock) -> str:
    preview = " ".join(block.ocr_text.split())
    if len(preview) > 80:
        preview = preview[:77] + "..."
    block_type = BLOCK_LABELS.get(block.block_type, block.block_type)
    return f"{block.page}쪽 · {block_type} · {preview}"


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


def _render_result(
    st: Any,
    client: ApiClient,
    result: SearchMatched,
) -> None:
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

    _render_search_review_results(st, client, result)

    st.caption(
        "메타데이터, 섹션별 원문·요약, 단락별 원문·요약·키워드, 표 셀은 아래 엑셀에서 확인할 수 있습니다."
    )
    _render_download(st, client, result.result_id)


def _render_search_review_results(
    st: Any,
    client: ApiClient,
    result: SearchMatched,
) -> None:
    try:
        documents = client.list_documents()
    except (ApiUnavailable, httpx.HTTPError):
        st.warning("검색 논문의 저장된 레이아웃·OCR 결과를 불러오지 못했습니다.")
        return

    documents_by_paper: dict[int, ReviewDocument] = {}
    for document in documents:
        if document.paper_id is not None:
            documents_by_paper.setdefault(document.paper_id, document)

    candidates: list[tuple[str, PaperSummary, ReviewDocument]] = []
    primary_document = documents_by_paper.get(result.primary_paper.paper_id)
    if primary_document is not None:
        candidates.append(("대표 논문", result.primary_paper, primary_document))
    if result.related_paper is not None:
        related_document = documents_by_paper.get(result.related_paper.paper_id)
        if related_document is not None:
            candidates.append(("연관 논문", result.related_paper, related_document))

    if not candidates:
        st.info("검색 논문과 연결된 레이아웃·OCR 검수 문서가 없습니다.")
        return

    st.subheader("검색 논문의 레이아웃·OCR 추출 결과")
    candidates_by_id = {
        document.document_id: (role, paper, document) for role, paper, document in candidates
    }
    document_id = st.radio(
        "검수 결과",
        list(candidates_by_id),
        format_func=lambda item: f"{candidates_by_id[item][0]} · {candidates_by_id[item][1].title}",
        horizontal=True,
        key=f"search_review_document_{result.result_id}",
    )
    role, _, document = candidates_by_id[document_id]
    columns = st.columns(4)
    columns[0].metric("논문 역할", role)
    columns[1].metric("페이지", len(document.pages))
    columns[2].metric("레이아웃 영역", len(document.blocks))
    columns[3].metric("OCR 완료 영역", _ocr_block_count(document))
    _render_layout_ocr_tabs(
        st,
        client,
        document,
        context_key=f"search_{result.result_id}",
    )


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
