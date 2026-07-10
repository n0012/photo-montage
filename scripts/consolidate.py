# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Consolidate every candidate in a project workspace into one final pool.

Canonical workspace convention (keep ALL of a run's artifacts in one tree so
nothing gets stranded in scattered batches):

    projects/<event>/
      candidates/…/photos.json        # any select_photos.py runs
      clips/…/segments.json           # any clip_videos.py runs
      all_photos.json  all_segments.json   # <- written by this script
      plan.json order.json crops.json cover.png music/… renders/final.mp4

This walks the workspace, merges every photo manifest (photos.json /
candidates.json / combined*.json) and every segment manifest (segments*.json),
dedups clips and writes the merged pool. Run it
right before the final director pass so it considers EVERYTHING.

(Learned the hard way: a great shot clipped in one batch got missed because the
director ran over another batch — and `find /tmp` silently returns nothing
because /tmp is a symlink. This script walks the given dir directly.)

Usage:
    uv run consolidate.py --workspace projects/rocky-mountain-fourth
    uv run consolidate.py --dirs /tmp/rmf /tmp/rmf2 /tmp/rmf_boot   # explicit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PHOTO_NAMES = {"photos.json", "candidates.json"}
PHOTO_PREFIX = "combined"


def find_manifests(roots: list[Path]):
    photos, segs = [], []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f in PHOTO_NAMES or (f.startswith(PHOTO_PREFIX) and f.endswith(".json")):
                    photos.append(Path(dirpath) / f)
                elif f.startswith("segments") and f.endswith(".json"):
                    segs.append(Path(dirpath) / f)
    return sorted(set(photos)), sorted(set(segs))


def load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolidate all candidates into one final pool.")
    ap.add_argument("--workspace", help="Project workspace dir to walk.")
    ap.add_argument("--dirs", nargs="*", help="Explicit dirs to scan (instead of --workspace).")
    ap.add_argument("--out-dir", help="Where to write all_photos.json/all_segments.json (default: workspace).")
    args = ap.parse_args()

    roots = [Path(d).expanduser() for d in (args.dirs or [])]
    if args.workspace:
        roots.append(Path(args.workspace).expanduser())
    if not roots:
        print(json.dumps({"error": "give --workspace or --dirs"})); return 2
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else roots[0]
    out_dir.mkdir(parents=True, exist_ok=True)

    pm, sm = find_manifests(roots)

    # photos: dedup by (date,w,h), keep type image with an existing file
    seen, photos = set(), []
    for m in pm:
        for it in load(m).get("items", []):
            if it.get("type") != "image":
                continue
            if not os.path.exists(it.get("path", "")):
                continue
            k = (it.get("date"), it.get("width"), it.get("height"))
            if k in seen:
                continue
            seen.add(k); photos.append(it)

    # segments: dedup by source basename (keep highest score)
    best = {}
    for m in sm:
        for s in load(m).get("segments", []):
            if not os.path.exists(s.get("path", "")):
                continue
            base = os.path.basename(s.get("source", "")).split(".")[0]
            if base and (base not in best or s.get("score", 0) > best[base].get("score", 0)):
                best[base] = s
    segs = list(best.values())

    (out_dir / "all_photos.json").write_text(json.dumps({"items": photos}, indent=2), encoding="utf-8")
    (out_dir / "all_segments.json").write_text(json.dumps({"segments": segs}, indent=2), encoding="utf-8")

    print(json.dumps({
        "photo_manifests": [str(p) for p in pm],
        "segment_manifests": [str(p) for p in sm],
        "photos": len(photos),
        "video_segments": len(segs),
        "video_sources": sorted({os.path.basename(s["source"]).split(".")[0] for s in segs}),
        "out_dir": str(out_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
