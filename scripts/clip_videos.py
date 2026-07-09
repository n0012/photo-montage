# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.0.0", "google-cloud-storage>=2.10"]
# ///
"""Intelligent video clipper — trim clips to their best moments with Gemini.

For each source video: downscale to a small proxy, send it to Gemini
(multimodal video understanding), get the 1-2 strongest segments as JSON with
start/end timestamps + reason + a scene description, then ffmpeg-trim the
ORIGINAL at those timestamps for full quality.

This is what makes the montage video-forward instead of a slideshow.

Auth (auto-selected):
- If GEMINI_API_KEY is set -> Gemini Developer API (Files API, simplest).
- Else -> Vertex AI via your gcloud ADC (no new key). Because Vertex has no
  Files API, the proxy is staged to a scratch GCS bucket and referenced by
  gs:// URI, then deleted. Set --project / --gcs-bucket or rely on
  PHOTO_MONTAGE_VERTEX_PROJECT + an auto-named bucket.

Degrades gracefully: no key AND no usable Vertex project -> note + exit 3.

Input: select_photos.py JSON (type==video items) or --videos a.mp4 ...
Output: list of trimmed segments with provenance/reason/score/scene.
"""

from __future__ import annotations

import argparse
import json
import os
import _env
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

PROMPT = """You are a video editor selecting footage for a short, tasteful memory montage.
Watch this clip and pick the {n} BEST moment(s) to use — the emotionally strongest,
most visually interesting, in-focus, well-composed seconds. Avoid shaky starts,
blurry frames, dead air, and moments where nothing happens.

Return ONLY valid JSON (no markdown, no prose) in this exact shape:
{{"segments": [{{"start_seconds": <number>, "end_seconds": <number>,
  "reason": "<why this moment>", "score": <0.0-1.0 quality>}}],
  "scene": "<one short phrase describing the clip, e.g. 'kids running through a sprinkler'>"}}

Rules:
- Each segment 1.5 to 6 seconds long.
- start_seconds/end_seconds are within the clip's duration.
- Order segments best-first.
- Keep at most {n} segments; fewer is fine if the clip only has one good moment."""


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)], timeout=60)
    try:
        return float((p.stdout or "0").strip())
    except ValueError:
        return 0.0


def make_proxy(src: Path, dst: Path, height: int) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    return run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                "-vf", f"scale=-2:{height}", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "30", str(dst)]).returncode == 0 and dst.exists()


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


def trim(src: Path, out: Path, start: float, end: float) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.5, end - start)
    return run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(src),
                "-t", f"{dur:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-movflags", "+faststart", str(out)]).returncode == 0 and out.exists()


def gather_videos(args) -> list[Path]:
    if args.videos:
        return [Path(v).expanduser() for v in args.videos]
    data = json.loads(Path(args.from_select).expanduser().read_text(encoding="utf-8"))
    items = data.get("items", data) if isinstance(data, dict) else data
    return [Path(it["path"]) for it in items
            if isinstance(it, dict) and it.get("type") == "video" and it.get("path")]


# ---- Auth backends ---------------------------------------------------------

class DevBackend:
    """Gemini Developer API — uses the Files API to upload local proxies."""
    def __init__(self, api_key: str, model: str):
        from google import genai
        self.genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def video_part(self, proxy: Path):
        from google.genai import types
        f = self.client.files.upload(file=str(proxy))
        for _ in range(60):
            info = self.client.files.get(name=f.name)
            state = getattr(getattr(info, "state", None), "name", None) or getattr(info, "state", None)
            if str(state).upper().endswith("ACTIVE"):
                break
            if str(state).upper().endswith("FAILED"):
                raise RuntimeError("Files API processing failed")
            time.sleep(2)
        self._last = f
        return types.Part.from_uri(file_uri=f.uri, mime_type=f.mime_type)

    def cleanup(self):
        try:
            self.client.files.delete(name=self._last.name)
        except Exception:
            pass


class VertexBackend:
    """Vertex AI — stages proxies to GCS (no Files API on Vertex) and cleans up.

    The genai model endpoint and the GCS bucket use DIFFERENT locations:
    Gemini 3.x is served from the `global` endpoint, but a bucket must be
    regional. So model_location defaults to global, bucket_region to us-central1.
    """
    def __init__(self, project: str, model_location: str, bucket_region: str,
                 bucket_name: str, model: str):
        from google import genai
        from google.cloud import storage
        self.client = genai.Client(vertexai=True, project=project, location=model_location)
        self.model = model
        self.storage = storage.Client(project=project)
        self.bucket = self._ensure_bucket(bucket_name, bucket_region)
        self._blob = None

    def _ensure_bucket(self, name: str, region: str):
        try:
            return self.storage.get_bucket(name)
        except Exception:
            loc = "us-central1" if region in ("global", "", None) else region
            return self.storage.create_bucket(name, location=loc)

    def video_part(self, proxy: Path):
        from google.genai import types
        blob = self.bucket.blob(f"photo-montage/{proxy.name}")
        blob.upload_from_filename(str(proxy), content_type="video/mp4")
        self._blob = blob
        uri = f"gs://{self.bucket.name}/{blob.name}"
        return types.Part.from_uri(file_uri=uri, mime_type="video/mp4")

    def cleanup(self):
        try:
            if self._blob:
                self._blob.delete()
        except Exception:
            pass


