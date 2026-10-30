"""
Train the BFM Orientation Classifier
====================================
Trains the small classifier that tells how far a meter photo is rotated, so the reader can turn it upright before
the digits are read (src/service/vision/orientation.py).

Input: a folder of UPRIGHT meter photos (the counter reads left to right, digits standing up). A good source is
production photos whose AI reading matched the confirmed reading: the digit model only reads upright photos, so
those are upright. Photos are read with cv2.imread, which applies an EXIF rotation tag; the service does not (it
re-saves the download as PNG), so use photos without one. Production app photos carry none (checked on 2,204).

What it does:
  1. Splits the photos into train/val BY PHOTO (all four copies of a photo land on the same side, so val accuracy
     is measured on meters the model has not seen).
  2. Squashes each photo to 320x320 (the service resizes to the same square before predicting) and writes four
     copies, named by how far the copy is rotated clockwise:
       r000 (as is), r090 (90 clockwise), r180, r270 (90 anticlockwise)
     The service undoes the predicted rotation: r090 -> rotate 90 anticlockwise, and so on.
  3. Fine-tunes yolo11n-cls at imgsz 224 with flips and rotation augmentation OFF (fliplr=0, flipud=0, degrees=0):
     they would change the label.
  4. Prints the val top-1 accuracy and where best.pt is.

Layout written under --out:
  dataset/{train,val}/{r000,r090,r180,r270}/<photo>.jpg
  run/weights/best.pt

The model in src/models/orientation_yolo11n_cls.pt was made in two runs (Oct 2026). First, 710 upright production
photos (15 epochs, batch 32; val top-1 accuracy 1.0); on 169 rotated production photos, turning them upright with it
(min_confidence 0.9) took readings that agreed with the confirmed value from 0 to 90. Then that model was fine-tuned
(--base, 10 epochs, batch 64) on 4,159 production photos whose AI value had been confirmed unchanged, after dropping
the 40 the first model did not call upright (operators had confirmed some garbage readings of rotated photos). It
turns all 9 rotated photos of the 100 hand-checked Annexure photos upright and makes fewer needless turns.

To deploy a run:
  cp <out>/run/weights/best.pt src/models/orientation_yolo11n_cls.pt
  (config.yaml models.orientation points at it; "" disables the model)

Run (any working directory; everything is written under --out):
  python training/orientation/train_orientation.py --photos /path/to/upright --out /path/to/orient_train
"""

import argparse
import os
import random
from pathlib import Path

import cv2

SIZE = 320
CLASSES = {
    "r000": None,
    "r090": cv2.ROTATE_90_CLOCKWISE,
    "r180": cv2.ROTATE_180,
    "r270": cv2.ROTATE_90_COUNTERCLOCKWISE,
}
EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def build_dataset(photos: Path, dataset: Path, val_fraction: float, seed: int) -> tuple[int, int]:
    """Write the four rotated copies of every photo; returns (train photos, val photos)."""
    files = sorted(p for p in photos.iterdir() if p.suffix.lower() in EXTENSIONS)
    random.Random(seed).shuffle(files)
    n_val = max(1, round(len(files) * val_fraction))
    counts = {"train": 0, "val": 0}
    for i, path in enumerate(files):
        split = "val" if i < n_val else "train"
        image = cv2.imread(str(path))
        if image is None:
            print(f"skipped (unreadable): {path.name}")
            continue
        image = cv2.resize(image, (SIZE, SIZE))
        for name, rotation in CLASSES.items():
            folder = dataset / split / name
            folder.mkdir(parents=True, exist_ok=True)
            copy = image if rotation is None else cv2.rotate(image, rotation)
            cv2.imwrite(str(folder / f"{path.stem}.jpg"), copy)
        counts[split] += 1
    return counts["train"], counts["val"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--photos", required=True, type=Path, help="folder of upright meter photos")
    parser.add_argument("--out", required=True, type=Path, help="work folder (dataset and run are written here)")
    parser.add_argument("--val-fraction", type=float, default=0.2)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", default=None, help="e.g. cpu, mps, 0 (default: ultralytics picks)")
    parser.add_argument("--base", type=Path, default=None,
                        help="pretrained weights to start from (default: yolo11n-cls.pt, downloaded into --out)")
    args = parser.parse_args()
    photos, out = args.photos.resolve(), args.out.resolve()
    base = args.base.resolve() if args.base else out / "yolo11n-cls.pt"
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(out)  # ultralytics downloads helper weights and fonts into the working directory

    dataset = out / "dataset"
    if dataset.exists() and any(dataset.iterdir()):
        raise SystemExit(f"{dataset} is not empty; use a new --out so photos from another run do not mix in")
    n_train, n_val = build_dataset(photos, dataset, args.val_fraction, args.seed)
    print(f"photos: {n_train} train, {n_val} val (x4 rotations)")

    from ultralytics import YOLO
    model = YOLO(str(base))
    model.train(
        data=str(dataset), epochs=args.epochs, imgsz=224, batch=args.batch, seed=args.seed, deterministic=True,
        fliplr=0.0, flipud=0.0, degrees=0, plots=False, device=args.device, project=str(out), name="run",
        exist_ok=True,
    )
    best = out / "run" / "weights" / "best.pt"
    metrics = YOLO(str(best)).val(data=str(dataset), split="val", imgsz=224, plots=False, device=args.device,
                                  project=str(out), name="val", exist_ok=True)
    print(f"val top-1 accuracy: {metrics.top1:.4f}")
    print(f"weights: {best}")


if __name__ == "__main__":
    main()
