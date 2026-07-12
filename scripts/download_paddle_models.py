from __future__ import annotations

import os
from pathlib import Path

from paperrag.config import Settings, get_settings


def download_models(settings: Settings) -> list[Path]:
    """설정된 Paddle 공식 추론 모델을 로컬 캐시에 준비한다."""
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(settings.paddlex_cache_dir)
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = settings.paddlex_model_source

    from paddlex.inference.utils.official_models import (  # type: ignore[import-not-found]
        official_models,
    )

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
        resolved = Path(official_models.get_model_path(model_name))
        _validate_model_directory(resolved, model_name)
        if configured_path is not None and resolved.resolve() != configured_path.resolve():
            raise RuntimeError(
                f"{model_name} 다운로드 경로는 {resolved}이지만 설정 경로는 "
                f"{configured_path}입니다. PAPERRAG_PADDLE_*_MODEL_DIR을 맞추세요."
            )
        paths.append(resolved)
    return paths


def _validate_model_directory(path: Path, model_name: str) -> None:
    required = ("inference.json", "inference.yml", "inference.pdiparams")
    missing = [filename for filename in required if not (path / filename).is_file()]
    if missing:
        raise RuntimeError(f"{model_name} 모델 파일이 불완전합니다: {', '.join(missing)}")


def main() -> int:
    settings = get_settings()
    for path in download_models(settings):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
