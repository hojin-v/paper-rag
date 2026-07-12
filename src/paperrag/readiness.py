from __future__ import annotations

import importlib
import importlib.util
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from paperrag.config import Settings
from paperrag.db import get_engine


@dataclass(frozen=True)
class ComponentStatus:
    status: str
    detail: str
    metadata: dict[str, Any] = field(default_factory=dict)


def build_readiness_report(
    settings: Settings,
    *,
    check_external: bool = True,
) -> dict[str, Any]:
    """운영 결과를 만들 수 있는 구성인지 폴백 없이 점검한다."""
    components = _local_components(settings)
    if check_external:
        components.update(
            {
                "database": _database_status(settings),
                "embedding_service": _embedding_status(settings),
                "llm_service": _llm_status(settings),
            }
        )
    errors = [name for name, value in components.items() if value.status == "error"]
    warnings = [name for name, value in components.items() if value.status == "warning"]
    return {
        "status": "ready" if not errors else "not_ready",
        "runtime_mode": settings.runtime_mode,
        "errors": errors,
        "warnings": warnings,
        "components": {name: asdict(value) for name, value in components.items()},
    }


def _local_components(settings: Settings) -> dict[str, ComponentStatus]:
    components: dict[str, ComponentStatus] = {}
    components["full_ocr_policy"] = (
        ComponentStatus("ok", "모든 PDF를 Paddle 전체 OCR 경로로 처리합니다.")
        if settings.ingest_backend == "paddle" and settings.review_default_backend == "paddle"
        else ComponentStatus(
            "error",
            "운영 수집과 업로드 backend는 모두 paddle이어야 합니다.",
            {
                "ingest_backend": settings.ingest_backend,
                "review_default_backend": settings.review_default_backend,
            },
        )
    )
    components["embedding_policy"] = (
        ComponentStatus("ok", "실제 sentence-transformers 임베딩을 사용합니다.")
        if settings.embed_encoder == "st"
        else ComponentStatus(
            "error",
            "hash 임베딩은 결정적 테스트 값일 뿐 의미 검색 결과가 아닙니다.",
            {"encoder": settings.embed_encoder},
        )
    )
    components["embedding_dimension"] = (
        ComponentStatus("ok", "DB VECTOR(1024)와 임베딩 차원이 일치합니다.")
        if settings.embed_dim == 1024
        else ComponentStatus(
            "error",
            "현재 DB 스키마는 VECTOR(1024)이므로 다른 차원은 적재할 수 없습니다.",
            {"configured_dim": settings.embed_dim},
        )
    )
    if settings.runtime_mode == "production" and settings.allow_degraded_results:
        components["degraded_result_policy"] = ComponentStatus(
            "error",
            "운영 모드에서 LLM 실패 결과를 규칙 기반 값으로 대체하도록 허용했습니다.",
        )
    else:
        components["degraded_result_policy"] = ComponentStatus(
            "ok",
            "LLM 실패를 정상 결과로 위장하지 않습니다.",
        )
    if settings.runtime_mode == "production" and settings.allow_diagnostic_backends:
        components["diagnostic_backend_policy"] = ComponentStatus(
            "error",
            "운영 모드에서 OCR 없는 진단 backend가 허용되어 있습니다.",
        )
    else:
        components["diagnostic_backend_policy"] = ComponentStatus(
            "ok",
            "진단 backend는 운영 API에서 차단됩니다.",
        )

    for module in ("pymupdf", "paddle", "paddleocr"):
        components[f"python_module:{module}"] = _module_status(module)

    model_paths = [
        ("layout_model", settings.paddle_layout_model_dir),
        ("text_detection_model", settings.paddle_text_detection_model_dir),
        ("ocr_model", settings.paddle_ocr_model_dir),
    ]
    if settings.paddle_use_table_recognition:
        model_paths.extend(
            [
                (
                    "table_classification_model",
                    settings.paddle_table_classification_model_dir,
                ),
                ("wired_table_model", settings.paddle_wired_table_model_dir),
                ("wireless_table_model", settings.paddle_wireless_table_model_dir),
            ]
        )
    for name, path in model_paths:
        if path is None:
            components[name] = ComponentStatus(
                "warning",
                "명시적인 로컬 모델 경로가 없습니다. Paddle 캐시에 사전학습 모델이 있어야 합니다.",
            )
        elif not _directory_has_files(path):
            components[name] = ComponentStatus(
                "error",
                "설정한 모델 디렉터리가 없거나 비어 있습니다.",
                {"path": str(path)},
            )
        else:
            components[name] = ComponentStatus(
                "ok",
                "로컬 모델 디렉터리를 확인했습니다.",
                {"path": str(path)},
            )
    return components


def _database_status(settings: Settings) -> ComponentStatus:
    try:
        with get_engine(settings).connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return ComponentStatus("error", f"PostgreSQL 연결 실패: {exc}")
    return ComponentStatus("ok", "PostgreSQL 연결과 기본 질의에 성공했습니다.")


def _embedding_status(settings: Settings) -> ComponentStatus:
    try:
        response = httpx.get(
            f"{settings.embed_base_url.rstrip('/')}/health",
            timeout=min(settings.embed_timeout_seconds, 10),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return ComponentStatus("error", f"임베딩 서비스 점검 실패: {exc}")
    encoder = str(payload.get("encoder", ""))
    dimension = payload.get("dim")
    if encoder != "st" or dimension != settings.embed_dim:
        return ComponentStatus(
            "error",
            "임베딩 서비스가 운영 모델 또는 설정 차원과 일치하지 않습니다.",
            {"encoder": encoder, "dim": dimension},
        )
    return ComponentStatus(
        "ok",
        "실제 임베딩 모델 서비스가 응답했습니다.",
        {"encoder": encoder, "model": payload.get("model"), "dim": dimension},
    )


def _llm_status(settings: Settings) -> ComponentStatus:
    try:
        response = httpx.get(
            f"{settings.ollama_base_url.rstrip('/')}/api/tags",
            timeout=min(settings.llm_timeout_seconds, 10),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return ComponentStatus("error", f"Ollama 점검 실패: {exc}")
    model_names = {
        str(row.get("name", ""))
        for row in payload.get("models", [])
        if isinstance(row, dict)
    }
    configured = settings.llm_model
    if configured not in model_names and not any(
        name.split(":", 1)[0] == configured.split(":", 1)[0] for name in model_names
    ):
        return ComponentStatus(
            "error",
            "설정한 LLM이 Ollama에 없습니다.",
            {"configured_model": configured, "available_models": sorted(model_names)},
        )
    return ComponentStatus(
        "ok",
        "설정한 로컬 LLM을 확인했습니다.",
        {"configured_model": configured},
    )


def _directory_has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))


def _module_status(module: str) -> ComponentStatus:
    if importlib.util.find_spec(module) is None:
        return ComponentStatus("error", f"필수 모듈 {module}이 설치되어 있지 않습니다.")
    try:
        imported = importlib.import_module(module)
    except Exception as exc:
        return ComponentStatus("error", f"{module} 모듈 로드 실패: {exc}")
    version = getattr(imported, "__version__", None)
    return ComponentStatus(
        "ok",
        f"{module} 모듈을 실제로 불러왔습니다.",
        {"version": str(version)} if version else {},
    )
