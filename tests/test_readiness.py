from paperrag.config import Settings
from paperrag.readiness import build_readiness_report


def test_readiness_rejects_development_substitutes() -> None:
    settings = Settings(
        _env_file=None,
        embed_encoder="hash",
        ingest_backend="simple",
        review_default_backend="docling",
        allow_degraded_results=True,
        allow_diagnostic_backends=True,
    )

    report = build_readiness_report(settings, check_external=False)

    assert report["status"] == "not_ready"
    assert "embedding_policy" in report["errors"]
    assert "full_ocr_policy" in report["errors"]
    assert "degraded_result_policy" in report["errors"]
    assert "diagnostic_backend_policy" in report["errors"]
