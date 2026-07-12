import argparse
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
import zipfile

LAYOUT_CATEGORIES = [
    "title",
    "author",
    "abstract",
    "section_header",
    "text",
    "table",
    "table_caption",
    "figure",
    "figure_caption",
    "formula",
    "reference",
    "header_footer",
]


def prepare(archive_path: Path, output_dir: Path, validation_ratio: float = 0.2) -> dict[str, int]:
    if not 0.0 <= validation_ratio < 1.0:
        raise ValueError("validation_ratio must be in [0, 1).")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    _safe_extract(archive_path, output_dir)

    annotations_path = output_dir / "layout" / "annotations.jsonl"
    ocr_labels_path = output_dir / "ocr" / "labels.jsonl"
    layout_rows = _read_jsonl(annotations_path)
    ocr_rows = _read_jsonl(ocr_labels_path)
    category_ids = {name: index for index, name in enumerate(LAYOUT_CATEGORIES, start=1)}

    splits = {"train": [], "val": []}
    for row in layout_rows:
        splits[_split_name(str(row["document_id"]), validation_ratio)].append(row)

    for split, rows in splits.items():
        coco = {
            "images": [],
            "annotations": [],
            "categories": [
                {"id": category_id, "name": name}
                for name, category_id in category_ids.items()
            ],
        }
        annotation_id = 1
        for image_id, row in enumerate(rows, start=1):
            coco["images"].append(
                {
                    "id": image_id,
                    "file_name": str(row["image"]).removeprefix("layout/images/"),
                    "width": float(row["width"]),
                    "height": float(row["height"]),
                }
            )
            for block in row.get("blocks", []):
                label = str(block["label"])
                if label not in category_ids:
                    continue
                x1, y1, x2, y2 = (float(value) for value in block["bbox"])
                width, height = x2 - x1, y2 - y1
                if width <= 0 or height <= 0:
                    continue
                coco["annotations"].append(
                    {
                        "id": annotation_id,
                        "image_id": image_id,
                        "category_id": category_ids[label],
                        "bbox": [x1, y1, width, height],
                        "area": width * height,
                        "iscrowd": 0,
                    }
                )
                annotation_id += 1
        target = output_dir / "layout" / f"{split}.json"
        target.write_text(json.dumps(coco, ensure_ascii=False, indent=2), encoding="utf-8")

    ocr_splits: dict[str, list[str]] = {"train": [], "val": []}
    for row in ocr_rows:
        document_id = str(row["document_id"])
        image = str(row["image"]).removeprefix("ocr/")
        text = str(row["text"]).replace("\t", " ").replace("\n", " ").strip()
        if text:
            ocr_splits[_split_name(document_id, validation_ratio)].append(f"{image}\t{text}")
    for split, rows in ocr_splits.items():
        (output_dir / "ocr" / f"{split}.txt").write_text(
            "\n".join(rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )

    stats = {
        "layout_train_pages": len(splits["train"]),
        "layout_val_pages": len(splits["val"]),
        "ocr_train_crops": len(ocr_splits["train"]),
        "ocr_val_crops": len(ocr_splits["val"]),
    }
    (output_dir / "prepared-manifest.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return stats


def _safe_extract(archive_path: Path, output_dir: Path) -> None:
    root = output_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"Unsafe archive member: {member.filename}")
        archive.extractall(output_dir)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: object required")
        rows.append(value)
    return rows


def _split_name(document_id: str, validation_ratio: float) -> str:
    digest = hashlib.sha256(document_id.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return "val" if fraction < validation_ratio else "train"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    args = parser.parse_args()
    stats = prepare(args.archive, args.output, args.validation_ratio)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
