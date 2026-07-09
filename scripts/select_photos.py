# /// script
# requires-python = ">=3.10"
# dependencies = ["osxphotos"]
# ///
"""Select and export the best stills/clips from Apple Photos for a montage.

Reads the on-device Photos library via osxphotos, filters by event window /
album / person / keyword / favourite, ranks by Apple's own per-asset aesthetic
score, culls screenshots and near-duplicate bursts, exports the chosen
originals (converting HEIC -> JPEG so ffmpeg/Remotion can decode them), and
prints a JSON manifest of the candidates to stdout.

This is the deterministic "find the good shots" half. The agent reads the
manifest + thumbnails and decides the final order / holds (the taste half).

Everything is read-only against the library and fully on-device.

Run with uv (auto-installs osxphotos):
    uv run select_photos.py --output-dir /tmp/reel --since-days 4 --max-items 40
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------
# Metadata helpers
# --------------------------------------------------------------------------

def overall_score(photo) -> float:
    score = getattr(photo, "score", None)
    overall = getattr(score, "overall", None) if score is not None else None
    try:
        return float(overall) if overall is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def photo_ts(photo) -> Optional[float]:
    date = getattr(photo, "date", None)
    try:
        return date.timestamp() if date is not None else None
    except (AttributeError, OSError, ValueError):
        return None


def has_local_original(photo) -> bool:
    path = getattr(photo, "path", None)
    if path and Path(path).exists():
        return True
    edited = getattr(photo, "path_edited", None)
    return bool(edited and Path(edited).exists())


def dedupe_bursts(photos: list) -> list:
    """Collapse each burst set to its single highest-scored frame."""
    best_by_burst: dict[str, Any] = {}
    singles: list = []
    for p in photos:
        if getattr(p, "burst", False) and getattr(p, "burstid", None):
            bid = p.burstid
            cur = best_by_burst.get(bid)
            if cur is None or overall_score(p) > overall_score(cur):
                best_by_burst[bid] = p
        else:
            singles.append(p)
    return singles + list(best_by_burst.values())


def convert_to_jpeg(heic_path: Path) -> Optional[Path]:
    """Convert HEIC/HEIF -> JPEG via macOS `sips`; remove the original."""
    jpg_path = heic_path.with_suffix(".jpg")
    try:
        proc = subprocess.run(
            ["sips", "-s", "format", "jpeg", str(heic_path), "--out", str(jpg_path)],
            capture_output=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not jpg_path.exists():
        return None
    try:
        heic_path.unlink()
    except OSError:
        pass
    return jpg_path


def extract_thumbnail(asset_path: Path, thumb_path: Path, kind: str) -> None:
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "image":
        cmd = ["ffmpeg", "-y", "-i", str(asset_path), "-vframes", "1",
               "-vf", "scale=640:-1", "-q:v", "3", str(thumb_path)]
    else:
        cmd = ["ffmpeg", "-y", "-ss", "1", "-i", str(asset_path),
               "-frames:v", "1", "-vf", "scale=640:-1", "-q:v", "3", str(thumb_path)]
    subprocess.run(cmd, capture_output=True, timeout=30)


# Apple ML labels that mark odd, non-montage "equipment" shots (AC units,
# engines, appliances) that the director occasionally keeps. Dropped by default.
MECHANICAL_LABELS = {
    "air conditioner", "electric fan", "mechanical fan", "machine", "appliance",
    "motor", "engine", "heater", "furnace", "generator", "pump",
}


def _is_mechanical(photo) -> bool:
    labels = {str(l).lower() for l in (getattr(photo, "labels", None) or [])}
    return bool(labels & MECHANICAL_LABELS)


def build_record(photo, asset_path: Path) -> dict[str, Any]:
    is_movie = bool(getattr(photo, "ismovie", False))
    score = getattr(photo, "score", None)
    location = getattr(photo, "location", None) or (None, None)
    place = getattr(photo, "place", None)
    place_name = getattr(place, "name", None) if place else None
    moment = getattr(photo, "moment", None)
    moment_info = None
    if moment is not None:
        moment_info = {
            "title": getattr(moment, "title", None),
            "subtitle": getattr(moment, "subtitle", None),
        }
    date = getattr(photo, "date", None)
    record: dict[str, Any] = {
        "uuid": photo.uuid,
        "type": "video" if is_movie else "image",
        "path": str(asset_path),
        "original_filename": getattr(photo, "original_filename", None),
        "date": date.isoformat() if date is not None else None,
        "favorite": bool(getattr(photo, "favorite", False)),
        "keywords": list(getattr(photo, "keywords", []) or []),
        "labels": list(getattr(photo, "labels", []) or []),
        "persons": [pn for pn in (getattr(photo, "persons", []) or []) if pn != "_UNKNOWN_"],
        "albums": list(getattr(photo, "albums", []) or []),
        "title": getattr(photo, "title", None),
        "place": place_name,
        "location": {"lat": location[0], "lon": location[1]} if location and location[0] is not None else None,
        "moment": moment_info,
        "width": getattr(photo, "width", None),
        "height": getattr(photo, "height", None),
        "aesthetic_score": round(overall_score(photo), 4),
        "is_screenshot": bool(getattr(photo, "screenshot", False)),
        "is_selfie": bool(getattr(photo, "selfie", False)),
        "is_live_photo": bool(getattr(photo, "live_photo", False)),
    }
    if score is not None:
        record["scores"] = {
            key: getattr(score, key, None)
            for key in ("overall", "curation", "promotion", "pleasant_composition",
                        "well_timed_shot", "well_chosen_subject", "sharply_focused_subject")
            if getattr(score, key, None) is not None
        }
    duration = getattr(photo, "duration", None)
    if is_movie and duration is not None:
        record["duration_seconds"] = round(float(duration), 2)
    return record


def moment_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    for it in items:
        moment = it.get("moment") or {}
        title = moment.get("title") or it.get("place") or "Untitled event"
        if title not in groups:
            groups[title] = {"title": title, "count": 0, "uuids": [], "place": it.get("place")}
            order.append(title)
        groups[title]["count"] += 1
        groups[title]["uuids"].append(it["uuid"])
    return [groups[t] for t in order]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def download_export(photo, media_dir: Path, convert_heic: bool, use_edited: bool):
    """Pull an iCloud-only original via PhotoKit and export it. Returns the
    chosen asset path (image or video) or None. Fast + reliable vs AppleScript."""
    from osxphotos import PhotoExporter, ExportOptions
    opts = ExportOptions(
        download_missing=True, use_photokit=True, overwrite=True,
        edited=use_edited and getattr(photo, "hasadjustments", False),
        convert_to_jpeg=convert_heic and not getattr(photo, "ismovie", False),
    )
    res = PhotoExporter(photo).export(str(media_dir), options=opts)
    files = [Path(f) for f in (getattr(res, "exported", res) or [])]
    if not files:
        return None
    want_video = bool(getattr(photo, "ismovie", False))
    vids = {".mov", ".mp4", ".m4v"}
    # Prefer the asset matching the item's kind (Live Photos export still+video).
    for f in files:
        if (f.suffix.lower() in vids) == want_video:
            return f
    return files[0]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Select best Apple Photos media for a montage.")
    ap.add_argument("--output-dir", required=True, help="Where to export media + thumbnails.")
    ap.add_argument("--library-path", help="Path to a specific .photoslibrary (default: system).")
    ap.add_argument("--since-days", type=int, help="Include items from the last N days.")
    ap.add_argument("--from-date", help="ISO lower bound, e.g. 2026-07-03.")
    ap.add_argument("--to-date", help="ISO upper bound, e.g. 2026-07-06.")
    ap.add_argument("--albums", nargs="*", help="Restrict to these album titles.")
    ap.add_argument("--shared-albums", nargs="*",
                    help="Also include items from these shared iCloud album titles "
                         "(e.g. a family shared album). Merged with the main query.")
    ap.add_argument("--keywords", nargs="*", help="Restrict to these keywords.")
    ap.add_argument("--persons", nargs="*", help="Restrict to these named people.")
    ap.add_argument("--favorites-only", action="store_true")
    ap.add_argument("--media-type", choices=["photo", "video", "any"], default="any")
    ap.add_argument("--max-items", type=int, default=60)
    ap.add_argument("--min-score", type=float, help="Drop items below this aesthetic score (0-1).")
    ap.add_argument("--sort-by", choices=["score", "date"], default="score",
                    help="Which ranking survives the max-items cap (output is always chronological).")
    ap.add_argument("--only-local", action="store_true",
                    help="Only items whose original is already on disk (skip iCloud-only).")
    ap.add_argument("--download-missing", action="store_true",
                    help="Pull iCloud-only originals on demand via PhotoKit (needs Photos "
                         "automation permission). Lets you montage a whole event, not just "
                         "what's already downloaded.")
    ap.add_argument("--keep-screenshots", action="store_true")
    ap.add_argument("--keep-mechanical", action="store_true",
                    help="Keep equipment shots (AC units, engines, appliances). "
                         "By default these are dropped via Apple ML labels.")
    ap.add_argument("--no-dedupe-bursts", action="store_true")
    ap.add_argument("--no-convert-heic", action="store_true")
    ap.add_argument("--no-thumbnails", action="store_true")
    ap.add_argument("--manifest-out", help="Also write the JSON manifest to this path.")
    return ap.parse_args()


def resolve_window(args) -> tuple[Optional[datetime], Optional[datetime]]:
    frm = datetime.fromisoformat(args.from_date) if args.from_date else None
    to = datetime.fromisoformat(args.to_date) if args.to_date else None
    if frm is None and args.since_days:
        frm = datetime.now() - timedelta(days=args.since_days)
    return frm, to


def main() -> int:
    args = parse_args()
    try:
        import osxphotos
    except Exception:
        print(json.dumps({"error": "osxphotos not available (macOS + `pip install osxphotos`)."}))
        return 2

    output_dir = Path(args.output_dir).expanduser()
    media_dir = output_dir / "media"
    thumbs_dir = output_dir / "thumbnails"
    media_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_thumbnails:
        thumbs_dir.mkdir(parents=True, exist_ok=True)

    want_images = args.media_type in ("photo", "any")
    want_movies = args.media_type in ("video", "any")
    frm, to = resolve_window(args)

    db = osxphotos.PhotosDB(dbfile=args.library_path) if args.library_path else osxphotos.PhotosDB()

    query: dict[str, Any] = {"images": want_images, "movies": want_movies, "intrash": False}
    if args.albums:
        query["albums"] = args.albums
    if args.keywords:
        query["keywords"] = args.keywords
    if args.persons:
        query["persons"] = args.persons
    if frm is not None:
        query["from_date"] = frm
    if to is not None:
        query["to_date"] = to

    photos = db.photos(**query)

    # Shared iCloud albums (e.g. a shared album) aren't matched by the
    # regular albums= query — pull them explicitly and merge (dedup by uuid).
    shared_alb = getattr(args, "shared_albums", None) or _env.shared_albums()
    if shared_alb:
        want = set(shared_alb)
        seen = {p.uuid for p in photos}
        for a in getattr(db, "album_info_shared", []) or []:
            if a.title in want:
                for p in a.photos:
                    if p.uuid in seen:
                        continue
                    if want_images and p.isphoto or want_movies and p.ismovie:
                        photos.append(p)
                        seen.add(p.uuid)

    # Date-window filter (applies to shared-album photos too; timestamp compare
    # avoids tz-aware vs naive issues).
    frm_ts = frm.timestamp() if frm else None
    to_ts = to.timestamp() if to else None

    filtered = []
    for p in photos:
        if frm_ts is not None and (photo_ts(p) or 0) < frm_ts:
            continue
        if to_ts is not None and (photo_ts(p) or 0) > to_ts:
            continue
        if args.favorites_only and not getattr(p, "favorite", False):
            continue
        if not args.keep_screenshots and getattr(p, "screenshot", False):
            continue
        if args.only_local and not has_local_original(p):
            continue
        if args.min_score is not None and overall_score(p) < args.min_score:
            continue
        if not args.keep_mechanical and _is_mechanical(p):
            continue
        filtered.append(p)

    if not args.no_dedupe_bursts:
        filtered = dedupe_bursts(filtered)

    candidates_matched = len(filtered)

    if args.sort_by == "date":
        filtered.sort(key=lambda p: (photo_ts(p) or 0))
    else:
        filtered.sort(key=overall_score, reverse=True)
    selected = filtered[: args.max_items]
    selected.sort(key=lambda p: (photo_ts(p) or 0))  # deliverable order = chronological

    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped = 0
    convert_heic = not args.no_convert_heic

    download_missing = bool(getattr(args, "download_missing", False))

    for p in selected:
        local = has_local_original(p)
        if not local and not download_missing:
            skipped += 1
            errors.append({"uuid": p.uuid,
                           "error": "original not on disk (iCloud) — use --download-missing or "
                                    "download it in Photos first"})
            continue

        asset_path = None
        try:
            if local:
                exported = p.export(str(media_dir),
                                    edited=getattr(p, "hasadjustments", False), overwrite=True)
                asset_path = Path(exported[0]) if exported else None
                # HEIC->JPG via sips for the local path (export() can't convert here).
                if (asset_path and convert_heic and not getattr(p, "ismovie", False)
                        and asset_path.suffix.lower() in (".heic", ".heif")):
                    converted = convert_to_jpeg(asset_path)
                    if converted is not None:
                        asset_path = converted
            else:
                # iCloud-only: pull via PhotoKit (ExportOptions handles HEIC->JPG).
                asset_path = download_export(p, media_dir, convert_heic,
                                             use_edited=True)
        except Exception as e:
            skipped += 1
            errors.append({"uuid": p.uuid, "error": f"{type(e).__name__}: {e}"})
            continue

        if not asset_path or not asset_path.exists():
            skipped += 1
            errors.append({"uuid": p.uuid, "error": "no file produced"})
            continue

        record = build_record(p, asset_path)
        if not args.no_thumbnails:
            thumb = thumbs_dir / f"{p.uuid}.jpg"
            try:
                extract_thumbnail(asset_path, thumb, record["type"])
                if thumb.exists():
                    record["thumbnail"] = str(thumb)
            except Exception:
                pass
        items.append(record)

    result = {
        "output_dir": str(output_dir),
        "candidates_matched": candidates_matched,
        "items_exported": len(items),
        "items_skipped": skipped,
        "moments": moment_summary(items),
        "items": items,
        "errors": errors[:50],
    }
    if args.manifest_out:
        Path(args.manifest_out).expanduser().write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
