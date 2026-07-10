# /// script
# requires-python = ">=3.10"
# dependencies = ["google-auth>=2.0", "requests>=2.28", "librosa>=0.10", "numpy>=1.24", "soundfile>=0.12"]
# ///
"""Generate a music bed with Google Lyria (Vertex AI), or use a library track.

Lyria (`lyria-002`) is a Vertex text-to-music model. It authenticates with your
existing gcloud Application Default Credentials (ADC) — no new API key. It
produces an instrumental clip (~30s, 48kHz WAV); this script loops/crossfades
it up to the requested reel duration and writes the bed.

Fallback: pass --library-track to skip generation and use an existing track.
When a library track is LONGER than the reel, this doesn't just start at 0:00 —
it analyzes the track and lifts the strongest ~`duration` window (the hook /
chorus / climax), snapped to a nearby onset for a clean entry, so the memorable
part of a real song lands on your reel. Override with --music-start, or keep the
old loop-from-the-top behavior with --from-start.

Examples:
    uv run make_music.py --prompt "warm, unhurried acoustic, summer nostalgia" \
        --duration 45 --output /tmp/reel/music.wav
    uv run make_music.py --library-track ~/Music/song.mp3 --duration 45 \
        --output /tmp/reel/music.m4a          # auto-picks the chorus/hook
    uv run make_music.py --library-track ~/Music/song.mp3 --duration 45 \
        --music-start 78 --output /tmp/reel/music.m4a   # force a start offset
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
                 "-t", f"{duration:.2f}", "-vn", "-af", f"afade=t=out:st={max(0,duration-1.5):.2f}:d=1.5",
                 str(out)])
        return p.returncode == 0 and out.exists()
    # stream_loop to cover duration, then trim + fade.
    loops = int(duration // src_dur) + 1
    p = run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", str(loops), "-i", str(src),
             "-t", f"{duration:.2f}", "-vn", "-af", f"afade=t=out:st={max(0,duration-1.5):.2f}:d=1.5",
             str(out)])
    return p.returncode == 0 and out.exists()


def pick_highlight(src: Path, duration: float) -> Optional[float]:
    """Return the start offset (s) of the track's strongest ~`duration` window —
    the hook/chorus/climax — snapped to a nearby onset for a clean cut-in.

    Heuristic: slide a window of the reel's length across the track's RMS-energy
    envelope and take the highest-energy window (a song's loudest sustained
    stretch is almost always its chorus/climax), then back up to the nearest
    onset within ~2s so the entry lands on a musical hit rather than mid-phrase.
    Returns None if analysis isn't possible (caller falls back to start=0)."""
    try:
        import numpy as np
        import librosa
    except Exception:
        return None
    try:
        y, sr = librosa.load(str(src), sr=22050, mono=True)
    except Exception:
        return None
    total = len(y) / sr if sr else 0.0
    if total <= duration + 1.0:
        return 0.0
    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    if rms.size == 0:
        return 0.0
    win = max(1, int(duration * sr / hop))
    if win >= rms.size:
        return 0.0
    csum = np.concatenate([[0.0], np.cumsum(rms.astype(np.float64))])
    sums = csum[win:] - csum[:-win]                 # total energy of each candidate window
    best = int(np.argmax(sums))
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    start = float(times[best])
    try:
        onsets = librosa.onset.onset_detect(y=y, sr=sr, hop_length=hop,
                                            backtrack=True, units="time")
        cand = [float(t) for t in onsets if start - 2.0 <= t <= start + 0.1]
        if cand:
            start = max(cand)
    except Exception:
        pass
    return max(0.0, min(start, total - duration))


def cut_segment(src: Path, out: Path, start: float, duration: float) -> bool:
    """Trim `duration` seconds from `src` starting at `start`, with a short
    fade-in and a fade-out so a mid-song entry sounds intentional."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fo = min(2.0, max(0.5, duration * 0.15))
    af = f"afade=t=in:st=0:d=0.6,afade=t=out:st={max(0.0, duration - fo):.2f}:d={fo:.2f}"
    p = run(["ffmpeg", "-y", "-loglevel", "error", "-accurate_seek",
             "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{duration:.2f}",
             "-vn", "-af", af, str(out)])   # -vn: drop any embedded album-art stream
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
    ap.add_argument("--music-start", type=float, default=None,
                    help="Force the library-track start offset (s). Default: auto-pick the highlight/chorus.")
    ap.add_argument("--from-start", action="store_true",
                    help="Use the library track from 0:00 (loop to length) instead of auto-picking the highlight.")
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
        src_dur = probe_duration(src)
        # Lift the strongest window (hook/chorus) unless told to loop from the top.
        if not args.from_start and src_dur > args.duration + 1.0:
            start = args.music_start if args.music_start is not None else pick_highlight(src, args.duration)
            if start is not None and cut_segment(src, out, start, args.duration):
                print(json.dumps({
                    "output": str(out), "source": "library",
                    "method": "manual_start" if args.music_start is not None else "auto_highlight",
                    "start": round(float(start), 2),
                    "duration": round(probe_duration(out), 2)}))
                return 0
            # analysis/cut unavailable → fall through to loop-from-start
        if not loop_to_duration(src, out, args.duration):
            print(json.dumps({"error": "failed to fit library track to duration"}))
            return 2
        print(json.dumps({"output": str(out), "source": "library",
                          "method": "loop_from_start", "duration": round(probe_duration(out), 2)}))
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
