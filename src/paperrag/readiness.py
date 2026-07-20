"""
`/ready` 엔드포인트가 사용하는 "운영 준비 상태" 점검 로직.

docs/guide/10-production-readiness.md의 표현대로 "테스트용 대체 결과를 운영 결과로 오인하지
않도록" 하는 것이 이 모듈의 목적이다. `/health`는 프로세스 생존만 확인하지만, `/ready`는 실제로
논문을 처리할 수 있는 구성인지(로컬 모델 파일 존재, hash 임베딩이 아닌 실제 BGE-M3, 지정 LLM이
Ollama에 로드돼 있는지, DB 연결 등)를 폴백 없이 점검한다. 여기서 "ok"가 아니면 그 구성요소로는
운영 품질의 결과를 만들 수 없다고 간주한다.
"""

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
    """개별 점검 항목(구성요소) 하나의 결과.

    `status`는 "ok" | "warning" | "error" 중 하나(문자열 리터럴로 강제하지 않는 이유는 항목마다
    나열 방식이 달라 호출부에서 유연하게 만들기 위함). `detail`은 사람이 읽는 한국어 설명, `metadata`
    는 원인 추적에 필요한 부가 정보(경로, 응답 필드 등)를 담는다.
    """

    status: str
    detail: str
    metadata: dict[str, Any] = field(default_factory=dict)


def build_readiness_report(
    settings: Settings,
    *,
    check_external: bool = True,
) -> dict[str, Any]:
    """운영 결과를 만들 수 있는 구성인지 폴백 없이 점검한다.

    로컬 정책/파일 점검(`_local_components`)에 더해, `check_external=True`(기본값)이면 실제
    PostgreSQL·임베딩 서버·Ollama에 네트워크 호출까지 수행해 "설정만 맞고 실제로는 응답하지 않는"
    상황도 잡아낸다. 단위 테스트 등 외부 서비스가 없는 환경에서는 `check_external=False`로 로컬
    점검만 수행할 수 있다.

    반환값의 `status`는 하나라도 "error"인 구성요소가 있으면 "not_ready", 없으면 "ready"다.
    "warning"은 준비 상태를 막지는 않지만(예: 모델 경로 미설정으로 Paddle 캐시에 의존) 사용자에게
    알려야 하는 항목이다.
    """
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
    """네트워크 호출 없이 설정값과 로컬 파일 시스템만으로 판정 가능한 점검 항목들.

    운영 정책(전체 OCR 단일 경로, 실제 임베딩 사용, 폴백 금지)과 Paddle 파이썬 모듈·로컬 모델
    파일의 존재 여부를 확인한다. DB/임베딩 서버/LLM처럼 네트워크 응답이 필요한 항목은
    `build_readiness_report`에서 별도로 추가한다.
    """
    components: dict[str, ComponentStatus] = {}
    # DESIGN.md 기준 운영 경로는 "모든 PDF를 Paddle 전체 OCR로 처리"하는 단일 경로다. 수집(ingest)과
    # 업로드(review) 양쪽 backend가 모두 paddle이 아니면 디지털/스캔 PDF에 다른 경로가 섞여
    # 설계와 어긋난다.
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
    # hash 인코더는 결정적이지만 의미 없는 벡터(단위 테스트 전용)이므로, 운영 설정에서
    # embed_encoder가 "st"(sentence-transformers 실제 모델)가 아니면 검색 품질을 보장할 수 없다.
    components["embedding_policy"] = (
        ComponentStatus("ok", "실제 sentence-transformers 임베딩을 사용합니다.")
        if settings.embed_encoder == "st"
        else ComponentStatus(
            "error",
            "hash 임베딩은 결정적 테스트 값일 뿐 의미 검색 결과가 아닙니다.",
            {"encoder": settings.embed_encoder},
        )
    )
    # 스키마의 모든 VECTOR 컬럼이 1024차원(BGE-M3, ADR-0001)으로 고정돼 있으므로 embed_dim이
    # 어긋나면 적재 시점에 차원 불일치로 실패한다 — 사전에 잡아내기 위한 검사.
    components["embedding_dimension"] = (
        ComponentStatus("ok", "DB VECTOR(1024)와 임베딩 차원이 일치합니다.")
        if settings.embed_dim == 1024
        else ComponentStatus(
            "error",
            "현재 DB 스키마는 VECTOR(1024)이므로 다른 차원은 적재할 수 없습니다.",
            {"configured_dim": settings.embed_dim},
        )
    )
    # allow_degraded_results=true는 LLM 실패 시 규칙 기반 대체값을 "정상 성공"으로 위장할 수
    # 있게 하는 스위치다. development에서는 디버깅 편의를 위해 허용하지만 production에서 켜져
    # 있으면 실패를 감춘 채 품질을 오인시키는 상태이므로 error로 판정한다.
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
    # allow_diagnostic_backends=true는 OCR 없이 텍스트 레이어만 쓰는 simple/docling 등 진단용
    # backend를 운영 API 업로드 경로에도 노출한다. 이 backend들은 비교 진단 전용이라(DESIGN.md §2)
    # production에서 허용돼 있으면 error다.
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

    # 전체 OCR 경로가 실제로 동작하려면 세 파이썬 패키지가 설치·임포트 가능해야 한다. import만
    # 시도해도 시간이 걸릴 수 있는 무거운 패키지들이라 여기서 한 번만 확인한다.
    for module in ("pymupdf", "paddle", "paddleocr"):
        components[f"python_module:{module}"] = _module_status(module)

    # 모델 파일 자체는 용량이 커서 git에 커밋하지 않고 scripts/download_paddle_models.py로 별도
    # 준비한다. 여기서는 Settings에 설정된 로컬 경로에 실제로 모델 파일이 존재하는지만 확인한다.
    model_paths = [
        ("layout_model", settings.paddle_layout_model_dir),
        ("text_detection_model", settings.paddle_text_detection_model_dir),
        ("ocr_model", settings.paddle_ocr_model_dir),
    ]
    if settings.paddle_use_table_recognition:
        # 표 구조 인식은 기본 비활성 기능이므로, 켜져 있을 때만 관련 모델 경로를 점검 대상에 추가한다.
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
            # 경로를 명시적으로 비워둔 경우, Paddle 라이브러리 자체의 기본 캐시 탐색에 맡긴다는
            # 뜻이므로 실패는 아니지만 확인이 안 되니 warning으로만 표시한다.
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
    """PostgreSQL에 실제로 연결해 간단한 질의(`SELECT 1`)가 성공하는지 확인한다."""
    try:
        with get_engine(settings).connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return ComponentStatus("error", f"PostgreSQL 연결 실패: {exc}")
    return ComponentStatus("ok", "PostgreSQL 연결과 기본 질의에 성공했습니다.")


