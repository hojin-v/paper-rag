"""Paddle 공식 추론 모델(레이아웃·텍스트 검출·텍스트 인식)을 미리 내려받아 로컬 캐시에 채우는 스크립트.

docs/guide/10-production-readiness.md "1단계: 설치와 모델 준비"에서 설명하듯, PaddleX는
`official_models.get_model_path()`를 처음 호출하는 시점에 모델을 BOS(바이두 오브젝트 스토리지) 등
`PAPERRAG_PADDLEX_MODEL_SOURCE`가 가리키는 원격지에서 내려받아 `PAPERRAG_PADDLEX_CACHE_DIR/official_models`에
캐시한다. 인터넷이 차단된 폐쇄망에 반입하기 전, 인터넷이 되는 환경에서 이 스크립트를 미리 실행해
캐시 디렉터리를 만들어두면, 이후 반입한 캐시만으로 다운로드 없이 오프라인 추론이 가능해진다.
"""

from __future__ import annotations

import os
from pathlib import Path

from paperrag.config import Settings, get_settings


def download_models(settings: Settings) -> list[Path]:
    """설정된 Paddle 공식 추론 모델(레이아웃 1종 + OCR 검출/인식 2종) 3개를 로컬 캐시에 준비한다.

    반환값은 각 모델이 실제로 저장된 로컬 디렉터리 경로 목록이다. 캐시 위치와 다운로드 소스는
    환경변수(PADDLE_PDX_CACHE_HOME, PADDLE_PDX_MODEL_SOURCE)로 PaddleX 라이브러리에 전달하며,
    이 값들은 `Settings`의 `paddlex_cache_dir`/`paddlex_model_source`에서 가져온다.
    """
    # PaddleX 내부 라이브러리가 캐시 경로와 다운로드 소스(BOS 등)를 환경변수로만 읽으므로,
    # official_models를 import하기 전에 먼저 설정해야 한다.
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(settings.paddlex_cache_dir)
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = settings.paddlex_model_source

    from paddlex.inference.utils.official_models import (  # type: ignore[import-not-found]
        official_models,
    )

    # (모델명, .env에 설정된 기대 경로) 3종 — 레이아웃 검출, 텍스트 검출, 한국어·영어 텍스트 인식.
    requested = (
        (
            settings.paddle_layout_model_name,
            settings.paddle_layout_model_dir,
        ),
        (
            settings.paddle_text_detection_model_name,
            settings.paddle_text_detection_model_dir,
        ),
        (
            settings.paddle_text_recognition_model_name,
            settings.paddle_ocr_model_dir,
        ),
    )
    paths: list[Path] = []
    for model_name, configured_path in requested:
        # get_model_path()를 호출하는 순간 캐시에 없으면 원격에서 내려받고, 있으면 캐시 경로만 반환한다.
        resolved = Path(official_models.get_model_path(model_name))
        _validate_model_directory(resolved, model_name)
        # 실제 다운로드 경로와 .env에 명시한 PAPERRAG_PADDLE_*_MODEL_DIR이 어긋나면 운영 코드가
        # 엉뚱한 디렉터리를 참조하게 되므로, 조기에 실패시켜 설정 불일치를 알린다.
        if configured_path is not None and resolved.resolve() != configured_path.resolve():
            raise RuntimeError(
                f"{model_name} 다운로드 경로는 {resolved}이지만 설정 경로는 "
                f"{configured_path}입니다. PAPERRAG_PADDLE_*_MODEL_DIR을 맞추세요."
            )
        paths.append(resolved)
    return paths


def _validate_model_directory(path: Path, model_name: str) -> None:
    """PaddleX 추론 모델 디렉터리에 필수 파일(구조 정의·설정·가중치) 3종이 모두 있는지 확인한다.

    다운로드가 중간에 끊기거나 캐시가 손상된 경우 일부 파일만 남을 수 있어, 실제 로딩 시점이
    아니라 이 시점에 미리 검증해 원인을 명확히 알린다.
    """
    required = ("inference.json", "inference.yml", "inference.pdiparams")
    missing = [filename for filename in required if not (path / filename).is_file()]
    if missing:
        raise RuntimeError(f"{model_name} 모델 파일이 불완전합니다: {', '.join(missing)}")


def main() -> int:
    """설정된 3개 모델을 모두 준비하고, 각 모델의 로컬 경로를 한 줄씩 표준출력에 남긴다."""
    settings = get_settings()
    for path in download_models(settings):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
