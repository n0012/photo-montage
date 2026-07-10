# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.0.0"]
# ///
"""Auto self-review — a critic pass over the finished reel before you watch it.

Samples frames across the rendered reel, sends them (each labeled with its
timecode) to Gemini, and asks for problems a human editor would catch:
off-story/irrelevant shots (judged against the reel's subject), near-duplicates,
blurry/weak frames, pacing that's too fast or too slow, a weak ending, and — since it sees the
FINAL rendered frames — shots that are visibly rotated/sideways (an orientation
safety net for EXIF bugs deterministic handling can miss; reported as flags with a
rotation direction + confidence, for the agent to confirm, not auto-apply). Also
computes a local slideshow-risk score from the plan. Returns JSON so the agent can
fix issues (e.g. drop the flagged shot) before finalizing.

This is what would have caught an off-story boat-engine or AC-unit shot in a family
trip reel, and a sideways-rendered box, automatically instead of by eye.

Usage:
    uv run review_reel.py --reel reel.mp4 --order order.json
"""

from __future__ import annotations

import argparse
import json
import os
import _env
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)])
    try:
        return float((p.stdout or "0").strip())
    except ValueError:
        return 0.0


def slideshow_risk(order_path: Path) -> dict:
    """Heuristic 0-1 (higher = more slideshow-y): mostly stills, short holds."""
    try:
        order = json.loads(order_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    shots = order if isinstance(order, list) else order.get("shots", [])
    if not shots:
        return {}
    n = len(shots)
    vids = sum(1 for s in shots if s.get("type") == "video")
    holds = [float(s.get("hold", 2.5)) for s in shots]
    avg = sum(holds) / len(holds)
    still_ratio = 1 - vids / n
    # short holds + few videos -> higher risk
    risk = max(0.0, min(1.0, 0.6 * still_ratio + 0.4 * max(0.0, (2.6 - avg) / 2.6)))
    advice = []
    if still_ratio > 0.8: advice.append("mostly stills — weave in more video segments")
    if avg < 2.2: advice.append("holds are short — raise --hold-scale or slow dissolves")
    if vids == 0: advice.append("no video — it will read as a slideshow")
    return {"score": round(risk, 2), "still_ratio": round(still_ratio, 2),
            "avg_hold_s": round(avg, 2), "videos": vids, "shots": n, "advice": advice}


def make_client():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return genai.Client(api_key=key)
    proj = _env.vertex_project()
    if not proj:
        return None
    loc = _env.vertex_location()
    return genai.Client(vertexai=True, project=proj, location=loc)


PROMPT = """You are a picky video editor reviewing a family memory reel before release.
The images are frames sampled from the reel, each labeled with its timecode.
Identify problems a good editor would fix. Return ONLY JSON:
{"verdict": "ship" | "fix",
 "issues": [{"time_s": <number>, "problem": "<what's wrong: off-story/irrelevant shot, near-duplicate, blurry, off-tone, etc.>", "fix": "<drop it / replace / trim>"}],
 "orientation": [{"time_s": <number>, "direction": "cw|ccw|180", "confidence": "high|medium|low", "note": "<why it looks rotated>"}],
 "pacing": "<too fast | good | too slow>",
 "ending": "<does it end on a strong, calm closer? issues?>",
 "notes": "<one-line overall>"}
Be specific with time_s. Flag shots that are OFF-STORY / irrelevant to this reel's
subject, near-duplicates, or blurry/weak frames. Judge by the STORY: infer the reel's
subject from the surrounding shots, and flag a shot only when it's genuinely unrelated
to that subject — NOT merely for its content type. The same kind of shot can be
off-story in one reel and the whole point in another, so don't penalize a shot just for
what it depicts.

ORIENTATION CHECK (these are RENDERED frames — what actually ships): if a shot is
visibly ROTATED — text/logos running vertically, faces or a person sideways, the
horizon vertical, a table/floor on a side wall — add it to "orientation" with the
rotation that would make it upright ("cw" = rotate clockwise, "ccw" =
counter-clockwise, "180" = flip). This catches orientation bugs that survive to the
final render. Guard against FALSE POSITIVES: do NOT flag genuine top-down / flat-lay
shots, aerials, or abstract close-ups where "up" is truly ambiguous — only clear
cases. Give "high" confidence only when it's unmistakable. Set verdict "fix" if any
high-confidence orientation problem exists. Empty list [] if all shots are upright."""


def main() -> int:
    ap = argparse.ArgumentParser(description="Critic pass over a finished reel.")
    ap.add_argument("--reel", required=True)
    ap.add_argument("--order", help="order.json for slideshow-risk + timeline.")
    ap.add_argument("--interval", type=float, default=3.0, help="Seconds between sampled frames.")
    ap.add_argument("--model", default=os.environ.get("PHOTO_MONTAGE_GEMINI_MODEL", "gemini-3.5-flash"))
    args = ap.parse_args()

    reel = Path(args.reel).expanduser()
    if not reel.is_file():
        print(json.dumps({"error": f"reel not found: {reel}"})); return 2
    dur = probe_duration(reel)
    risk = slideshow_risk(Path(args.order).expanduser()) if args.order else {}

    try:
        from google.genai import types
    except Exception:
        print(json.dumps({"error": "google-genai unavailable", "slideshow_risk": risk})); return 2
    client = make_client()
    if client is None:
        print(json.dumps({"error": "no_auth", "slideshow_risk": risk})); return 3

    with tempfile.TemporaryDirectory(prefix="review_") as tmp:
        contents = [f"Reel duration {dur:.1f}s. Frames follow, each with its timecode."]
        t = 0.5
        while t < dur:
            fp = Path(tmp) / f"{int(t*10):04d}.jpg"
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(reel),
                 "-frames:v", "1", "-vf", "scale=360:-1", str(fp)])
            if fp.exists():
                contents.append(f"t={t:.1f}s")
                contents.append(types.Part.from_bytes(data=fp.read_bytes(), mime_type="image/jpeg"))
            t += args.interval
        contents.append(PROMPT)
        try:
            resp = client.models.generate_content(
                model=args.model, contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"))
            crit = json.loads(resp.text)
        except Exception as e:
            print(json.dumps({"error": f"critic failed: {type(e).__name__}: {e}", "slideshow_risk": risk})); return 1

    crit["slideshow_risk"] = risk
    crit["duration_s"] = round(dur, 1)
    print(json.dumps(crit, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
