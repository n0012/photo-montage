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


def detect_subject(img, face_cascades, body_cascades):
    """Find the subject center. Try frontal+profile faces, then upper body, then a
    detail-energy ("where's the interesting stuff") fallback — better than a blind
    upper-third guess for landscapes with no people. Returns (cx, cy, n_faces)."""
    import cv2
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    boxes = []
    for cas in face_cascades:
        for b in cas.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)):
            boxes.append(tuple(int(v) for v in b))
    n_faces = len(boxes)
    if not boxes:  # no faces — try upper body
        for cas in body_cascades:
            for b in cas.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(60, 120)):
                boxes.append(tuple(int(v) for v in b))

    if boxes:
        cxs = [x + bw / 2 for (x, y, bw, bh) in boxes]
        cys = [y + bh / 2 for (x, y, bw, bh) in boxes]
        return sum(cxs) / len(cxs), sum(cys) / len(cys), n_faces

    # Detail-energy saliency (core-cv2 only): Laplacian magnitude, heavily blurred,
    # take the peak — centers on the most textured/interesting region.
    try:
        lap = cv2.convertScaleAbs(cv2.Laplacian(gray, cv2.CV_16S, ksize=3))
        energy = cv2.GaussianBlur(lap, (0, 0), sigmaX=max(w, h) / 25.0)
        _, _, _, maxloc = cv2.minMaxLoc(energy)
        return float(maxloc[0]), float(maxloc[1]), 0
    except Exception:
        return w / 2, h * 0.42, 0


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
    def load_cascade(name):
        try:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            return None if c.empty() else c
        except Exception:
            return None

    face_cascades = [c for c in (load_cascade("haarcascade_frontalface_default.xml"),
                                 load_cascade("haarcascade_profileface.xml")) if c]
    body_cascades = [c for c in (load_cascade("haarcascade_upperbody.xml"),) if c]

    ratio = ASPECTS[args.aspect]
    crop_map: dict[str, dict] = {}

    for path in args.images:
        p = Path(path).expanduser()
        img = cv2.imread(str(p))
        if img is None:
            continue
        h, w = img.shape[:2]
        cx, cy, n_faces = detect_subject(img, face_cascades, body_cascades)
        # Headroom: with a person, nudge the crop down slightly so the subject
        # lands in the upper third (more natural than dead-centre).
        if n_faces:
            cy += 0.06 * h
        crop_map[str(p)] = crop_for(w, h, ratio, cx, cy) | {"faces": int(n_faces)}

    if args.out:
        Path(args.out).expanduser().write_text(json.dumps(crop_map, indent=2), encoding="utf-8")
    print(json.dumps(crop_map, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
