# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.0.0"]
# ///
"""Gemini director — review the whole candidate set and return an edit plan.

This is the step that makes Gemini the *editor*, not just the clip-trimmer.
It sends every candidate (still thumbnails + trimmed video-segment frames) plus
compact metadata (aesthetic score, date, place, people, and — for clips — the
scene/why from clip_videos.py) to Gemini multimodal, and asks for an ordered
edit plan for a short vertical reel:

  - which items to include and in what order (a story, not a dump),
  - a hold (seconds) and Ken Burns motion per shot,
  - a transition suggestion per cut,
  - an overall vibe, a music prompt (for make_music.py / Lyria),
  - a short narration script (for make_voiceover.py; optional to use).

Images are sent inline (small thumbnails), so this works on both the Gemini
Developer API (GEMINI_API_KEY) and Vertex (ADC) without the Files API or GCS.

Output JSON (stdout + optional --plan-out):
  {"vibe": "...", "music_prompt": "...", "negative_music_prompt": "...",
   "narration": "...",
   "shots": [{"path": "...", "type": "image|video", "hold": 3.0,
              "motion": "push_in", "transition": "cut|dissolve", "reason": "..."}]}

Example:
  uv run plan_edit.py --from-select candidates.json --segments segments.json \
      --duration 40 --plan-out plan.json
"""

from __future__ import annotations

import argparse
import json
import os
import _env
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _ts(date_str) -> Optional[float]:
    """Parse an ISO date string to a sortable epoch, or None."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(str(date_str)).timestamp()
    except (ValueError, TypeError):
        return None


def _aspect(w, h, orientation=None) -> Optional[float]:
    """True display aspect (w/h), accounting for EXIF rotation (orientations 5-8 swap axes)."""
    if not w or not h:
        return None
    if orientation in (5, 6, 7, 8):
        w, h = h, w
    try:
        return round(w / h, 2)
    except ZeroDivisionError:
        return None

MOTIONS = ["push_in", "pull_out", "drift_left", "drift_right", "none"]

INSTR = """You are an award-winning editor cutting a VERTICAL memory reel
(YouTube Shorts / Reels / TikTok) from the candidate media above.

Each candidate is shown as [index] a thumbnail + metadata. Video segments are
already trimmed to a good moment (their scene/why is given).

FIRST decide the IDEAL total duration between {dur_min} and {dur_max} seconds,
based on how many candidates are genuinely STRONG and chain well together. Use
the length the material earns — long enough to tell the story, but do NOT pad
with weak, blurry, or near-duplicate shots. A tight {dur_min}s cut beats a
padded {dur_max}s one. Put your choice in recommended_duration_seconds.

