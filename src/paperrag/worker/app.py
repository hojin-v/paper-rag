"""
Celery worker 앱 정의.

`PAPERRAG_CELERY_BROKER_URL`/`PAPERRAG_CELERY_RESULT_BACKEND`(둘 다 Redis, 기본은 서로 다른
논리 DB 0/1)를 사용하는 Celery 앱을 만들고, 검수 문서 OCR 처리 같은 무거운 작업을 태스크로
등록한다. `celery -A paperrag.worker.app worker` 형태로 별도 프로세스로 기동한다
(docs/guide/02-stack.md의 `worker` profile 참고).
"""

from typing import Any

from celery import Celery

from paperrag.config import get_settings

# 모듈 임포트 시점에 한 번만 설정을 읽는다 — worker 프로세스는 태스크마다 재시작되지 않으므로
# 태스크 실행 중 설정이 바뀔 걱정 없이 모듈 전역으로 캐시해도 안전하다.
settings = get_settings()
app = Celery(
    "paperrag",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # 온프레미스 운영 조직 기준 시간대로 맞춘다. UTC 내부 저장(enable_utc)과는 별개로 로그·스케줄
    # 표시에 사용된다.
    timezone="Asia/Seoul",
    enable_utc=True,
)


@app.task(name="paperrag.ingest_review_document")
def ingest_review_document(document_id: str) -> dict[str, Any]:
    """검수 대기 중인 문서 하나를 OCR·레이아웃 분석 파이프라인으로 처리하는 Celery 태스크.

    `paperrag.review.service.ReviewService`를 여기서 지연 임포트(함수 내부 import)하는 이유는,
    그 모듈이 Paddle/OCR 같은 무거운 선택적 의존성을 끌어오기 때문이다 — worker 앱 자체(Celery
    설정)는 이 무거운 의존성 없이도 임포트 가능해야 한다(CLAUDE.md 코드 규칙).
    결과는 Celery가 JSON으로 직렬화해 result backend(Redis)에 저장할 수 있도록
    `model_dump(mode="json")`으로 변환해 반환한다.
    """
    from paperrag.review.service import ReviewService

    result = ReviewService(settings).ingest(document_id)
    return result.model_dump(mode="json")


@app.task(name="paperrag.ingest_collected_paper")
def ingest_collected_paper(source_path: str) -> dict[str, Any]:
    """수집된 논문 PDF 1편을 STEP 1~8 IngestPipeline(운영 backend 그대로)으로 처리하는 태스크.

    `collect.cli`가 OpenAlex 등에서 새로 다운로드한 논문마다 이 태스크를 큐에
    넣어, 사람이 별도로 `python -m paperrag.ingest`를 수동 실행하지 않아도
    수집→적재가 자동으로 이어지게 한다("이미 서버에서 추출해놓는 것"의 추출
    과정 자동화). `ingest_review_document`와 같은 이유로 무거운 의존성
    (PaddleOCR, Ollama, 임베딩 클라이언트, Postgres 엔진)을 전부 함수 내부에서
    지연 임포트한다. layout backend는 항상 운영 정책(settings.ingest_backend,
    기본 paddle)을 그대로 따른다 — 사용자 업로드 논문의 단계별 검수 흐름
    (review.service의 layout_review→ocr_review)과 달리, 이 경로는 사람 개입 없이
    STEP 1~8을 한 번에 끝까지 실행한다.
    """
    from paperrag.ingest.embeddings import HttpEmbeddingClient
    from paperrag.ingest.layout import get_backend
    from paperrag.ingest.llm_enrich import OllamaClient
    from paperrag.ingest.pipeline import IngestPipeline
    from paperrag.ingest.repository import PostgresIngestRepository

    pipeline = IngestPipeline(
        PostgresIngestRepository(settings),
        get_backend(settings.ingest_backend),
        OllamaClient(settings),
        HttpEmbeddingClient(settings),
        settings=settings,
    )
    report = pipeline.run(source_path)
    return report.model_dump(mode="json")


@app.task(name="paperrag.run_automatic_ocr")
def run_automatic_ocr_task(document_id: str) -> dict[str, Any]:
    """레이아웃 검수 완료 문서의 영역별 OCR·자동 품질 판정을 백그라운드에서 실행한다.

    review.api의 동기 엔드포인트(`POST /documents/{id}/auto-ocr`)는 200 DPI 논문
    1페이지에 5분 이상 걸릴 수 있음이 실측됐다(docs/guide/10). 그 요청을 HTTP
    커넥션 하나로 붙잡고 있으면 리버스 프록시·브라우저 타임아웃에 취약하므로,
    이 태스크를 큐에 넣고 결과는 GET /jobs/{task_id}로 폴링하는 방식을 쓴다
    (review.api.submit_automatic_ocr / get_job_status, ui.client의 폴링 참고).
    """
    from paperrag.review.service import ReviewService

    result = ReviewService(settings).run_automatic_ocr(document_id)
    return result.model_dump(mode="json")


@app.task(name="paperrag.run_reviewed_ocr")
def run_reviewed_ocr_task(document_id: str) -> dict[str, Any]:
    """레이아웃 검수 완료 문서에 대해 사람이 확정한 블록 기준으로 OCR만 실행한다.

    `run_automatic_ocr_task`와 같은 이유(장시간 동기 HTTP 회피)로 비동기 큐를
    거친다. 관리자가 레이아웃을 직접 교정한 뒤(자동 승인이 아니라) OCR을 실행할
    때 쓰는 경로다.
    """
    from paperrag.review.service import ReviewService

    result = ReviewService(settings).run_reviewed_ocr(document_id)
    return result.model_dump(mode="json")
