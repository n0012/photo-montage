# /// script
# requires-python = ">=3.10"
# dependencies = ["librosa>=0.10", "soundfile>=0.12"]
# ///
"""Detect beats/tempo in a music track so cuts can land on the beat.

Outputs the beat times (seconds) and tempo as JSON. build_reel.py consumes
`--beats-file` to snap shot boundaries to musical beats — the single biggest
"this looks professionally edited" upgrade, and it's pure local DSP (no AI).

Kept separate from build_reel.py so a basic reel doesn't pull in librosa.

Example:
    uv run analyze_audio.py --audio /tmp/reel/music.wav --beats-out /tmp/reel/beats.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Detect beats/tempo for beat-synced editing.")
    ap.add_argument("--audio", required=True, help="Music track to analyze.")
    ap.add_argument("--beats-out", help="Write beat JSON here (also printed to stdout).")
    ap.add_argument("--tightness", type=float, default=100.0, help="librosa beat tracker tightness.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    audio = Path(args.audio).expanduser()
    if not audio.is_file():
        print(json.dumps({"error": f"audio not found: {audio}"}))
        return 2

    try:
        import librosa
    except Exception as e:
        print(json.dumps({"error": f"librosa unavailable: {e}"}))
        return 2

    y, sr = librosa.load(str(audio), mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, tightness=args.tightness)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    beats = [round(float(t), 3) for t in beat_times]

    # librosa may return tempo as a 0-d/1-elem array.
    try:
        tempo_val = float(tempo)
    except (TypeError, ValueError):
        tempo_val = float(tempo[0]) if len(tempo) else 0.0

    result = {
        "audio": str(audio),
        "tempo_bpm": round(tempo_val, 1),
        "beat_count": len(beats),
        "beats": beats,
        "duration_seconds": round(float(len(y) / sr), 2),
    }
    if args.beats_out:
        Path(args.beats_out).expanduser().write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "beats"} | {"beats_preview": beats[:12]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
