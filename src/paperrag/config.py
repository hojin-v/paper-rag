from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAPERRAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    runtime_mode: Literal["development", "production"] = "production"
    allow_degraded_results: bool = False
    allow_diagnostic_backends: bool = False
    database_url: str = "postgresql+psycopg://paperrag:paperrag@localhost:5432/paperrag"
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    llm_timeout_seconds: int = 300
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 192
    llm_forbid_cjk_ideographs: bool = True
    llm_cache_enabled: bool = True
    llm_cache_dir: Path = Path("./data/llm-cache")
    embed_base_url: str = "http://localhost:8100"
    embed_dim: int = 1024
    embed_timeout_seconds: int = 600
    embed_encoder: str = "st"
    embed_model_name: str = "BAAI/bge-m3"
    data_dir: Path = Path("./data")
    result_dir: Path = Path("./outputs")
    review_dir: Path = Path("./data/review")
    review_default_backend: str = "paddle"
    ingest_backend: str = "paddle"
    review_max_upload_mb: int = 100
    review_render_dpi: int = 120
    ocr_render_dpi: int = 200
    paddlex_cache_dir: Path = Path("./models/paddlex")
    paddlex_model_source: Literal["bos", "huggingface", "modelscope", "aistudio"] = (
        "bos"
    )
    paddle_device: str = "cpu"
    paddle_enable_mkldnn: bool = False
    paddle_isolate_process: bool = True
    paddle_worker_timeout_seconds: int = 1800
    automatic_ocr_min_coverage: float = 0.9
    automatic_ocr_require_author: bool = True
    paddle_layout_model_name: str = "PP-DocLayout-M"
    paddle_layout_threshold: float = 0.3
    paddle_layout_text_reconcile: bool = True
    paddle_text_coverage_threshold: float = 0.8
    paddle_text_merge_gap_ratio: float = 1.8
    paddle_abstract_merge_gap_ratio: float = 0.5
    paddle_abstract_merge_x_overlap: float = 0.7
    paddle_section_heading_split: bool = True
    paddle_section_heading_min_body_lines: int = 2
    paddle_section_heading_max_width_ratio: float = 0.72
    paddle_section_heading_line_overlap: float = 0.6
    paddle_inline_abstract_split: bool = True
    paddle_inline_heading_ocr_min_confidence: float = 0.7
    paddle_inline_heading_max_prefix_ratio: float = 0.4
    paddle_title_region_recovery: bool = True
    paddle_title_min_width_ratio: float = 0.55
    paddle_author_region_recovery: bool = True
    footnote_filter_enabled: bool = True
    footnote_bottom_ratio: float = 0.88
    footnote_max_height_ratio: float = 0.02
    footnote_max_width_ratio: float = 0.5
    footnote_max_chars: int = 240
    paddle_text_detection_model_name: str = "PP-OCRv5_mobile_det"
    paddle_text_recognition_model_name: str = "korean_PP-OCRv5_mobile_rec"
    paddle_layout_model_dir: Path | None = Path(
        "./models/paddlex/official_models/PP-DocLayout-M"
    )
    paddle_text_detection_model_dir: Path | None = Path(
        "./models/paddlex/official_models/PP-OCRv5_mobile_det"
    )
    paddle_ocr_model_dir: Path | None = Path(
        "./models/paddlex/official_models/korean_PP-OCRv5_mobile_rec"
    )
    paddle_use_table_recognition: bool = False
    paddle_table_classification_model_dir: Path | None = Path(
        "./models/paddlex/official_models/PP-LCNet_x1_0_table_cls"
    )
    paddle_wired_table_model_dir: Path | None = Path(
        "./models/paddlex/official_models/SLANeXt_wired"
    )
    paddle_wireless_table_model_dir: Path | None = Path(
        "./models/paddlex/official_models/SLANet_plus"
    )
    paddle_table_min_structure_quality: float = 0.7
    paper_collection_dir: Path = Path("./data/inbox/collected")
    paper_collection_manifest_name: str = "collection-manifest.jsonl"
    paper_smoke_dir: Path = Path("./data/inbox/smoke")
    paper_smoke_pages: int = 1
    paper_collection_query: str = "document layout analysis OCR"
    paper_collection_limit: int = 3
    paper_collection_candidate_multiplier: int = 5
    paper_collection_allowed_licenses: str = "cc-by,cc-by-sa,cc0"
    paper_download_max_mb: int = 50
    paper_collection_timeout_seconds: int = 60
    openalex_base_url: str = "https://api.openalex.org"
    openalex_api_key: str | None = None
    openalex_contact_email: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    search_suggestion_limit: int = 3
    # BGE-M3 실측에서 유사어 쌍(예지보전↔예측 유지보수)이 0.59로 측정되어 0.6에서 하향.
    # Phase 0 평가셋 구축 후 재보정한다.
    search_similarity_threshold: float = 0.5
    relation_top_k: int = 20
    keyword_alias_similarity_threshold: float = 0.95
    paragraph_min_chars: int = 100
    paragraph_max_chars: int = 1500
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    public_api_base_url: str = "http://localhost:8000"
    api_timeout_seconds: int = 1800
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