Then produce a tasteful, professionally-edited plan:
- SELECT the strongest shots that flow together (cull weak/dupe/blurry).
- ORDER them CHRONOLOGICALLY by the `date` in each item's metadata (earliest
  first). Items group into day/place scenes — keep whole scenes in time order and
  NEVER place a later-dated shot before an earlier one (e.g. a next-day boat trip
  must come AFTER the prior night's fireworks). Within a single scene, arrange for
  flow: open on an establishing/hook shot, build, hit peaks, wind down.
- VARIETY: never place two near-identical shots adjacent; alternate wide/detail
  and people/place; mix videos and photos.
- FORMAT: this is a 9:16 VERTICAL reel. Prefer shots with meta portrait:true —
  they fill the frame; portrait:false (landscape) shots get cropped, so use them
  sparingly, mainly as brief establishing beats.
- SCENES: use `labels` (Apple's on-device scene tags, e.g. "Fireworks", "Boat",
  "Mountain") together with `place`/`date` to recognize distinct scenes and pick
  strong establishing shots to open each one.
- HOLD per shot in seconds (heroes ~3-4s, connective ~1.5-2s) so the total ≈
  recommended_duration_seconds.
- MOTION per still: push_in, pull_out, drift_left, drift_right, none. Video
  segments use motion "none".
- TRANSITION into each shot: "cut" (default) or "dissolve" (sparingly).
- Overall VIBE, a MUSIC PROMPT (mood/instruments/tempo, instrumental), a
  NEGATIVE music prompt, and a short NARRATION script (may be empty).

Return ONLY valid JSON:
{{"recommended_duration_seconds": <number>, "vibe": "...", "music_prompt": "...",
  "negative_music_prompt": "...", "narration": "...",
  "shots": [{{"index": <int>, "hold": <sec>, "motion": "<motion>",
             "transition": "cut|dissolve", "reason": "<why here>"}}]}}
Use each index at most once. Order the shots array in final timeline order."""


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def frame_from_video(path: Path, out: Path) -> Optional[Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=480:-1", "-q:v", "4", str(out)])
    return out if (r.returncode == 0 and out.exists()) else None


def extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def load_candidates(args, workdir: Path) -> list[dict[str, Any]]:
    """Build a unified candidate list: stills (thumbnails) + clip segments (frames)."""
    cands: list[dict[str, Any]] = []
    if args.from_select:
        d = json.loads(Path(args.from_select).expanduser().read_text(encoding="utf-8"))
        for it in d.get("items", []):
            thumb = it.get("thumbnail") or it.get("path")
            asp = _aspect(it.get("width"), it.get("height"), it.get("orientation"))
            cands.append({
                "path": it["path"], "type": it["type"], "image": thumb,
                "meta": {"score": it.get("aesthetic_score"), "date": it.get("date"),
                         "place": it.get("place"), "persons": it.get("persons"),
                         "moment": (it.get("moment") or {}).get("title"),
                         "labels": (it.get("labels") or [])[:6],
                         "aspect": asp, "portrait": (asp is not None and asp < 1.0)},
            })
    if args.segments:
        d = json.loads(Path(args.segments).expanduser().read_text(encoding="utf-8"))
        for i, s in enumerate(d.get("segments", [])):
            frame = frame_from_video(Path(s["path"]), workdir / f"segframe_{i}.jpg")
            asp = _aspect(s.get("width"), s.get("height"), s.get("orientation"))
            cands.append({
                "path": s["path"], "type": "video", "image": str(frame) if frame else None,
                "meta": {"scene": s.get("scene"), "why": s.get("reason"),
                         "score": s.get("score"), "duration": s.get("duration"),
                         "date": s.get("date"), "place": s.get("place"),
                         "labels": (s.get("labels") or [])[:6],
                         "aspect": asp, "portrait": (asp is not None and asp < 1.0)},
            })
    return cands


def make_client():
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key), "dev"
    project = _env.vertex_project()
    if not project:
        return None, None
    # Gemini 3.x is served from the global endpoint.
    region = _env.vertex_location()
    return genai.Client(vertexai=True, project=project, location=region), "vertex"


def main() -> int:
    ap = argparse.ArgumentParser(description="Gemini director: review media, return an edit plan.")
    ap.add_argument("--from-select", help="select_photos.py JSON (stills + metadata).")
    ap.add_argument("--segments", help="clip_videos.py JSON (trimmed video segments).")
    ap.add_argument("--duration", type=float, help="Fix the reel length (s). Omit to let the director choose within the range.")
    ap.add_argument("--min-duration", type=float, default=30, help="Min length when the director chooses (s).")
    ap.add_argument("--max-duration", type=float, default=60, help="Max length when the director chooses (s).")
    ap.add_argument("--brief", default="", help="Extra creative direction (tone/audience/hook).")
    ap.add_argument("--model", default=os.environ.get("PHOTO_MONTAGE_GEMINI_MODEL", "gemini-3.5-flash"))
    ap.add_argument("--plan-out", help="Write the edit plan JSON here.")
    ap.add_argument("--strict-chronological", action=argparse.BooleanOptionalAction, default=True,
                    help="Force the final timeline into chronological order by capture date "
                         "(default on). Use --no-strict-chronological to let the model's order stand.")
    ap.add_argument("--skip-existing", action="store_true",
                    help="If --plan-out already exists, reuse it instead of re-calling Gemini (resume).")
    args = ap.parse_args()

    if args.skip_existing and args.plan_out and Path(args.plan_out).expanduser().is_file():
        print(Path(args.plan_out).expanduser().read_text(encoding="utf-8"))
        return 0

    try:
        from google.genai import types
    except Exception:
        print(json.dumps({"error": "google-genai not available"})); return 2

    client, mode = make_client()
    if client is None:
        print(json.dumps({"error": "no_auth",
                          "message": "Set GEMINI_API_KEY or PHOTO_MONTAGE_VERTEX_PROJECT (ADC)."}))
        return 3

    import tempfile
    with tempfile.TemporaryDirectory(prefix="planedit_") as tmp:
        workdir = Path(tmp)
        cands = load_candidates(args, workdir)
        if not cands:
            print(json.dumps({"error": "no candidates"})); return 2

        # Fixed duration pins the range; otherwise the director chooses in-range.
        if args.duration:
            dur_min = dur_max = int(args.duration)
        else:
            dur_min, dur_max = int(args.min_duration), int(args.max_duration)
        contents: list[Any] = ["Candidate media for the reel:"]
        for i, c in enumerate(cands):
            contents.append(f"[{i}] type={c['type']} meta={json.dumps(c['meta'], default=str)}")
            img = c.get("image")
            if img and Path(img).is_file():
                data = Path(img).read_bytes()
                contents.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
        instr = INSTR.format(dur_min=dur_min, dur_max=dur_max)
        if args.brief:
            instr += f"\n\nCREATIVE BRIEF (weight heavily): {args.brief}"
        contents.append(instr)

        try:
            resp = client.models.generate_content(
                model=args.model, contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
        except Exception as e:
            print(json.dumps({"error": f"gemini plan failed: {type(e).__name__}: {e}"})); return 1

        plan = extract_json(getattr(resp, "text", "") or "")
        if not plan or "shots" not in plan:
            print(json.dumps({"error": "no plan returned", "raw": (getattr(resp, 'text', '') or '')[:500]}))
            return 1

        # Resolve indices -> concrete shots with paths.
        shots = []
        for s in plan["shots"]:
            idx = s.get("index")
            if idx is None or idx < 0 or idx >= len(cands):
                continue
            c = cands[idx]
            motion = s.get("motion", "push_in")
            if c["type"] == "video":
                motion = "none"
            if motion not in MOTIONS:
                motion = "push_in"
            shots.append({
                "path": c["path"], "type": c["type"],
                "hold": float(s.get("hold", 2.5)), "motion": motion,
                "transition": s.get("transition", "cut"), "reason": s.get("reason", ""),
                "date": c["meta"].get("date"),
            })

    # Deterministic safety net: guarantee chronological order regardless of the model's
    # ordering (undated items sort to the end, original order preserved among ties).
    if getattr(args, "strict_chronological", True):
        shots.sort(key=lambda sh: (_ts(sh.get("date")) is None, _ts(sh.get("date")) or 0.0))

    n = len(shots) or 1
    vids = sum(1 for s in shots if s["type"] == "video")
    avg_hold = sum(s["hold"] for s in shots) / n
    still_ratio = 1 - vids / n
    risk = max(0.0, min(1.0, 0.6 * still_ratio + 0.4 * max(0.0, (2.6 - avg_hold) / 2.6)))

    out = {
        "backend": mode,
        "recommended_duration_seconds": plan.get("recommended_duration_seconds"),
        "planned_holds_total": round(sum(s["hold"] for s in shots), 1),
        "slideshow_risk": {"score": round(risk, 2), "still_ratio": round(still_ratio, 2),
                           "avg_hold_s": round(avg_hold, 2), "videos": vids, "shots": n},
        "vibe": plan.get("vibe", ""),
        "music_prompt": plan.get("music_prompt", ""),
        "negative_music_prompt": plan.get("negative_music_prompt", ""),
        "narration": plan.get("narration", ""),
        "shots": shots,
        "candidate_count": len(cands),
        "selected_count": len(shots),
    }
    if args.plan_out:
        Path(args.plan_out).expanduser().write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
