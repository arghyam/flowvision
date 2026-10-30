"""Read a folder of meter photos through the real service code, with the Phase 1 fixes off and on, side by side.

Run from services/bfm-ocr (config and model paths are relative to it):
    python scripts/try_photos.py /path/to/photos              # prints a table
    python scripts/try_photos.py /path/to/photos --csv out.csv

"before" is the service with both fixes switched off (FLOWVISION_COUNTER_ROW_ONLY=false, FLOWVISION_ORIENTATION=off),
which is how it behaved before Phase 1; "after" is Phase 1. Each photo goes through ImageService.extract_reading,
the same path as POST /extract-reading, with the download replaced by reading the local file. Nothing is stored.
"""
import argparse
import csv
import logging
import os
import sys
from io import BytesIO
from types import SimpleNamespace

SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(SERVICE)
sys.path.insert(0, os.path.join(SERVICE, "src"))

from fastapi import BackgroundTasks  # noqa: E402
from PIL import Image  # noqa: E402

from conf.config import Config  # noqa: E402
from models.models import ReadingExtractionRequest  # noqa: E402
from service.api import image_service  # noqa: E402

MODES = {"before": ("false", "off"), "after": ("true", "on")}


def local_photo(path):
    """download_image, from a local file (the same PNG re-encode)."""
    buf = BytesIO()
    Image.open(path).save(buf, format="PNG")
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description="Phase 1 before/after on a folder of photos")
    ap.add_argument("photos", help="folder of .jpg/.jpeg/.png photos")
    ap.add_argument("--csv", help="also write the results to this CSV file")
    args = ap.parse_args()

    # no database writes: the service only reads the photos
    image_service.MetadataStore = lambda config: SimpleNamespace(store_request=lambda *a: None,
                                                                 store_response=lambda *a: None)
    angle = {}
    turn = image_service.turn_upright

    def recording_turn(image_bytes, model=None):
        out = turn(image_bytes, model)
        angle["value"] = out[1]
        return out

    image_service.turn_upright = recording_turn

    # keep the table readable: no fastai progress bars or per-image YOLO lines
    logging.getLogger("ultralytics").setLevel(logging.WARNING)
    for name in ("classify_bfm_image", "classify_color_image"):
        def quiet(*a, _original=getattr(image_service, name), **k):
            with k["model"].no_bar():
                return _original(*a, **k)
        setattr(image_service, name, quiet)

    service = image_service.ImageService(Config())
    service.download_image = local_photo

    names = sorted(f for f in os.listdir(args.photos) if f.lower().endswith((".jpg", ".jpeg", ".png")))
    rows = []
    print(f"{'photo':28s} {'before':>18s} {'after':>18s}  turned  colour")
    for name in names:
        row = {"photo": name}
        for mode, (row_only, orientation) in MODES.items():
            os.environ["FLOWVISION_COUNTER_ROW_ONLY"], os.environ["FLOWVISION_ORIENTATION"] = row_only, orientation
            angle.clear()
            response = service.extract_reading(
                ReadingExtractionRequest(imageURL=os.path.join(args.photos, name)), BackgroundTasks())
            result = response.result if hasattr(response, "result") else None
            data = result.data if result else None
            reading = data.meterReading if result and result.status == "SUCCESS" else (
                result.status if result else "ERROR")
            row[f"{mode}_status"] = result.status if result else "ERROR"
            row[f"{mode}_reading"] = reading
            row[f"{mode}_last_digit_colour"] = data.lastDigitColor if data else ""
            if mode == "after":
                row["turned_by"] = angle.get("value", 0)
        rows.append(row)
        print(f"{name[:28]:28s} {str(row['before_reading']):>18s} {str(row['after_reading']):>18s}  "
              f"{row['turned_by'] or '-':>6}  {row['after_last_digit_colour']}")
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print("written", args.csv)


if __name__ == "__main__":
    main()
