"""Colab 파인튜닝용 학습 데이터를 준비하는 1회성 스크립트.

`GET /training/export`(docs/guide/09-upload-review-colab-training.md 2~3단계)가 만들어주는
`paperrag-training-data.zip`(레이아웃 이미지·annotations.jsonl, OCR crop·labels.jsonl)을 입력으로 받아,
Colab 노트북이 바로 학습에 사용할 수 있는 형태로 변환한다.

- 레이아웃: 12개 클래스(LAYOUT_CATEGORIES)의 bounding box를 COCO 형식(`layout/train.json`,
  `layout/val.json`)으로 재구성한다.
- OCR: crop 이미지 경로와 정답 텍스트를 탭으로 구분한 PaddleOCR 인식 학습 포맷
  (`ocr/train.txt`, `ocr/val.txt`)으로 재구성한다.

문서(document_id) 단위로 해시 기반 결정적 분할을 적용해 학습/검증 세트를 나누므로, 페이지나 crop이
아니라 "같은 논문의 데이터가 학습과 검증에 동시에 섞이지 않도록" 보장한다.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
import zipfile

# PP-StructureV3 레이아웃 검출 학습에 사용하는 12개 클래스. 순서가 곧 COCO category_id(1부터)로
# 이어지므로 순서를 바꾸면 기존에 만든 학습 데이터의 라벨 의미가 달라진다.
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
    """검수 완료 학습 ZIP(archive_path)을 COCO 레이아웃/PaddleOCR 인식 학습 포맷으로 변환한다.

    출력 디렉터리(output_dir)는 매 실행마다 완전히 새로 만든다(기존 디렉터리가 있으면 삭제 후
    재생성) — 이전 실행의 잔여 파일이 섞여 잘못된 학습 세트가 만들어지는 것을 막기 위함이다.
    반환값은 레이아웃/OCR 각각의 train/val 개수 통계이며, 같은 값을 `prepared-manifest.json`에도
    저장해 Colab 쪽에서 데이터 검사에 활용한다.
    """
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

    # 페이지가 아니라 문서(document_id) 단위로 train/val을 나눠, 같은 논문의 페이지가 학습과
    # 검증 세트에 동시에 들어가 검증 점수가 부풀려지는 것(data leakage)을 방지한다.
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
                    # 12개 학습 대상 클래스에 없는 라벨(예: 향후 추가된 실험적 유형)은 건너뛴다.
                    continue
                x1, y1, x2, y2 = (float(value) for value in block["bbox"])
                width, height = x2 - x1, y2 - y1
                if width <= 0 or height <= 0:
                    # 폭·높이가 0 이하인 퇴화된 박스는 COCO 학습 세트에 포함시키지 않는다.
                    continue
                coco["annotations"].append(
                    {
                        "id": annotation_id,
                        "image_id": image_id,
                        "category_id": category_ids[label],
                        "bbox": [x1, y1, width, height],  # COCO bbox는 [x, y, width, height] 형식.
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
        # PaddleOCR 인식 학습 포맷은 "이미지경로\t정답텍스트" 한 줄 형식이므로, 탭·개행이 정답
        # 텍스트에 섞여 있으면 형식이 깨진다 — 공백으로 치환해 안전하게 만든다.
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
    """ZIP 압축을 풀되, 압축 해제 결과가 output_dir 바깥으로 나가는 항목(Zip Slip)이 있으면 거부한다."""
    root = output_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"Unsafe archive member: {member.filename}")
        archive.extractall(output_dir)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """JSON Lines 파일을 한 줄씩 파싱해 dict 목록으로 읽는다. 빈 줄은 건너뛴다."""
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
    """document_id를 SHA-256 해시해 [0,1) 구간의 결정적 난수로 바꾼 뒤 train/val을 정한다.

    같은 document_id는 항상 같은 split으로 분류되므로(난수 시드 없이도 재현 가능), 여러 번
    실행해도 같은 문서가 학습과 검증에 번갈아 들어가지 않는다.
    """
    digest = hashlib.sha256(document_id.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return "val" if fraction < validation_ratio else "train"


def main() -> int:
    """CLI 인자(원본 ZIP 경로, 출력 디렉터리, 검증 비율)를 받아 `prepare()`를 실행하고 통계를 출력한다."""
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
