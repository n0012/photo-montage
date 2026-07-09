# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.0.0"]
# ///
"""Generate a title / cover / end card with Nano Banana (Gemini image model).

A catchy opening card is what makes a family/social reel feel produced. This
generates a 9:16 (or other aspect) card image from a prompt using Gemini's
image model ("Nano Banana"), so build_reel can drop it in as the first (and/or
last) shot. Tasteful use of AI: it creates NEW graphics rather than restyling
real memories.

Auth (auto-selected): GEMINI_API_KEY, else Vertex ADC.

Example:
  uv run make_titlecard.py --text "Lake Days" --subtitle "July 4th 2026" \
      --style "warm golden-hour, mountain lake, hand-lettered, clean" \
      --output titlecard.png
"""

from __future__ import annotations

import argparse
import json
import os
import _env
import subprocess
import sys
from pathlib import Path

ASPECT_HINT = {"vertical": "9:16 vertical", "landscape": "16:9 widescreen", "square": "1:1 square"}
ASPECT_DIMS = {"vertical": (1080, 1920), "landscape": (1920, 1080), "square": (1080, 1080)}


def fit_to_frame(raw: Path, out: Path, w: int, h: int) -> bool:
    """Fit the generated card to exactly WxH with NO cropping: a blurred,
    zoomed copy fills the frame as background, the full sharp card sits on top
    (scaled to fit). Nothing in the design gets cut off."""
    filt = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=24[bg];"
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                        "-filter_complex", filt, "-frames:v", "1", str(out)],
                       capture_output=True, timeout=60)
    return p.returncode == 0 and out.exists()


def make_client():
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key), ["_"]
    project = _env.vertex_project()
    if not project:
        return None, None
    region = _env.vertex_location()
    locs = list(dict.fromkeys([region if region != "global" else "us-central1", "us-central1", "global"]))
    return ("vertex", project, locs), locs


def generate(model: str, prompt: str, out: Path) -> bool:
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    clients = []
    if api_key:
        clients.append(genai.Client(api_key=api_key))
    else:
        project = _env.vertex_project()
        region = _env.vertex_location()
        for loc in dict.fromkeys([region if region != "global" else "us-central1", "us-central1", "global"]):
            clients.append(genai.Client(vertexai=True, project=project, location=loc))

    last = None
    for client in clients:
        try:
            resp = client.models.generate_content(
                model=model, contents=[prompt],
                config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
            )
            for cand in getattr(resp, "candidates", []) or []:
                for part in getattr(cand.content, "parts", []) or []:
                    inline = getattr(part, "inline_data", None)
                    if inline and getattr(inline, "data", None):
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(inline.data)
                        return True
            last = "no image part in response"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            continue
    raise RuntimeError(last or "image generation failed")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a title/cover card with Nano Banana.")
    ap.add_argument("--text", required=True, help="Main title text.")
    ap.add_argument("--subtitle", default="", help="Smaller subtitle (date/place).")
    ap.add_argument("--style", default="warm, clean, modern, tasteful",
                    help="Art direction (palette, motif, lettering).")
    ap.add_argument("--aspect", choices=list(ASPECT_HINT), default="vertical")
    ap.add_argument("--output", required=True)
    ap.add_argument("--model", default="gemini-2.5-flash-image",
                    help="Nano Banana image model id (verify current, e.g. gemini-3.1-flash-image-preview).")
    args = ap.parse_args()

    if make_client()[0] is None:
        print(json.dumps({"error": "no_auth", "message": "Set GEMINI_API_KEY or PHOTO_MONTAGE_VERTEX_PROJECT (ADC)."}))
        return 3

    sub = f' with smaller subtitle text "{args.subtitle}"' if args.subtitle else ""
    prompt = (
        f'Design a {ASPECT_HINT[args.aspect]} title card for a family memories video. '
        f'Large, beautifully hand-lettered title text "{args.text}"{sub}. '
        f'Style: {args.style}. Cohesive composition, generous negative space, '
        f'high-end and social-media-ready, no watermarks, spell the text exactly and correctly.'
    )
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".raw.png")
    try:
        generate(args.model, prompt, raw)
    except Exception as e:
        print(json.dumps({"error": f"titlecard failed: {e}"}))
        return 1
    # Fit to exact aspect with no cropping (blurred-fill background).
    w, h = ASPECT_DIMS[args.aspect]
    if not fit_to_frame(raw, out, w, h):
        raw.replace(out)  # fall back to the raw image if ffmpeg fit fails
    else:
        try:
            raw.unlink()
        except OSError:
            pass
    print(json.dumps({"output": str(out), "resolution": f"{w}x{h}",
                      "text": args.text, "subtitle": args.subtitle, "model": args.model}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
