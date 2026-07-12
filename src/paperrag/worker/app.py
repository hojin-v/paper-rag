from typing import Any

from celery import Celery

from paperrag.config import get_settings

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
    timezone="Asia/Seoul",
    enable_utc=True,
)


@app.task(name="paperrag.ingest_review_document")
def ingest_review_document(document_id: str) -> dict[str, Any]:
    from paperrag.review.service import ReviewService

    result = ReviewService(settings).ingest(document_id)
    return result.model_dump(mode="json")
