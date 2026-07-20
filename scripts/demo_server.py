"""데모 검색 API 서버 — DB·LLM·임베딩 모델 없이 기동한다 (UI 확인·시연용).

이 스크립트는 실제 PostgreSQL, Ollama, BGE-M3 임베딩 서버를 전혀 띄우지 않고, 인메모리로 만든
소량의 시드 데이터(`build_repository`)와 결정적(가짜) LLM·임베딩 구현만으로 검색 API를 흉내낸다.
따라서 `docs/guide/10-production-readiness.md`가 말하는 "운영 폴백 차단" 대상이며, 이 서버가 응답한다고
해서 실제 OCR·검색·임베딩 파이프라인이 동작함을 검증한 것이 아니다 — 오직 검색 API의 요청/응답
스키마와 UI 화면 흐름을 빠르게 눈으로 확인하기 위한 용도이다.

실행:
    .venv/bin/python scripts/demo_server.py [--port 8000]

데모 질의:
    "스마트팩토리 이상탐지 논문"  → 정확 매칭 (대표: 논문10, 연관: 논문20)
    "예지보전 논문"               → 유사 키워드 3개 제안 → 선택 흐름
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import uvicorn

from paperrag.config import Settings
from paperrag.search import api as search_api
from paperrag.search.repository import InMemorySearchRepository
from paperrag.search.service import SearchService


class DemoLLM:
    """항상 실패해 서비스의 폴백 키워드 추출 경로를 태운다.

    실제 Ollama 없이도 "LLM 실패 시 규칙 기반 키워드 추출로 대체"하는 SearchService의 경로를
    시연하기 위해, 의도적으로 매 호출마다 예외를 던진다.
    """

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        raise RuntimeError("데모 모드: LLM 미사용")


class DemoEmbedding:
    """부분 문자열 매칭 기반 결정적 2차원 벡터 (시연용).

    실제 BGE-M3는 1024차원 실수 벡터를 생성하지만, 데모에서는 실행 환경에 임베딩 서버가 없어도
    검색 흐름을 재현할 수 있도록 미리 정한 키워드가 텍스트에 포함되면 항상 같은 2차원 벡터를
    돌려주는 규칙 기반 스텁으로 대체한다. 실제 의미 유사도 계산이 아니다.
    """

    RULES = [
        ("이상탐지", [1.0, 0.0]),
        ("예지보전", [0.92, 0.08]),
        ("예측 유지보수", [0.9, 0.1]),
        ("설비 진단", [0.8, 0.2]),
        ("rag", [0.0, 1.0]),
    ]

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            # RULES에 등록된 키워드 중 텍스트에 포함된 첫 번째 것을 사용하고, 없으면 두 클러스터
            # 어디에도 속하지 않는 낮은 값 벡터([0.05, 0.05])로 처리한다.
            match = next((vec for key, vec in self.RULES if key in lowered), [0.05, 0.05])
            vectors.append(match)
        return vectors


def build_repository() -> InMemorySearchRepository:
    """PostgreSQL 대신 사용할 인메모리 시드 데이터(키워드·논문·단락·표·연관관계)를 만든다.

    실제 DB 스키마(docs/guide/03-database.md의 papers/paragraphs/keywords/... 테이블)와 같은 형태의
    딕셔너리를 손으로 채워 넣어, 검색 서비스가 실제 저장소와 동일한 인터페이스로 동작하도록 한다.
    """
    return InMemorySearchRepository(
        keywords=[
            {"keyword_id": 1, "keyword": "이상탐지", "display_form": "이상탐지",
             "frequency": 12, "embedding": [1.0, 0.0]},
            {"keyword_id": 2, "keyword": "예측 유지보수", "display_form": "예측 유지보수",
             "frequency": 8, "embedding": [0.9, 0.1]},
            {"keyword_id": 3, "keyword": "설비 진단", "display_form": "설비 진단",
             "frequency": 5, "embedding": [0.8, 0.2]},
            {"keyword_id": 4, "keyword": "rag", "display_form": "RAG",
             "frequency": 3, "embedding": [0.0, 1.0]},
        ],
        papers=[
            {"paper_id": 10, "title": "스마트팩토리 딥러닝 기반 이상탐지 기법",
             "authors": "홍길동; 김철수", "published_year": 2024,
             "journal": "한국스마트제조학회지",
             "abstract": "딥러닝으로 제조 설비의 이상을 탐지한다.",
             "abstract_summary": "딥러닝 기반 설비 이상탐지 기법 제안.",
             "full_text_link": "https://example.test/paper10"},
            {"paper_id": 20, "title": "제조 설비 이상탐지 사례 연구",
             "authors": "박영희", "published_year": 2021, "journal": "산업공학논문지",
             "abstract": "현장 설비 이상탐지 적용 사례를 분석한다.",
             "abstract_summary": "이상탐지 현장 적용 사례 분석."},
            {"paper_id": 30, "title": "예측 유지보수 프레임워크 설계",
             "authors": "이민호", "published_year": 2023, "journal": "설비관리학회지",
             "abstract": "예측 유지보수 체계를 제안한다.",
             "abstract_summary": "예측 유지보수 프레임워크 제안."},
        ],
        paper_keywords=[
            {"paper_id": 10, "keyword_id": 1, "score": 0.9},
            {"paper_id": 20, "keyword_id": 1, "score": 0.7},
            {"paper_id": 30, "keyword_id": 2, "score": 0.85},
            {"paper_id": 10, "keyword_id": 2, "score": 0.5},
        ],
        paragraphs=[
            {"paper_id": 10, "paragraph_order": 1, "section_name": "서론",
             "original_text": "스마트팩토리에서 이상탐지는 핵심 과제이다.",
             "cleaned_text": "스마트팩토리에서 이상탐지는 핵심 과제이다.",
             "summary": "이상탐지의 중요성 소개.", "embedding": [1.0, 0.0],
             "keywords": ["이상탐지"]},
            {"paper_id": 10, "paragraph_order": 2, "section_name": "방법",
             "original_text": "오토인코더 기반 딥러닝 모델을 설계하였다.",
             "cleaned_text": "오토인코더 기반 딥러닝 모델을 설계하였다.",
             "summary": "오토인코더 모델 설계.", "embedding": [0.95, 0.05],
             "keywords": ["딥러닝"]},
            {"paper_id": 20, "paragraph_order": 1, "section_name": "사례",
             "original_text": "프레스 설비에 이상탐지를 적용하였다.",
             "cleaned_text": "프레스 설비에 이상탐지를 적용하였다.",
             "summary": "프레스 설비 적용 사례.", "embedding": [0.9, 0.1],
             "keywords": ["이상탐지"]},
            {"paper_id": 30, "paragraph_order": 1, "section_name": "설계",
             "original_text": "예측 유지보수 절차를 정의한다.",
             "cleaned_text": "예측 유지보수 절차를 정의한다.",
             "summary": "예측 유지보수 절차 정의.", "embedding": [0.9, 0.1],
             "keywords": ["예측 유지보수"]},
        ],
        tables=[
            {"paper_id": 10, "table_title": "표 1. 모델 성능 비교",
             "table_text": "모델 | F1\n오토인코더 | 0.91\nLSTM | 0.88",
             "table_summary": "오토인코더가 F1 0.91로 최고 성능."},
        ],
        relations=[
            {"source_paper_id": 10, "related_paper_id": 20, "relation_score": 0.81,
             "relation_reason": "겹치는 키워드: 이상탐지"},
            {"source_paper_id": 30, "related_paper_id": 10, "relation_score": 0.74,
             "relation_reason": "겹치는 키워드: 예측 유지보수"},
        ],
    )


def build_service() -> SearchService:
    """`.env`를 무시한 최소 데모 설정으로 SearchService를 조립한다(가짜 LLM·임베딩·인메모리 저장소).

    `_env_file=None`으로 실제 `.env`를 읽지 않게 해, 개발자 PC의 운영 설정과 무관하게 항상 같은
    데모 동작을 재현한다.
    """
    settings = Settings(
        _env_file=None,
        result_dir=Path("outputs"),
        search_suggestion_limit=3,
        search_similarity_threshold=0.6,
    )
    settings.result_dir.mkdir(parents=True, exist_ok=True)
    return SearchService(build_repository(), DemoLLM(), DemoEmbedding(), settings)


def main() -> None:
    """CLI 인자로 host/port를 받아, 실제 검색 API 앱에 데모 서비스만 주입해 실행한다.

    FastAPI의 `dependency_overrides`를 사용해 운영용 `get_service` 의존성만 데모 SearchService로
    바꿔치기하므로, 라우팅·요청/응답 스키마는 운영 API(`paperrag.search.api`)와 동일하다.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    service = build_service()
    search_api.app.dependency_overrides[search_api.get_service] = lambda: service
    uvicorn.run(search_api.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
