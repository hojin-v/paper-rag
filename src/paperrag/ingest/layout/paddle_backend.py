from paperrag.ingest.models import DocumentLayout


class PaddleBackend:
    def analyze(self, pdf_path: str) -> DocumentLayout:
        raise NotImplementedError(
            "PP-StructureV3 backend는 아직 파인튜닝 트랙 자리만 준비되어 있습니다. "
            "`pip install -e \".[ingest-full]\"`로 PaddleOCR 계열 의존성을 준비한 뒤 "
            "docs/adr/0002-parsing-stack.md와 docs/design/DESIGN.md §6의 "
            "파인튜닝 계획에 맞춰 어댑터를 구현하세요."
        )
