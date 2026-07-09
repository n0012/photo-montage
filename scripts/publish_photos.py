# /// script
# requires-python = ">=3.10"
# dependencies = ["photoscript"]
# ///
"""Import the finished reel back into Apple Photos, optionally into an album.

Uses photoscript (a dependency of osxphotos) which drives Photos.app via
AppleScript — so the video lands in your real library, syncs to your devices,
and can appear in Memories. On-device only; no upload.

The first run triggers a macOS automation-permission prompt ("Terminal wants
to control Photos") — approve it once.

Example:
    uv run publish_photos.py --video reel.mp4 --album "4th of July 2026"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Import a video into Apple Photos.")
    ap.add_argument("--video", required=True, help="Path to the finished reel.")
    ap.add_argument("--album", help="Album to file it under (created if missing).")
    ap.add_argument("--allow-duplicates", action="store_true",
                    help="Import even if Photos thinks it's a duplicate.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    video = Path(args.video).expanduser()
    if not video.is_file():
        print(json.dumps({"error": f"video not found: {video}"}))
        return 2

    try:
        from photoscript import PhotosLibrary
    except Exception:
        print(json.dumps({"error": "photoscript not available (macOS + `pip install osxphotos`)."}))
        return 2

    lib = PhotosLibrary()

    album = None
    album_name = args.album or _env.default_album()
    args.album = album_name
    if args.album:
        # album() returns the first match or None; create it if absent.
        try:
            album = lib.album(args.album)
        except Exception:
            album = None
        if album is None:
            album = lib.create_album(args.album)

    try:
        imported = lib.import_photos(
            [str(video)],
            album=album,
            skip_duplicate_check=args.allow_duplicates,
        )
    except Exception as e:
        print(json.dumps({"error": f"import failed: {type(e).__name__}: {e}"}))
        return 1

    uuids = []
    for ph in imported or []:
        try:
            uuids.append(ph.uuid)
        except Exception:
            pass

    print(json.dumps({
        "status": "published" if uuids else "no_asset_returned",
        "imported_uuids": uuids,
        "album": args.album,
        "video": str(video),
    }, indent=2))
    return 0 if uuids else 1


if __name__ == "__main__":
    sys.exit(main())
