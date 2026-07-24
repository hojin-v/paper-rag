"""paper-rag의 유일한 프런트엔드(Streamlit 앱).

이 프로젝트에는 화면이 딱 하나뿐이며 두 개의 탭으로 구성된다.
1. "레이아웃·OCR 검수" 탭(`_render_upload_review`): PDF를 업로드해 STEP 1~3(이미지화·레이아웃
   검출·OCR·자동 품질 판정)까지의 결과를 사람이 확인·교정·승인하는 "운영 품질 모니터"다.
   일반 검색 사용자는 이 탭을 보지 않으며, 관리자가 자동 처리 결과의 근거를 확인하고 모델
   개선 데이터를 만드는 용도다.
2. "RAG 검색·엑셀" 탭(`_render_search`): 일반 사용자가 자연어로 질의하면 정확 매칭 또는
   유사 키워드 제안(2단계 인터랙션, DESIGN.md §5.1)을 거쳐 대표/연관 논문과 엑셀 다운로드를
   보여주는 화면이다.

이 모듈은 `paperrag.ui.client.ApiClient`를 통해서만 백엔드와 통신한다(직접 DB 접근 없음).
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx

from paperrag.config import get_settings
from paperrag.logging_config import configure_logging
from paperrag.review.models import ReviewBlock, ReviewDocument
from paperrag.review.viewer import BLOCK_LABELS
from paperrag.search.schemas import KeywordCandidate, PaperSummary, SearchMatched, SearchSuggest
from paperrag.ui.client import ApiClient, ApiUnavailable

# Streamlit은 상호작용마다 이 스크립트를 처음부터 재실행하므로, configure_logging의
# idempotent 가드가 중복 핸들러 등록을 막아준다.
configure_logging(get_settings())


@dataclass(frozen=True, slots=True)
class _LayoutQualityMetrics:
    """레이아웃 자동 보정 결과를 화면에 표시하기 좋은 형태로 정리한 값 객체.

    필드 각각의 의미는 `_layout_quality_metrics()` 함수 docstring을 참고.
    """

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
    """블록(`ReviewBlock`) 단위 검수 상태(`review_status`)를 집계한 값 객체.

    `unreviewed`/`approved`/`corrected`/`rejected`는 `document.blocks`의 `review_status`
    개수를 그대로 센 것이고(총합 = 블록 수), `changed_from_detection`은 검수 상태와 무관하게
    "자동 검출 결과에서 사람이나 자동 보정이 실제로 값을 바꾼 블록 수"다(중복 카운트 아님,
    독립적인 지표). 이 두 축이 다르다는 점은 `_review_progress()`와
    `_filter_review_documents()`의 "검수 대기" 판정 근거에서 특히 중요하다.
    """

    unreviewed: int
    approved: int
    corrected: int
    rejected: int
    changed_from_detection: int


def _layout_quality_metrics(layout_quality: Any) -> _LayoutQualityMetrics:
    """`LayoutQuality`(서버가 계산한 원시 지표)를 화면에 그대로 보여줄 `_LayoutQualityMetrics`로 변환한다.

    STEP 2(레이아웃 검출) 단계에서 모델이 페이지 이미지의 텍스트 검출선을 자동 레이아웃 박스와
    대조해, 어떤 자동 보정을 얼마나 수행했는지 나타내는 지표를 사람이 읽을 숫자/비율로 바꾼다.
    - `detected_text_lines`: 페이지에서 실제로 검출된 텍스트 줄 수(레이아웃 박스와 무관한 기준선).
    - `initial/final_text_coverage`: 자동 보정 전/후 텍스트 검출선이 레이아웃 박스에 포함된 비율.
      `final`은 `initial`보다 낮을 수 없도록 `max()`로 보정한다(자동 보정은 커버리지를
      낮추지 않는다는 전제이며, 서버 값이 흔들려도 화면에는 역행하지 않게 방어).
    - `uncovered_text_lines`: 서버가 값을 안 주면 `detected_text_lines`와 `final_text_coverage`로
      역산한 기본값(`default_uncovered`)을 쓰고, 서버 값이 0인데 커버리지가 100% 미만이면
      (불일치 상황) 역시 기본값으로 대체해 "0줄 미포함"이라는 잘못된 인상을 주지 않게 한다.
    - `expanded_blocks`/`added_text_blocks`/`split_section_headings`: 박스 확장, 텍스트 검출선만
      있고 박스가 없던 자리에 본문 블록 자동 추가, 제목·저자가 뭉쳐 있던 섹션 헤더를 분리한 횟수.
    - `recovered_title_blocks`/`recovered_author_blocks`: 제목/저자 블록을 자동으로 복구한 횟수.
    """
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
    """OCR 텍스트가 실제로 채워진(공백이 아닌) 블록 수를 센다.

    레이아웃 단계(phase="layout_review")에서는 아직 OCR을 돌리지 않았으므로 항상 0이 된다.
    """
    return sum(bool(block.ocr_text.strip()) for block in document.blocks)


def _review_progress(document: ReviewDocument) -> _ReviewProgress:
    """문서의 블록들을 훑어 검수 상태(승인/교정/제외/미검수) 개수와 "원본 대비 변경" 개수를 집계한다.

    `changed_from_detection`은 `review_status`(사람이 명시적으로 승인/제외했는지)와는 별개로,
    자동 검출 결과 대비 실제 값이 달라진 블록을 센다. 아래 중 하나라도 해당하면 "변경됨"으로 센다.
    - `detected_bbox`가 없음: 애초에 자동 검출되지 않고 사람/자동 보정이 새로 추가한 블록.
    - 자동 검출 시점의 블록 유형(`detected_block_type`)과 현재 유형(`block_type`)이 다름.
    - 자동 검출 좌표(`detected_bbox`)와 현재 좌표(`bbox`)가 다름(둘 다 값이 있을 때만 비교).
    - 교정 텍스트(`corrected_text`)가 있고 그것이 원본 OCR 텍스트(`ocr_text`)와 다름.
    즉 "검수 대기 개수"(unreviewed)와 "원본 대비 변경 개수"(changed_from_detection)는 서로
    독립적인 집계축이다 — 승인된 블록도 원본과 달라져 있을 수 있고, 미검수 블록도 자동 검출
    그대로일 수 있다.
    """
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
    """문서 선택 셀렉트박스에 쓸 한 줄 요약 라벨을 만든다.

    파일명, 현재 처리 단계(phase), 페이지·검출 영역 수, OCR 완료 영역 수, 미검수 블록 수를
    한 문자열로 이어붙인다. 여기서 "미검수"는 `_review_progress(document).unreviewed`,
    즉 블록 단위 `review_status == "unreviewed"` 개수이며, 아래 `_filter_review_documents`의
    "검수 대기" scope와 같은 기준을 쓴다(다만 대시보드의 "레이아웃 단계"/"OCR 품질 예외"
    scope는 이것과 다른 축인 `document.phase`를 쓴다 — 자세한 내용은 그 함수 docstring 참고).
    """
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
    """문서 목록이 주어졌을 때 기본 선택 문서(가장 최근에 생성된 문서)의 ID를 고른다.

    빈 목록이면 선택할 문서가 없으므로 None을 돌려주고, 호출부는 이를 "검수할 문서 없음"으로
    처리한다.
    """
    if not documents:
        return None
    return max(documents, key=lambda document: document.created_at).document_id


def _filter_review_documents(
    documents: list[ReviewDocument],
    scope: str,
) -> list[ReviewDocument]:
    """검수 대시보드의 "문서 범위" 세그먼트(전체/검수 대기/레이아웃/OCR 예외/완료)에 맞춰 문서를 거른다.

    중요: 이 함수가 다루는 필터 기준은 서로 다른 두 집계 축을 섞어 쓴다.
    - "pending"(검수 대기)·"completed"(완료)는 **블록 단위** 기준이다: 아직 적재되지 않았고
      `_review_progress(document).unreviewed > 0`인, 즉 미검수 블록이 하나라도 남아있는
      문서를 "검수 대기"로 본다. 미검수 블록이 하나도 없으면(전부 승인/교정/제외됨)
      "완료"로 본다.
    - "layout"(레이아웃 단계)·"ocr_exception"(OCR 품질 예외)는 **문서 단위** 기준이다:
      서버가 자동으로 매긴 `document.phase`(layout_review / ocr_review / ready_to_ingest)만
      본다. phase는 자동 품질 판정 결과(`automation_quality.status`)로 정해지며 블록별
      검수 상태와는 별개로 관리된다.

    이 두 축이 다르기 때문에 대시보드 상단 "검수 대기"·"OCR 품질 예외" 숫자가 서로 정확히
    일치하지 않을 수 있다(둘 다 "검수가 필요하다"는 신호이지만 계산 근거가 다르다). 실제로
    `phase="ocr_review"`이면서 `unreviewed == 0`인 문서가 존재할 수 있다 — 예를 들어 문서에
    제목/저자 블록 자체가 검출되지 않아 자동 품질 판정이 "제목 미검출"을 사유로 phase를
    ocr_review로 묶어두지만, 되돌릴(unreviewed로 표시할) 구체적인 블록이 없어서 블록 단위
    미검수 개수는 0으로 남는 경우다. 두 지표를 같은 것으로 오해하지 않도록 주의.
    """
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
    """Streamlit 엔트리포인트. `streamlit run src/paperrag/ui/app.py`로 실행된다.

    화면을 준비 상태 표시 + 두 개 탭("레이아웃·OCR 검수", "RAG 검색·엑셀")으로 구성한다.
    `streamlit`을 함수 내부에서 import하는 이유는 이 모듈이 `streamlit run`으로만
    실행되고, 코어 패키지 임포트(`import paperrag.ui.client` 등)는 streamlit이 설치되지
    않아도 가능해야 하기 때문이다(CLAUDE.md의 "무거운 의존성은 optional import" 규칙).
    """
    import streamlit as st

    st.set_page_config(layout="wide")
    _ensure_state(st)
    settings = get_settings()
    client = ApiClient(
        settings.api_base_url,
        timeout_seconds=settings.api_timeout_seconds,
        public_base_url=settings.public_api_base_url,
        api_key=settings.api_key,
    )

    st.title("Paper RAG")
    _render_readiness(st, client)
    upload_tab, search_tab = st.tabs(["레이아웃·OCR 검수", "RAG 검색·엑셀"])

    with upload_tab:
        _render_upload_review(st, client)

    with search_tab:
        _render_search(st, client)


def _render_readiness(st: Any, client: ApiClient) -> None:
    """상단에 실사용 준비 상태 배너를 그린다.

    `client.readiness()`가 API 연결 자체에 실패하면(서버 미기동 등) 오류 배너만 띄우고
    끝낸다. 연결에는 성공했지만 실제 OCR·임베딩·LLM·DB 등 구성요소 중 일부가 아직 준비되지
    않았다면(`status != "ready"`) 어떤 항목이 빠졌는지 펼침 영역에 상세히 보여준다.
    """
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
    """일반 사용자의 검색 화면 전체 흐름을 그린다.

    2단계 인터랙션(DESIGN.md §5.1)을 그대로 따른다.
    1. 질의를 입력하고 제출하면 `_run_search`가 `POST /search`를 호출한다.
    2. 정확 매칭(`SearchMatched`)이면 곧바로 결과 카드를 그리고,
       매칭 실패로 유사 키워드 제안(`SearchSuggest`)이 오면 `_render_suggestions`가
       후보 3개 중 하나를 고르는 라디오 버튼과 "이 키워드로 검색" 버튼을 그린다.
    3. 세션 상태(`st.session_state["result"]`/`["suggestion"]`)에 남아있는 이전 결과나
       제안을 다시 렌더링해, 위젯 상호작용으로 인한 재실행(rerun) 후에도 화면이 유지되게 한다.

    질의 키워드 추출은 항상 Ollama LLM으로 이뤄진다(2026-07-22 결정 — 형태소
    분석 빠른 경로는 더 이상 사용자가 고르는 옵션이 아니라 LLM 실패 시의 내부
    안전망일 뿐이다). 그래서 검색 폼에는 "AI 검색 사용" 같은 경로 선택 체크박스가
    없다 — 매 검색이 LLM 호출 1회(직렬 처리, 실측 건당 18~20초) 이상을 수반하며,
    이 지연·동시성 특성에 필요한 서버 사양은
    `docs/reports/assessments/2026-07-22-llm-search-capacity.md`에 따로 정리했다.

    연관 논문 포함/표 포함/초록 포함/섹션 선택은 전부 "검색이 무엇을 찾을지"가
    아니라 "찾은 결과를 엑셀에 어떻게 담을지"를 정하는 산출물 옵션이라, 이 폼에
    두지 않는다. 첫 검색은 항상 기본값(연관 논문·표·초록 전부 포함, 섹션 필터
    없음)으로 실행되고, 결과가 나온 뒤 `_render_output_options`에서 그 결과를
    바탕으로 산출물 구성을 다시 골라 같은 질의를 재검색한다(어떤 섹션 제목이
    있는지 자체가 검색 결과를 봐야 알 수 있는 정보이기도 하다).
    """
    st.subheader("논문 검색")

    with st.form("search_form"):
        query = st.text_input("질의", value=st.session_state["query"])
        submitted = st.form_submit_button("검색")

    if submitted:
        st.session_state["query"] = query
        # 첫 검색은 항상 기본 구성(전체 포함, 섹션 필터 없음)으로 실행한다.
        # 산출물을 좁히는 건 결과를 본 뒤 _render_output_options의 몫이다.
        st.session_state["section_query"] = None
        st.session_state["include_related"] = True
        st.session_state["include_tables"] = True
        st.session_state["include_abstract"] = True
        with st.spinner(
            "AI가 질의를 분석하고 관련 논문을 찾고 있습니다"
            "(LLM 직렬 처리라 잠시 걸릴 수 있습니다)..."
        ):
            _run_search(
                st,
                client,
                query,
                section_query=None,
                include_related=True,
                include_tables=True,
                include_abstract=True,
            )

    suggestion = st.session_state.get("suggestion")
    if suggestion is not None:
        _render_suggestions(st, client, suggestion)

    result = st.session_state.get("result")
    if result is not None:
        _render_result(st, client, result)


def _render_upload_review(st: Any, client: ApiClient) -> None:
    """관리자용 검수 대시보드 전체를 그린다("레이아웃·OCR 검수" 탭).

    이 함수는 다음을 순서대로 그린다.
    1. 상단 요약 지표(분석 문서 수, 검수 대기, OCR 품질 예외, 원본 대비 변경 영역 수)와
       "문서 범위" 세그먼트 컨트롤 — 뒤에서 설명할 두 집계 축(블록 단위 vs 문서 단위)이
       섞여 있으므로 숫자 해석에 주의가 필요하다(아래 상세 설명 참고).
    2. "새 PDF 분석 실행" 펼침 영역 — 업로드하면 `client.upload_document`로 STEP 1~2까지만
       먼저 실행한다.
    3. 선택된 단일 문서의 상세 화면 — 단계(phase), 검수 진행 상황(`_review_progress`),
       레이아웃 자동 보정 지표(`_layout_quality_metrics`), 자동 품질 판정
       (`document.automation_quality`), 레이아웃/OCR 결과 뷰(`_render_layout_ocr_tabs`),
       다음 자동화 단계 실행 버튼, 관리자 진단·교정 도구, RAG 적재 버튼.

    "검수 대기" 지표와 "레이아웃 단계"/"OCR 품질 예외" 지표는 서로 다른 집계 축을 쓴다는
    점이 이 화면에서 가장 헷갈리기 쉬운 부분이다.
    - "검수 대기"(및 "완료") = `_filter_review_documents(..., "pending"/"completed")` =
      **블록 단위** 집계: 문서 안의 어떤 블록이든 `review_status == "unreviewed"`가 하나라도
      남아있으면 그 문서를 검수 대기로 센다.
    - "레이아웃 단계"/"OCR 품질 예외" = `_filter_review_documents(..., "layout"/"ocr_exception")`
      = **문서 단위** 집계: 서버가 자동 품질 판정으로 매긴 `document.phase` 값만 본다.
    두 축이 다르므로 "검수 대기" 문서 수와 "OCR 품질 예외" 문서 수는 정확히 일치하지 않을 수
    있다. 예를 들어 `phase="ocr_review"`이지만 미검수 블록 수가 0인 문서가 있을 수 있다 —
    제목/저자 블록 자체가 레이아웃 단계에서 검출되지 않아, 자동 품질 판정이 "제목 미검출"을
    사유로 phase를 ocr_review로 남기더라도 되돌려 표시할(=unreviewed로 만들) 구체적인
    블록이 없기 때문이다. 이 화면의 숫자를 볼 때는 이 차이를 감안해야 한다.
    """
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
        # 아래 pending_documents/exception_documents는 각각 블록 단위·문서 단위 축으로 계산되므로
        # 두 개수를 단순 비교하면 안 된다 — 위 함수 docstring의 불일치 설명 참고.
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
            # 선택한 범위에 문서가 하나도 없으면(예: "검수 대기" 범위인데 전부 검수 완료된 경우)
            # 빈 화면을 보여주는 대신 전체 문서 목록으로 폴백해 최소한 뭔가는 선택할 수 있게 한다.
            st.info("선택한 범위에 해당하는 검수 문서가 없습니다.")
            visible_documents = recent_documents
        labels = {document.document_id: _document_label(document) for document in visible_documents}
        document_ids = list(labels)
        current_document_id = st.session_state.get("review_document_id")
        if current_document_id not in labels:
            # 세션에 남아있던 선택 문서가 현재 범위 밖으로 벗어났다면(범위 전환 등으로) 그 범위
            # 안에서 가장 최근 문서를 새 기본값으로 잡는다.
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

    # automation_quality는 STEP 2(레이아웃)~STEP 3(OCR) 결과에 대해 서버가 내린 자동 판정이며,
    # document.phase(layout_review/ocr_review/ready_to_ingest)를 결정한 직접적인 근거다.
    # quality.status가 "needs_review"면 phase가 ocr_review로 격리된 상태라는 뜻이다.
    quality = document.automation_quality
    if quality is not None:
        quality_columns = st.columns(4)
        quality_columns[0].metric("자동 판정", "합격" if quality.status == "passed" else "예외")
        quality_columns[1].metric("OCR 영역 인식률", f"{quality.ocr_coverage:.1%}")
        # 제목이 검출됐는지(title_detected)와, 검출된 제목이 페이지 전반에서 일관되는지
        # (title_consistent) 두 조건을 모두 만족해야 "성공"으로 표시한다.
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
        # 200 DPI 논문 1페이지가 5분 이상 걸릴 수 있다는 실측(docs/guide/10) 때문에
        # 동기 호출 대신 Celery 큐에 제출하고 폴링한다. 진행 중인 task_id를
        # session_state에 들고 있다가, 완료 전까지는 짧게 대기한 뒤 자동으로
        # rerun해 사람이 수동 새로고침을 누르지 않아도 진행 상황이 갱신되게 한다.
        ocr_task_key = f"ocr_task_{document.document_id}"
        pending_task_id = st.session_state.get(ocr_task_key)
        if pending_task_id:
            try:
                status = client.job_status(pending_task_id)
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"작업 상태 조회에 실패했습니다: {exc}")
                st.session_state[ocr_task_key] = None
            else:
                if status["status"] in {"pending", "started"}:
                    st.info("영역별 OCR과 품질 판정을 백그라운드에서 처리 중입니다...")
                    time.sleep(2)
                    st.rerun()
                elif status["status"] == "success":
                    st.session_state[ocr_task_key] = None
                    st.success("자동 OCR과 품질 판정을 완료했습니다.")
                    st.rerun()
                else:
                    st.session_state[ocr_task_key] = None
                    st.error(f"자동 OCR 실행에 실패했습니다: {status.get('error')}")
        elif st.button("다음 자동화 단계: 영역별 OCR·품질 판정 실행", type="primary"):
            try:
                task_id = client.run_automatic_ocr_async(document.document_id)
            except (ApiUnavailable, httpx.HTTPError) as exc:
                st.error(f"자동 OCR 제출에 실패했습니다: {exc}")
            else:
                st.session_state[ocr_task_key] = task_id
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
    """문서 하나에 대해 "레이아웃 오버레이"(iframe 뷰어) / "OCR 결과"(표+상세) 전환 화면을 그린다.

    검수 대시보드(`_render_upload_review`)와 검색 결과 화면(`_render_search_review_results`)
    양쪽에서 재사용하므로, 위젯 key 충돌을 막기 위해 호출부마다 다른 `context_key`를 받는다.
    """
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
    """OCR 텍스트가 있는 블록을 페이지·영역 유형으로 걸러 표와 상세 보기로 보여준다.

    페이지 선택(0=전체)과 영역 유형 다중 선택을 순서대로 적용해 후보를 좁힌 뒤, 표에서
    고른 블록 하나의 OCR 원문(과 있다면 검수·교정 텍스트)을 하단에 크게 보여준다.
    """
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
    """OCR 영역 선택 드롭다운에 쓸 라벨(페이지·영역 유형·원문 80자 미리보기)을 만든다."""
    preview = " ".join(block.ocr_text.split())
    if len(preview) > 80:
        preview = preview[:77] + "..."
    block_type = BLOCK_LABELS.get(block.block_type, block.block_type)
    return f"{block.page}쪽 · {block_type} · {preview}"


def _ensure_state(st: Any) -> None:
    """`st.session_state`에 필요한 키가 없으면 기본값으로 초기화한다.

    Streamlit은 위젯 상호작용마다 스크립트 전체를 처음부터 다시 실행하므로, 검색/제안/엑셀
    캐시/검수 대상 문서 선택처럼 재실행 사이에 유지해야 하는 상태는 `session_state`에 보관해야
    한다. 이 함수는 `main()` 시작 시 한 번 호출돼 이미 값이 있는 키는 건드리지 않는다.
    """
    defaults = {
        "query": "",
        "section_query": None,
        "include_related": True,
        "include_tables": True,
        "include_abstract": True,
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


def _run_search(
    st: Any,
    client: ApiClient,
    query: str,
    *,
    section_query: list[str] | None = None,
    include_related: bool = True,
    include_tables: bool = True,
    include_abstract: bool = True,
) -> None:
    """검색 폼 제출을 처리한다: 질의 검증 → API 호출 → 정확 매칭/유사 키워드 제안 분기 저장.

    응답이 `SearchMatched`면 결과를 세션에 저장하고(`_set_result`), 그 외(유사 키워드 제안
    `SearchSuggest`)면 제안을 세션에 저장한다(`_set_suggestion`). 실제 렌더링은 이 함수가
    아니라 `_render_search`가 세션 상태를 읽어서 수행한다. 옵션 인자는 그대로
    `client.search`에 전달된다. 결과 화면의 "결과물 구성"(`_render_output_options`)도
    같은 함수를 재사용해 include_related/include_tables/include_abstract/section_query만
    바꿔 다시 호출한다.
    """
    normalized_query = query.strip()
    if not normalized_query:
        st.warning("검색할 질의를 입력하세요.")
        return

    try:
        response = client.search(
            normalized_query,
            section_query=section_query,
            include_related=include_related,
            include_tables=include_tables,
            include_abstract=include_abstract,
        )
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
    """정확 매칭에 실패했을 때 유사 키워드 후보(최대 3개) 중 하나를 고르는 화면을 그린다.

    사용자가 라디오에서 키워드를 고르고 "이 키워드로 검색"을 누르면, 제안과 함께 받은
    `session_id`(세션에 없으면 `suggestion.session_id`로 대체)와 선택한 `keyword_id`로
    `client.select()`(`POST /search/select`)를 호출해 확정 결과를 받는다.
    """
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
        st.rerun()


def _render_result(
    st: Any,
    client: ApiClient,
    result: SearchMatched,
) -> None:
    """확정된 검색 결과(대표/연관 논문 카드, 검수 연동, 엑셀 다운로드)를 그린다."""
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
    _render_output_options(st, client, result)

    st.caption(
        "메타데이터, 섹션별 원문·요약, 단락별 원문·요약·키워드, 표 셀은 아래 엑셀에서 확인할 수 있습니다."
    )
    _render_download(st, client, result.result_id)


def _render_output_options(st: Any, client: ApiClient, result: SearchMatched) -> None:
    """검색이 아니라 "찾은 결과를 엑셀에 어떻게 담을지"를 정하는 산출물 옵션을 그린다.

    연관 논문 포함/표 포함/초록 포함/섹션 선택 네 가지는 전부 이미 확정된 검색
    결과에서 무엇을 보여줄지만 좁히는 것이라(대표/연관 논문 선정 자체에는
    영향 없음), 검색 폼이 아니라 결과 화면에 둔다. 섹션 선택지(`result.available_sections`)는
    이 논문에 실제로 존재하는 section_name을 문서 등장 순서로 합친 목록이라
    자유 텍스트 입력 없이 바로 고를 수 있고, 여러 섹션을 동시에 골라 결과·엑셀
    단락을 그 조합으로 좁힐 수 있다(비워두면 전체 보기). "적용"을 누르면 직전과
    같은 질의(`query`)를 유지한 채 이 네 옵션만 바꿔 `_run_search`를 다시 호출해
    결과(및 엑셀)를 재구성한다.
    """
    st.subheader("결과물 구성")

    with st.form(f"output_options_{result.result_id}"):
        option_columns = st.columns(3)
        include_related = option_columns[0].checkbox(
            "연관 논문 포함",
            value=st.session_state["include_related"],
            help="끄면 연관 논문 조회를 생략하고 결과·엑셀에서 연관 논문 항목을 뺍니다.",
        )
        include_tables = option_columns[1].checkbox(
            "표 포함",
            value=st.session_state["include_tables"],
            help="끄면 표 조회를 생략하고 엑셀에 표 데이터/표 셀 시트를 만들지 않습니다.",
        )
        include_abstract = option_columns[2].checkbox(
            "초록 포함",
            value=st.session_state["include_abstract"],
            help="끄면 논문 정보 시트의 초록 원문·초록 요약 칸을 비웁니다.",
        )

        section_query: list[str] | None = st.session_state.get("section_query")
        if result.available_sections:
            current = [
                name
                for name in (st.session_state.get("section_query") or [])
                if name in result.available_sections
            ]
            chosen = st.multiselect(
                "특정 섹션만 포함",
                result.available_sections,
                default=current,
                help="이 논문에 실제로 있는 섹션 제목 중 하나 이상을 골라 결과·엑셀 단락을 좁힙니다. 아무것도 안 고르면 전체 보기입니다.",
            )
            section_query = chosen or None

        applied = st.form_submit_button("이 구성으로 다시 만들기")

    if applied:
        st.session_state["include_related"] = include_related
        st.session_state["include_tables"] = include_tables
        st.session_state["include_abstract"] = include_abstract
        st.session_state["section_query"] = section_query
        with st.spinner("선택한 구성으로 결과를 다시 만들고 있습니다..."):
            _run_search(
                st,
                client,
                st.session_state["query"],
                section_query=section_query,
                include_related=include_related,
                include_tables=include_tables,
                include_abstract=include_abstract,
            )
        st.rerun()


def _render_search_review_results(
    st: Any,
    client: ApiClient,
    result: SearchMatched,
) -> None:
    """검색 결과로 나온 대표/연관 논문이 어떤 레이아웃·OCR 검수 문서에서 왔는지 연결해 보여준다.

    검수 문서(`ReviewDocument`)와 검색 결과 논문(`PaperSummary`)은 `paper_id`로 연결된다
    (검수 문서를 `client.ingest_document()`로 적재할 때 `paper_id`가 채워진다). 대표/연관
    논문 각각에 대응하는 검수 문서가 있으면 라디오로 골라 `_render_layout_ocr_tabs`로
    레이아웃 오버레이·OCR 결과를 다시 보여준다. 이렇게 하면 검색 사용자도 결과 논문의
    원본 OCR 근거를 확인할 수 있다.
    """
    try:
        documents = client.list_documents()
    except (ApiUnavailable, httpx.HTTPError):
        st.warning("검색 논문의 저장된 레이아웃·OCR 결과를 불러오지 못했습니다.")
        return

    documents_by_paper: dict[int, ReviewDocument] = {}
    for document in documents:
        if document.paper_id is not None:
            # 같은 paper_id로 검수 문서가 여러 개 있어도(재분석 등) 먼저 나온 것을 유지한다.
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
    """대표/연관 논문 카드 하나(관련도 설명·제목·저자·연도·저널·매칭 키워드·점수·선정 사유)를 그린다.

    relevance_summary(LLM이 이 논문에서 가장 유사한 단락을 근거로 생성한 "왜
    이 논문인가" 설명)를 제목 바로 아래에 강조 표시한다 — 사용자가 엑셀을
    내려받기 전에 이 논문이 실제로 원하는 내용인지 먼저 판단할 수 있게 하기
    위함이다. 생성에 실패해 relevance_summary가 없으면(드묾, 단락 자체가 없는
    경우 등) 이 블록은 그냥 생략된다.
    """
    with st.container(border=True):
        st.markdown(f"#### {label}")
        st.markdown(f"**{paper.title}**")
        if paper.relevance_summary:
            st.info(paper.relevance_summary)
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
    """검색 결과의 엑셀 파일을 내려받아 세션에 캐시하고 다운로드 버튼을 그린다.

    `result_id`가 이전에 캐시한 것과 다르면(다른 검색 결과로 바뀜) 캐시를 비우고 새로
    받아온다. 같은 결과를 다시 그릴 때(예: 다른 위젯 조작으로 인한 rerun)는 이미 받아둔
    바이트를 재사용해 API를 중복 호출하지 않는다.
    """
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
    """확정 검색 결과를 세션에 저장하고, 이전 제안·엑셀 캐시를 지운다(상태 전이: 제안 → 결과)."""
    st.session_state["result"] = result
    st.session_state["suggestion"] = None
    st.session_state["suggest_session_id"] = None
    st.session_state["excel_result_id"] = None
    st.session_state["excel_bytes"] = None


def _set_suggestion(st: Any, suggestion: SearchSuggest) -> None:
    """유사 키워드 제안을 세션에 저장하고, 이전 결과·엑셀 캐시를 지운다(상태 전이: 결과 → 제안)."""
    st.session_state["result"] = None
    st.session_state["suggestion"] = suggestion
    st.session_state["suggest_session_id"] = suggestion.session_id
    st.session_state["excel_result_id"] = None
    st.session_state["excel_bytes"] = None


def _format_candidate(candidate: KeywordCandidate) -> str:
    """유사 키워드 후보 라디오 항목에 쓸 "키워드 (유사도)" 라벨을 만든다."""
    return f"{candidate.keyword} ({candidate.similarity:.3f})"


if __name__ == "__main__":
    main()
