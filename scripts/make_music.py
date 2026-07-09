# /// script
# requires-python = ">=3.10"
# dependencies = ["google-auth>=2.0", "requests>=2.28"]
# ///
"""Generate a music bed with Google Lyria (Vertex AI), or use a library track.

Lyria (`lyria-002`) is a Vertex text-to-music model. It authenticates with your
existing gcloud Application Default Credentials (ADC) — no new API key. It
produces an instrumental clip (~30s, 48kHz WAV); this script loops/crossfades
it up to the requested reel duration and writes the bed.

Fallback: pass --library-track to skip generation and just fit an existing
track to length (fully offline/free).

Examples:
    uv run make_music.py --prompt "warm, unhurried acoustic, summer nostalgia" \
        --duration 45 --output /tmp/reel/music.wav
    uv run make_music.py --library-track ~/Music/beds/warm.mp3 --duration 45 \
        --output /tmp/reel/music.m4a
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import _env
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

VERTEX_MODEL = "lyria-002"


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)], timeout=60)
    try:
        return float((p.stdout or "0").strip())
    except ValueError:
        return 0.0


def loop_to_duration(src: Path, out: Path, duration: float) -> bool:
    """Loop `src` (with a short crossfade) until >= duration, then trim + fade out."""
    src_dur = probe_duration(src)
    if src_dur <= 0:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    if src_dur >= duration:
        p = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                 "-t", f"{duration:.2f}", "-af", f"afade=t=out:st={max(0,duration-1.5):.2f}:d=1.5",
                 str(out)])
        return p.returncode == 0 and out.exists()
    # stream_loop to cover duration, then trim + fade.
    loops = int(duration // src_dur) + 1
    p = run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", str(loops), "-i", str(src),
             "-t", f"{duration:.2f}", "-af", f"afade=t=out:st={max(0,duration-1.5):.2f}:d=1.5",
             str(out)])
    return p.returncode == 0 and out.exists()


def generate_lyria(prompt: str, negative_prompt: str, seed: Optional[int],
                   project: str, region: str, raw_out: Path) -> Optional[Path]:
    """Call the Vertex Lyria predict endpoint with an ADC bearer token."""
    import google.auth
    import google.auth.transport.requests
    import requests

    creds, adc_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    project = project or adc_project
    if not project:
        raise RuntimeError("No GCP project (set --project or PHOTO_MONTAGE_VERTEX_PROJECT).")

    url = (f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
           f"/locations/{region}/publishers/google/models/{VERTEX_MODEL}:predict")
    instance: dict = {"prompt": prompt}
    if negative_prompt:
        instance["negative_prompt"] = negative_prompt
    if seed is not None:
        instance["seed"] = seed
    body = {"instances": [instance], "parameters": {"sample_count": 1}}

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
        json=body, timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Lyria predict {resp.status_code}: {resp.text[:400]}")
    preds = resp.json().get("predictions") or []
    if not preds:
        raise RuntimeError("Lyria returned no predictions.")
    b64 = preds[0].get("bytesBase64Encoded") or preds[0].get("audioContent")
    if not b64:
        raise RuntimeError(f"Lyria prediction had no audio bytes: keys={list(preds[0])}")
    raw_out.write_bytes(base64.b64decode(b64))
    return raw_out


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Make a music bed via Lyria (Vertex) or a library track.")
    ap.add_argument("--output", required=True, help="Output music bed path (.wav/.m4a/.mp3).")
    ap.add_argument("--duration", type=float, required=True, help="Target length in seconds.")
    ap.add_argument("--prompt", help="Music description for Lyria (mood, instruments, tempo).")
    ap.add_argument("--negative-prompt", default="", help="What to avoid (e.g. 'vocals, drums').")
    ap.add_argument("--seed", type=int, help="Deterministic seed.")
    ap.add_argument("--library-track", help="Use this existing track instead of generating.")
    ap.add_argument("--project", help="GCP project (default: ADC / PHOTO_MONTAGE_VERTEX_PROJECT).")
    ap.add_argument("--region", default="us-central1", help="Vertex region for Lyria.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.output).expanduser()

    if args.library_track:
        src = Path(args.library_track).expanduser()
        if not src.is_file():
            print(json.dumps({"error": f"library track not found: {src}"}))
            return 2
        if not loop_to_duration(src, out, args.duration):
            print(json.dumps({"error": "failed to fit library track to duration"}))
            return 2
        print(json.dumps({"output": str(out), "source": "library", "duration": round(probe_duration(out), 2)}))
        return 0

    if not args.prompt:
        print(json.dumps({"error": "provide --prompt (to generate) or --library-track (to reuse)."}))
        return 2

    project = args.project or _env.vertex_project()
    with tempfile.TemporaryDirectory(prefix="lyria_") as tmp:
        raw = Path(tmp) / "lyria.wav"
        try:
            generate_lyria(args.prompt, args.negative_prompt, args.seed, project, args.region, raw)
        except Exception as e:
            print(json.dumps({
                "error": f"lyria generation failed: {type(e).__name__}: {e}",
                "hint": "Check the project has Vertex AI enabled + Lyria access in the region, "
                        "or pass --library-track to use an existing bed.",
            }))
            return 1
        if not loop_to_duration(raw, out, args.duration):
            print(json.dumps({"error": "failed to fit generated track to duration"}))
            return 2

    print(json.dumps({
        "output": str(out), "source": "lyria", "model": VERTEX_MODEL,
        "duration": round(probe_duration(out), 2), "prompt": args.prompt,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
