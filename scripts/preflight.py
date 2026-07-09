# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["google-genai>=1.0.0", "osxphotos==0.76.1"]
# ///
"""Preflight — check capabilities and recommend the model/endpoint up front.

Avoids the fumbling we hit live: gemini-3.5-flash 404s on us-central1 (3.x is
served from the `global` endpoint) and 429s on very large multi-image pools
(use gemini-3.1-pro-preview there). This probes what's actually reachable and
prints a recommendation, so the run picks the right model/endpoint and sizes
its batches before doing work.

Usage:
    uv run preflight.py                 # full check
    uv run preflight.py --pool 99       # also recommend a model for a pool size
"""

from __future__ import annotations

import argparse
import json
import os
import _env
import shutil
import subprocess
import sys


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def probe_gemini(models, location="global"):
    from google import genai
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    out = {}
    try:
        if key:
            client = genai.Client(api_key=key); backend = "dev"
        else:
            proj = _env.vertex_project()
            if not proj:
                return {"error": "no GEMINI_API_KEY and no PHOTO_MONTAGE_VERTEX_PROJECT"}, None
            client = genai.Client(vertexai=True, project=proj, location=location); backend = f"vertex/{location}"
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, None
    for m in models:
        try:
            client.models.generate_content(model=m, contents="ok")
            out[m] = "ok"
        except Exception as e:
            out[m] = "404" if "404" in str(e) else ("429" if "429" in str(e) else "err")
    return out, backend


def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight capabilities + model recommendation.")
    ap.add_argument("--pool", type=int, help="Candidate pool size to recommend a director model for.")
    args = ap.parse_args()

    report = {
        "ffmpeg": have("ffmpeg"),
        "sips": have("sips"),
        "uv": have("uv"),
        "gemini_api_key": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
        "vertex_project": _env.vertex_project(),
    }
    # osxphotos + real Photos-library access (confirms Full Disk Access)
    try:
        import osxphotos
        db = osxphotos.PhotosDB()
        report["osxphotos"] = True
        report["photos_library_ok"] = True
        report["photos_count"] = len(db.photos(intrash=False))
    except Exception as e:
        report["osxphotos"] = report.get("osxphotos", False)
        report["photos_library_ok"] = False
        report["photos_error"] = str(e)[:120]
    # RAID music mount
    report["raid_music_mounted"] = os.path.isdir("/Volumes/RAID/Music") or os.path.isdir(
        os.path.expanduser("~/nas-raid/Music"))

    # Gemini models on the global endpoint
    models = ["gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-2.5-flash"]
    report["gemini"], report["gemini_backend"] = probe_gemini(models)

    # Recommend a director model
    flash_ok = report["gemini"].get("gemini-3.5-flash") == "ok"
    pro_ok = report["gemini"].get("gemini-3.1-pro-preview") == "ok"
    if args.pool and args.pool > 60 and pro_ok:
        rec = "gemini-3.1-pro-preview"  # large pool -> pro (flash 429s)
    elif flash_ok:
        rec = "gemini-3.5-flash"
    elif pro_ok:
        rec = "gemini-3.1-pro-preview"
    else:
        rec = report["gemini"].get("gemini-2.5-flash") == "ok" and "gemini-2.5-flash" or None
    report["recommended_director_model"] = rec
    report["notes"] = [
        "Gemini 3.x is only on the global endpoint (us-central1 -> 404).",
        "Pools >~60 images: use gemini-3.1-pro-preview (3.5-flash rate-limits/429).",
        "Set PHOTO_MONTAGE_GEMINI_MODEL / VERTEX_MODEL_LOCATION to override.",
    ]
    ready = report["ffmpeg"] and report.get("photos_library_ok") and bool(rec)
    report["ready"] = ready
    print(json.dumps(report, indent=2))
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
