import re
from pathlib import Path

from paperrag.review.models import ReviewDocument

DOCUMENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class DocumentNotFoundError(KeyError):
    pass


class FileReviewStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def document_dir(self, document_id: str) -> Path:
        if not DOCUMENT_ID_RE.fullmatch(document_id):
            raise DocumentNotFoundError(document_id)
        return self.root / document_id

    def create_dir(self, document_id: str) -> Path:
        path = self.document_dir(document_id)
        path.mkdir(parents=True, exist_ok=False)
        return path

    def save(self, document: ReviewDocument) -> None:
        directory = self.document_dir(document.document_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "review.json"
        temporary = directory / "review.json.tmp"
        temporary.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def get(self, document_id: str) -> ReviewDocument:
        path = self.document_dir(document_id) / "review.json"
        if not path.is_file():
            raise DocumentNotFoundError(document_id)
        return ReviewDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[ReviewDocument]:
        documents: list[ReviewDocument] = []
        for path in sorted(self.root.glob("*/review.json"), reverse=True):
            try:
                documents.append(
                    ReviewDocument.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                continue
        return sorted(documents, key=lambda item: item.created_at, reverse=True)

    def source_path(self, document_id: str) -> Path:
        path = self.document_dir(document_id) / "source.pdf"
        if not path.is_file():
            raise DocumentNotFoundError(document_id)
        return path

    def page_image_path(self, document_id: str, page: int) -> Path:
        document = self.get(document_id)
        page_row = next((item for item in document.pages if item.page == page), None)
        if page_row is None:
            raise DocumentNotFoundError(f"{document_id}/page/{page}")
        path = self.document_dir(document_id) / page_row.image_name
        if not path.is_file():
            raise DocumentNotFoundError(f"{document_id}/page/{page}")
        return path