def _embedding_status(settings: Settings) -> ComponentStatus:
    """임베딩 HTTP 서버(`embed/server.py`)의 `/health`를 호출해 실제 운영 인코더인지 확인한다.

    타임아웃은 설정된 `embed_timeout_seconds`를 그대로 쓰지 않고 최대 10초로 캡을 씌운다 —
    임베딩 실제 호출은 대량 텍스트 처리로 수백 초가 걸릴 수 있어 여유 있게 잡혀 있지만, 헬스체크는
    "서버가 살아있는지"만 빠르게 확인하면 되므로 `/ready` 응답이 느려지지 않게 한다.
    """
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
    # 서버가 응답은 하더라도 hash 모드로 떠 있거나 차원이 설정과 다르면 실제로는 쓸 수 없는 상태다.
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
    """Ollama `/api/tags`로 설정된 LLM 모델이 실제로 pull되어 있는지 확인한다.

    LLM 호출 자체(생성 시간이 김)의 타임아웃과 별개로, 헬스체크는 `/ready` 응답 지연을 막기 위해
    10초로 캡을 씌운다(`_embedding_status`와 같은 이유).
    """
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
    # Ollama는 같은 모델이라도 태그(quant 버전 등)가 다르면 다른 이름으로 보고한다. 정확히 일치하는
    # 이름이 없어도 콜론 앞부분(모델 계열명)이 같으면 태그 차이로 보고 통과시킨다.
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
    """디렉터리가 존재하고 그 안에 파일이 하나 이상 있는지 재귀적으로 확인한다.

    경로만 있고 내용이 비어 있는 경우(예: 다운로드가 중간에 실패해 빈 디렉터리만 생성된 경우)를
    "모델이 준비됨"으로 오판하지 않기 위한 검사다.
    """
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))


def _module_status(module: str) -> ComponentStatus:
    """파이썬 모듈이 설치돼 있을 뿐 아니라 실제로 임포트까지 성공하는지 확인한다.

    `find_spec`으로 설치 여부를, 실제 `import_module`로 임포트 가능 여부(네이티브 의존성 누락 등
    설치는 됐지만 로드가 실패하는 경우까지)를 이중으로 점검한다.
    """
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
