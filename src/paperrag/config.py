"""
전체 시스템(수집·OCR·LLM 정제·임베딩·검색·워커)의 유일한 설정 진입점.

CLAUDE.md 코드 규칙에 따라 "설정은 환경변수(.env) → Settings로만 접근 (하드코딩 금지)"이며,
이 파일이 그 단일 창구다. 모든 필드는 `PAPERRAG_` 접두사가 붙은 환경변수(.env 포함)로 덮어쓸 수
있고, 각 필드의 기본값은 곧 로컬 개발/운영 정책의 기본값이다. `docs/design/DESIGN.md`의 STEP 1~8
파이프라인과 `docs/guide/10-production-readiness.md`의 운영 정책 표가 이 필드들과 1:1로 대응한다.
값을 바꾸면 DB 스키마(VECTOR 차원)·저장된 임베딩·운영 게이트(readiness.py)와 어긋날 수 있으므로
설계서·가이드 문서를 먼저 확인한다.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """pydantic-settings 기반 전역 설정.

    `env_prefix="PAPERRAG_"`이므로 예: `PAPERRAG_DATABASE_URL` 환경변수가 `database_url`
    필드를 채운다. `.env` 파일을 읽되(`env_file=".env"`) 실제 환경변수가 우선한다.
    `extra="ignore"`이므로 여기 정의되지 않은 `PAPERRAG_*` 변수는 조용히 무시된다(오타를 잡지
    못하므로 새 필드를 추가할 때는 반드시 여기에도 선언해야 한다).
    """

    model_config = SettingsConfigDict(
        env_prefix="PAPERRAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "development" | "production". readiness.py가 production일 때만 폴백 차단 정책을 엄격히
    # 검사한다(예: hash 임베딩·진단 backend 금지). docs/guide/10-production-readiness.md 참조.
    runtime_mode: Literal["development", "production"] = "production"
    # true면 LLM 요약/키워드 실패 시 규칙 기반 대체 결과를 정상 응답인 것처럼 반환할 수 있다.
    # 운영에서는 반드시 false — 실패를 감춰 품질 착시를 만들지 않기 위함(readiness.py에서 검사).
    allow_degraded_results: bool = False
    # true면 OCR을 수행하지 않는 진단용 backend(simple/docling)를 운영 업로드 경로에서도 허용한다.
    # Docling/Simple은 비교 진단 전용이며 운영 반영은 금지(DESIGN.md §2) — 기본값 false 유지.
    allow_diagnostic_backends: bool = False
    database_url: str = "postgresql+psycopg://paperrag:paperrag@localhost:5432/paperrag"
    ollama_base_url: str = "http://localhost:11434"
    # LLM 파인튜닝 전 기본 모델(DESIGN.md §2): Apache 2.0 라이선스, 한국어 품질 양호, CPU 구동 가능.
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    llm_timeout_seconds: int = 300
    # 0.0 = 결정적 출력. 단락 정제·요약·키워드 JSON 생성 재현성을 위해 창의성보다 일관성을 우선.
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 192
    # true면 LLM 응답에서 한자(CJK 표의문자)를 거부한다. 한국어/영어 논문 처리 중 모델이 의도치 않게
    # 한자를 섞어 내는 것을 막기 위한 출력 검증 스위치.
    llm_forbid_cjk_ideographs: bool = True
    # 동일 입력(단락 텍스트 등)에 대한 LLM 호출 결과를 로컬에 캐시해 재처리·재시도 시 CPU LLM 재호출
    # 비용을 줄인다.
    llm_cache_enabled: bool = True
    llm_cache_dir: Path = Path("./data/llm-cache")
    embed_base_url: str = "http://localhost:8100"
    # BGE-M3 임베딩 차원. DB 스키마의 모든 VECTOR 컬럼이 1024로 고정되어 있으므로(ADR-0001) 이 값을
    # 임의로 바꾸면 기존 벡터와 호환되지 않아 적재가 실패한다.
    embed_dim: int = 1024
    embed_timeout_seconds: int = 600
    # "hash"(모델 없이 결정적 테스트 벡터, 의미 검색 불가) | "st"(sentence-transformers 실제 임베딩).
    # 운영은 반드시 "st" — readiness.py가 "hash"면 error로 판정한다.
    embed_encoder: str = "st"
    embed_model_name: str = "BAAI/bge-m3"
    data_dir: Path = Path("./data")
    result_dir: Path = Path("./outputs")
    review_dir: Path = Path("./data/review")
    # 검수(review) 업로드 경로의 기본 OCR backend. 운영 정책상 "paddle"만 허용(전체 PDF OCR 단일 경로).
    review_default_backend: str = "paddle"
    # 배치 수집(ingest) 경로의 기본 OCR backend. review_default_backend와 함께 readiness.py의
    # "full_ocr_policy" 검사 대상이다.
    ingest_backend: str = "paddle"
    review_max_upload_mb: int = 100
    review_render_dpi: int = 120
    # 수집 파이프라인 STEP 1(source check)에서 페이지를 이미지화할 때 쓰는 해상도. 200 DPI는
    # OCR 인식률과 처리 시간의 절충값으로 선택됨(docs/guide/10-production-readiness.md 실측 참고).
    ocr_render_dpi: int = 200
    paddlex_cache_dir: Path = Path("./models/paddlex")
    # PaddleX 사전학습 모델 다운로드 출처. 국내망에서는 기본값(bos)이 가장 안정적으로 접근 가능.
    paddlex_model_source: Literal["bos", "huggingface", "modelscope", "aistudio"] = (
        "bos"
    )
    paddle_device: str = "cpu"
    # MKLDNN 가속을 끈 상태가 기본값. 개발 머신에서 sudo 권한 없이 vendored libgomp1을 쓰는 우회
    # 환경(scripts/with_paddle_runtime.sh)과의 호환성 문제로 비활성화되어 있다.
    paddle_enable_mkldnn: bool = False
    # true면 Paddle 레이아웃·OCR을 별도 프로세스로 격리 실행해, 처리 종료 후 모델이 점유한 CPU/메모리를
    # 확실히 회수한다(API 프로세스 자체의 RSS를 낮게 유지하기 위함).
    paddle_isolate_process: bool = True
    paddle_worker_timeout_seconds: int = 1800
    # STEP 2(layout) 이후 자동 품질 게이트 기준: OCR이 텍스트 영역을 얼마나 커버해야 "자동 통과"로
    # 볼지의 하한. 미달 시 사람 검수 큐로 넘어간다(docs/guide/10-production-readiness.md).
    automatic_ocr_min_coverage: float = 0.9
    # true면 저자(author) 영역이 인식되지 않은 문서를 자동 품질 게이트에서 통과시키지 않는다.
    automatic_ocr_require_author: bool = True
    paddle_layout_model_name: str = "PP-DocLayout-M"
    # 레이아웃 분류 confidence 하한. 이보다 낮은 신뢰도의 영역은 채택하지 않는다(오탐 억제).
    paddle_layout_threshold: float = 0.3
    # true면 레이아웃 모델 결과와 텍스트 검출(OCR det) 결과를 대조해 레이아웃 누락 영역을 보정한다
    # (docs/guide/10-production-readiness.md의 "텍스트 검출 기반 레이아웃 자동 보정" 참고).
    paddle_layout_text_reconcile: bool = True
    # 텍스트 검출 보정을 적용할지 판단하는 커버리지 임계값(레이아웃이 텍스트 선을 얼마나 놓쳤는지).
    paddle_text_coverage_threshold: float = 0.8
    # 인접한 텍스트 줄을 같은 블록으로 병합할지 판단하는 줄 간격 대비 비율. 값이 클수록 넓게 병합한다.
    paddle_text_merge_gap_ratio: float = 1.8
    # 초록(abstract) 영역이 여러 줄/블록으로 쪼개져 인식됐을 때 병합 기준이 되는 간격 비율(본문보다
    # 좁게 잡아 다른 섹션과 잘못 합쳐지는 것을 방지).
    paddle_abstract_merge_gap_ratio: float = 0.5
    # 초록 블록 병합 시 요구하는 최소 가로 겹침 비율(같은 컬럼에 속하는지 판단).
    paddle_abstract_merge_x_overlap: float = 0.7
    # true면 본문 블록 안에 섞여 있는 섹션 제목(heading)을 별도 블록으로 분리한다(단락 귀속 STEP 4의
    # 정확도를 높이기 위함).
    paddle_section_heading_split: bool = True
    # 섹션 제목 분리를 시도하려면 그 아래 본문이 최소 이 줄 수 이상이어야 한다(짧은 한 줄짜리 블록을
    # 잘못 "제목+본문"으로 쪼개는 오탐 방지).
    paddle_section_heading_min_body_lines: int = 2
    # 섹션 제목 후보 줄의 가로 폭이 블록 전체 폭 대비 이 비율보다 작아야 "제목"으로 인정한다(제목은
    # 보통 본문보다 짧다는 휴리스틱).
    paddle_section_heading_max_width_ratio: float = 0.72
    paddle_section_heading_line_overlap: float = 0.6
    # true면 초록 블록 안에 다음 섹션 제목이 이어 붙어 나온 경우(인라인) 이를 분리한다.
    paddle_inline_abstract_split: bool = True
    # 인라인 섹션 제목으로 인정하기 위한 OCR 신뢰도 하한(낮은 신뢰도의 오인식을 제목 분리에 쓰지 않음).
    paddle_inline_heading_ocr_min_confidence: float = 0.7
    # 인라인 제목 뒤에 붙은 본문 접두 텍스트가 전체 줄 길이 대비 이 비율을 넘으면 "제목+본문 혼재"로
    # 보지 않는다(제목만 있는 경우와 구분).
    paddle_inline_heading_max_prefix_ratio: float = 0.4
    # true면 레이아웃 모델이 제목(title) 영역을 놓쳤을 때 텍스트 검출 결과로 복구를 시도한다.
    paddle_title_region_recovery: bool = True
    # 제목 영역 복구 시 후보로 인정할 최소 가로 폭 비율(너무 좁은 텍스트 선은 제목이 아닐 가능성).
    paddle_title_min_width_ratio: float = 0.55
    # true면 저자(author) 영역도 같은 방식으로 텍스트 검출 기반 복구를 시도한다.
    paddle_author_region_recovery: bool = True
    # 각주(footnote)를 본문에서 걸러낼지 여부. 각주는 STEP 3(filter)에서 참고문헌처럼 제외 대상이다.
    footnote_filter_enabled: bool = True
    # 페이지 세로 위치가 이 비율(페이지 하단 88%) 아래에 있어야 각주 후보로 본다.
    footnote_bottom_ratio: float = 0.88
    # 각주 후보 블록의 세로 높이가 페이지 높이 대비 이 비율을 넘으면 각주가 아닌 본문으로 간주한다.
    footnote_max_height_ratio: float = 0.02
    # 각주 후보 블록의 가로 폭이 페이지 폭 대비 이 비율을 넘으면 각주가 아닌 것으로 본다.
    footnote_max_width_ratio: float = 0.5
    # 각주로 인정할 최대 글자 수(길면 각주가 아니라 잘못 분류된 본문일 가능성이 큼).
    footnote_max_chars: int = 240
    paddle_text_detection_model_name: str = "PP-OCRv5_mobile_det"
    # 한국어 인식 모델을 기본값으로 사용(영문도 함께 인식 가능). docs/guide/10 다운로드 표 참고.
    paddle_text_recognition_model_name: str = "korean_PP-OCRv5_mobile_rec"
    # 아래 세 모델 디렉터리는 scripts/download_paddle_models.py가 받아 두는 로컬 캐시 경로이며,
    # readiness.py가 실제 파일 존재 여부를 점검한다(경로가 있어도 비어 있으면 error).
    paddle_layout_model_dir: Path | None = Path(
        "./models/paddlex/official_models/PP-DocLayout-M"
    )
    paddle_text_detection_model_dir: Path | None = Path(
        "./models/paddlex/official_models/PP-OCRv5_mobile_det"
    )
    paddle_ocr_model_dir: Path | None = Path(
        "./models/paddlex/official_models/korean_PP-OCRv5_mobile_rec"
    )
    # 표 구조 인식(SLANet 계열)은 비용이 크고 아직 검증 초기 단계라 기본값은 비활성화.
    paddle_use_table_recognition: bool = False
    # 유선(wired)/무선(wireless) 표 구조 모델 중 어느 쪽을 쓸지 먼저 분류하는 모델.
    paddle_table_classification_model_dir: Path | None = Path(
        "./models/paddlex/official_models/PP-LCNet_x1_0_table_cls"
    )
    # 선이 있는(wired) 표 구조 복원 모델.
    paddle_wired_table_model_dir: Path | None = Path(
        "./models/paddlex/official_models/SLANeXt_wired"
    )
    # 선이 없는(wireless) 표 구조 복원 모델.
    paddle_wireless_table_model_dir: Path | None = Path(
        "./models/paddlex/official_models/SLANet_plus"
    )
    # 표 구조 복원 품질 점수 하한. 미달 시 표 구조 인식 결과를 신뢰하지 않고 대체 경로(텍스트 보존
    # 등)로 처리한다.
    paddle_table_min_structure_quality: float = 0.7
    paper_collection_dir: Path = Path("./data/inbox/collected")
    paper_collection_manifest_name: str = "collection-manifest.jsonl"
    paper_smoke_dir: Path = Path("./data/inbox/smoke")
    # smoke(빠른 기능 점검)용으로 원본 PDF에서 앞부분 몇 페이지만 잘라낼지 지정. CPU에서 11~19페이지
    # 전체 OCR은 오래 걸리므로 기능 경로만 빠르게 확인하기 위한 값(docs/guide/11 참고).
    paper_smoke_pages: int = 1
    # OpenAlex 기본 검색어. CLI에서 --query를 생략했을 때 사용되는 기본 수집 주제.
    paper_collection_query: str = "document layout analysis OCR"
    paper_collection_limit: int = 3
    # OpenAlex 검색 시 최종 채택 개수(limit)보다 몇 배 많은 후보를 먼저 조회할지. 라이선스·PDF URL
    # 필터로 걸러지는 후보가 있으므로 여유분을 두기 위함(openalex.py의 candidate_limit 계산에 사용).
    paper_collection_candidate_multiplier: int = 5
    # 자동 수집을 허용하는 라이선스 화이트리스트(콤마 구분). CC BY/CC BY-SA/CC0만 허용 — NC·ND·라이선스
    # 미상 논문은 재가공·재배포 권리가 불명확해 코드에서 거절한다(docs/guide/11-paper-collection.md).
    paper_collection_allowed_licenses: str = "cc-by,cc-by-sa,cc0"
    paper_download_max_mb: int = 50
    paper_collection_timeout_seconds: int = 60
    openalex_base_url: str = "https://api.openalex.org"
    # 무료 API 키(선택). 설정하지 않아도 익명 요청이 동작하지만 공식 운영 조건으로 보장되지 않는다.
    openalex_api_key: str | None = None
    # OpenAlex "polite pool" 식별을 위한 담당자 이메일. User-Agent에 포함되어 응답 속도·안정성에 유리.
    openalex_contact_email: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    # 브로커(0번 DB)와 결과 백엔드(1번 DB)를 같은 Redis 인스턴스의 다른 논리 DB로 분리해 둔다.
    celery_result_backend: str = "redis://localhost:6379/1"
    # 정확 매칭 실패 시 사용자에게 보여줄 유사 키워드 후보 개수(DESIGN.md §5.2 STEP 3).
    search_suggestion_limit: int = 3
    # BGE-M3 실측에서 유사어 쌍(예지보전↔예측 유지보수)이 0.59로 측정되어 0.6에서 하향.
    # Phase 0 평가셋 구축 후 재보정한다.
    search_similarity_threshold: float = 0.5
    # 논문 간 연관도(paper_relations)를 사전 계산할 때 논문당 상위 몇 편까지 저장할지(DESIGN.md §STEP 8).
    relation_top_k: int = 20
    # 키워드 임베딩 코사인 유사도가 이 값 이상이면 서로 다른 표기를 동의어(keyword_aliases)로 병합한다.
    keyword_alias_similarity_threshold: float = 0.95
    # 단락 분리(STEP 4) 기준: 이보다 짧은 단락은 인접 단락과 병합한다.
    paragraph_min_chars: int = 100
    # 단락 분리(STEP 4) 기준: 이보다 긴 단락은 문장 경계에서 분할한다.
    paragraph_max_chars: int = 1500
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    # 외부(사용자)에 노출되는 API 기준 URL. 사내망 리버스 프록시 등으로 api_base_url과 달라질 수 있어
    # 별도 필드로 분리되어 있다(예: 엑셀 다운로드 링크 생성 시 사용).
    public_api_base_url: str = "http://localhost:8000"
    api_timeout_seconds: int = 1800
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """프로세스 전역에서 재사용되는 `Settings` 싱글턴을 반환한다.

    `lru_cache`로 최초 호출 시 한 번만 `.env`/환경변수를 읽고 이후 호출은 같은 인스턴스를 재사용한다.
    설정을 다시 읽어야 하는 테스트 등에서는 `get_settings.cache_clear()`로 캐시를 비운다.
    """
    return Settings()
