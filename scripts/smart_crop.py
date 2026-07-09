# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["opencv-python-headless==4.10.0.84", "numpy<2"]
# ///
"""Compute subject-aware crop windows for landscape -> vertical reframing.

For each image, detect the main face (OpenCV Haar cascade) and return a crop
rectangle of the target aspect centred on the subject, so vertical reels keep
faces in frame instead of naive centre-cropping (which decapitates people).
Falls back to an upper-third-biased centre crop when no face is found.

Outputs a JSON map {image_path: {x, y, w, h}} in ORIGINAL-pixel coordinates.
build_reel.py consumes `--crop-map` and applies these before Ken Burns.

Kept separate so a basic reel doesn't pull in OpenCV.

Example:
    uv run smart_crop.py --aspect vertical --images a.jpg b.jpg --out crops.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ASPECTS = {"vertical": 9 / 16, "landscape": 16 / 9, "square": 1.0}


def crop_for(img_w: int, img_h: int, target_ratio: float, cx: float, cy: float) -> dict:
    """Largest rect of target_ratio (w/h) inside the image, centred near (cx, cy)."""
    if img_w / img_h > target_ratio:
        # image wider than target -> limit by height
        h = img_h
        w = int(round(h * target_ratio))
    else:
        w = img_w
        h = int(round(w / target_ratio))
    x = int(round(cx - w / 2))
    y = int(round(cy - h / 2))
    x = max(0, min(x, img_w - w))
    y = max(0, min(y, img_h - h))
    return {"x": x, "y": y, "w": w, "h": h}


def main() -> int:
    ap = argparse.ArgumentParser(description="Subject-aware crop windows for reframing.")
    ap.add_argument("--images", nargs="+", required=True)
    ap.add_argument("--aspect", choices=list(ASPECTS), default="vertical")
    ap.add_argument("--out", help="Write crop map JSON here (also printed).")
    args = ap.parse_args()

    try:
        import cv2
    except Exception as e:
        print(json.dumps({"error": f"opencv unavailable: {e}"}))
        return 2

    # Face detection is best-effort. If the cascade can't load (wheel/version
    # quirk), fall back to a heuristic upper-third crop rather than failing —
    # a reframed reel is still better than a hard error.
    cascade = None
    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if cascade.empty():
            cascade = None
    except Exception:
        cascade = None

    ratio = ASPECTS[args.aspect]
    crop_map: dict[str, dict] = {}

    for path in args.images:
        p = Path(path).expanduser()
        img = cv2.imread(str(p))
        if img is None:
            continue
        h, w = img.shape[:2]
        faces = []
        if cascade is not None:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
        if len(faces):
            # Centre on the union of detected faces.
            xs = [x + fw / 2 for (x, y, fw, fh) in faces]
            ys = [y + fh / 2 for (x, y, fw, fh) in faces]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        else:
            # No face: bias slightly above centre (where subjects usually sit).
            cx, cy = w / 2, h * 0.42
        crop_map[str(p)] = crop_for(w, h, ratio, cx, cy) | {"faces": int(len(faces))}

    if args.out:
        Path(args.out).expanduser().write_text(json.dumps(crop_map, indent=2), encoding="utf-8")
    print(json.dumps(crop_map, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