def analyze(backend, proxy: Path, duration: float, max_segments: int) -> Optional[dict]:
    from google.genai import types
    part = backend.video_part(proxy)
    resp = backend.client.models.generate_content(
        model=backend.model,
        contents=[part, PROMPT.format(n=max_segments)],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    backend.cleanup()
    data = extract_json(getattr(resp, "text", "") or "")
    if not data or "segments" not in data:
        return None
    for seg in data["segments"]:
        seg["start_seconds"] = max(0.0, float(seg.get("start_seconds", 0)))
        seg["end_seconds"] = min(duration, float(seg.get("end_seconds", duration)))
    return data


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Trim videos to their best moments with Gemini.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-select", help="select_photos.py JSON (uses type=video items).")
    src.add_argument("--videos", nargs="*", help="Explicit video paths.")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--model", default=os.environ.get("PHOTO_MONTAGE_GEMINI_MODEL", "gemini-3.5-flash"), help="Gemini model (verify current id).")
    ap.add_argument("--max-segments", type=int, default=2)
    ap.add_argument("--proxy-height", type=int, default=480)
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--manifest-out")
    # Vertex path options
    ap.add_argument("--project", help="GCP project (default: PHOTO_MONTAGE_VERTEX_PROJECT).")
    ap.add_argument("--region", default=os.environ.get("CLOUD_ML_REGION", "us-central1"))
    ap.add_argument("--gcs-bucket", help="Scratch bucket for Vertex staging (auto-named if omitted).")
    return ap.parse_args()


def make_backend(args):
    """Prefer a Gemini key; otherwise fall back to Vertex ADC."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return DevBackend(api_key, args.model), "dev"
    project = args.project or _env.vertex_project()
    if not project:
        return None, None
    # Gemini 3.x lives on the `global` endpoint; the GCS staging bucket is regional.
    model_location = _env.vertex_location()
    bucket_region = args.region if args.region and args.region != "global" else "us-central1"
    bucket = args.gcs_bucket or f"{project}-photo-montage-scratch"
    return VertexBackend(project, model_location, bucket_region, bucket, args.model), "vertex"


def main() -> int:
    args = parse_args()
    try:
        backend, mode = make_backend(args)
    except Exception as e:
        print(json.dumps({"error": f"backend init failed: {type(e).__name__}: {e}", "segments": []}))
        return 2
    if backend is None:
        print(json.dumps({
            "error": "no_auth",
            "message": "Set GEMINI_API_KEY, or provide a Vertex project via --project / "
                       "PHOTO_MONTAGE_VERTEX_PROJECT (uses your gcloud ADC). "
                       "Without either, use whole clips or stills only.",
            "segments": [],
        }, indent=2))
        return 3

    videos = [v for v in gather_videos(args) if v.exists()]
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    proxies_dir = output_dir / "_proxies"

    segments: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for vid in videos:
        duration = probe_duration(vid)
        proxy = proxies_dir / (vid.stem + ".mp4")
        if not make_proxy(vid, proxy, args.proxy_height):
            errors.append({"source": str(vid), "error": "proxy failed"})
            continue
        try:
            data = analyze(backend, proxy, duration, args.max_segments)
        except Exception as e:
            errors.append({"source": str(vid), "error": f"{type(e).__name__}: {e}"})
            continue
        if not data:
            errors.append({"source": str(vid), "error": "no segments returned"})
            continue
        scene = data.get("scene", "")
        for i, seg in enumerate(data["segments"][: args.max_segments]):
            score = float(seg.get("score", 0.0))
            if score < args.min_score:
                continue
            start, end = seg["start_seconds"], seg["end_seconds"]
            if end - start < 0.5:
                continue
            out = output_dir / f"{vid.stem}_seg{i:02d}.mp4"
            if not trim(vid, out, start, end):
                errors.append({"source": str(vid), "error": f"trim failed @ {start}-{end}"})
                continue
            segments.append({
                "source": str(vid), "path": str(out),
                "start": round(start, 2), "end": round(end, 2), "duration": round(end - start, 2),
                "reason": seg.get("reason", ""), "score": score, "scene": scene, "type": "video",
            })

    try:
        for f in proxies_dir.glob("*"):
            f.unlink()
        proxies_dir.rmdir()
    except OSError:
        pass

    result = {"backend": mode, "segments": segments, "clips_analyzed": len(videos),
              "segments_kept": len(segments), "errors": errors[:50]}
    if args.manifest_out:
        Path(args.manifest_out).expanduser().write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
