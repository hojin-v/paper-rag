"""STEP 2(layout) 백엔드 레지스트리.

`get_backend(name)` 하나로 문자열 이름을 실제 백엔드 구현체로 바꿔주는 팩토리 모듈이다.
현재 운영 경로(DESIGN.md §2, 2026-07-12 사용자 결정)는 PaddleOCR PP-StructureV3
(`PaddleBackend`) 전체 OCR 단일 경로이며, `DoclingBackend`/`SimpleTextLayerBackend`는
디지털 파싱 트랙 폐기 이후 운영 적재에는 사용하지 않고 진단·비교·단위 테스트 용도로만
남겨둔 것이다 (ADR-0002 참고).
"""

from paperrag.ingest.layout.base import LayoutBackend
from paperrag.ingest.layout.docling_backend import DoclingBackend
from paperrag.ingest.layout.paddle_backend import PaddleBackend
from paperrag.ingest.layout.simple_backend import SimpleTextLayerBackend


def get_backend(name: str) -> LayoutBackend:
    """이름 문자열로 layout 백엔드 인스턴스를 생성해 반환한다.

    대소문자·앞뒤 공백을 정규화한 뒤 매칭하므로 CLI/설정 파일 어디서 넘어온 값이든
    "simple" / "SIMPLE " 처럼 표기가 달라도 동일하게 처리된다.
    "paddle"/"pp-structure"/"pp-structurev3"는 전부 같은 운영 백엔드(PaddleBackend)를
    가리키는 별칭이다 — 설정값이나 문서에서 명칭이 혼용되는 것을 흡수하기 위함.

    Raises:
        ValueError: 등록되지 않은 이름이 들어온 경우.
    """
    normalized = name.strip().lower()
    if normalized == "simple":
        # 진단용: pdfplumber 텍스트 레이어 휴리스틱 (운영 미사용, docs/reports/benchmarks 참고)
        return SimpleTextLayerBackend()
    if normalized == "docling":
        # 진단용: 과거 디지털 PDF 트랙 후보, 현재는 비교/테스트 전용 (ADR-0002)
        return DoclingBackend()
    if normalized in {"paddle", "pp-structure", "pp-structurev3"}:
        # 운영 경로: 전체 PDF OCR 단일 경로 (DESIGN.md §2)
        return PaddleBackend()
    raise ValueError(f"알 수 없는 layout backend입니다: {name}")


__all__ = [
    "DoclingBackend",
    "LayoutBackend",
    "PaddleBackend",
    "SimpleTextLayerBackend",
    "get_backend",
]
