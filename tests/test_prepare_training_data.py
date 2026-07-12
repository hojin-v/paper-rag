import json
from pathlib import Path
import zipfile

from scripts.prepare_training_data import prepare


def test_prepare_converts_export_to_coco_and_ocr_lists(tmp_path: Path) -> None:
    archive_path = tmp_path / "training.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("layout/images/doc-p0001.png", b"png")
        archive.writestr("ocr/images/doc-b1.png", b"png")
        archive.writestr(
            "layout/annotations.jsonl",
            json.dumps(
                {
                    "image": "layout/images/doc-p0001.png",
                    "width": 100,
                    "height": 200,
                    "document_id": "doc-1",
                    "page": 1,
                    "blocks": [{"label": "text", "bbox": [10, 20, 80, 60]}],
                }
            )
            + "\n",
        )
        archive.writestr(
            "ocr/labels.jsonl",
            json.dumps(
                {
                    "image": "ocr/images/doc-b1.png",
                    "text": "OCR text",
                    "document_id": "doc-1",
                    "block_id": "b1",
                }
            )
            + "\n",
        )

    stats = prepare(archive_path, tmp_path / "prepared", validation_ratio=0.0)

    assert stats["layout_train_pages"] == 1
    coco = json.loads((tmp_path / "prepared/layout/train.json").read_text())
    assert coco["annotations"][0]["bbox"] == [10.0, 20.0, 70.0, 40.0]
    assert "images/doc-b1.png\tOCR text" in (
        tmp_path / "prepared/ocr/train.txt"
    ).read_text()
