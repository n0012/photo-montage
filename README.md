# photo-montage

**Turn your own Apple Photos into a cinematic, professionally-edited memory reel — with Gemini as the editor — then publish it back to Photos. macOS, on-device, private.**

`photo-montage` is an [agent skill](https://docs.anthropic.com/en/docs/claude-code) (works with Claude Code and other skill-aware AI agents). You describe the reel in plain language — *"make a reel from my 4th of July weekend"* — and the agent drives a local pipeline: it finds your best shots, uses **Gemini** to trim videos to their strongest moments and direct the edit, scores it with AI or your own music, and renders a vertical, social-ready cut for YouTube Shorts / Reels / TikTok.

Your photos never leave your machine. The only outward step is importing the finished reel back into *your own* Photos library.

## What makes it good

- **Gemini clips your videos to the best moment** — no more 40-second raw clips; it finds the laugh, the jump, the reveal.
- **Gemini directs the edit** — reviews every candidate, picks and orders the shots, chooses the ideal length the material earns (it won't pad).
- **Chronological by EXIF** — a true multi-day story arc (the next morning lands after last night).
- **Cinematic finish** — color grade + vignette, smooth continuous motion on stills, cross-dissolves, fades, loudness-normalized audio.
- **AI or your own music** — Google **Lyria** (copyright-clean) or a track from your own library.
- **AI cover card** — a generated title card (Nano Banana), full-frame 9:16.
- **Auto self-review** — a Gemini critic flags oddball/duplicate/blurry shots and pacing problems *before* you watch it.
- **Private by design** — reads your library read-only; nothing is uploaded.

## How it works

```
preflight → select (your library) → clip videos (Gemini) → consolidate
→ director/storyboard (Gemini) → music → EXIF order → cover → build (cinematic)
→ self-review → publish back to Photos
```

Each step is a small, self-contained `uv run` script in `scripts/`; the agent orchestrates them and checks the storyboard with you before rendering. See [`SKILL.md`](SKILL.md) for the full agent instructions.

## Requirements

- **macOS** with the Apple Photos app
- [`uv`](https://docs.astral.sh/uv/) · `ffmpeg` (`brew install ffmpeg`) · `sips` (built-in)
- Terminal **Full Disk Access** (read the library) and, to publish, **Automation → Photos**
- An AI backend (either one):
  - a **Gemini API key** (`GEMINI_API_KEY`, free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)), **or**
  - **Vertex AI** via `gcloud auth application-default login`

## Install

Clone into your agent's skills directory (Claude Code example):

```bash
git clone https://github.com/n0012/photo-montage.git ~/.claude/skills/photo-montage
```

Then configure (all optional — see [`.env.example`](.env.example)):

```bash
export GEMINI_API_KEY=...              # or use gcloud ADC
export PHOTO_MONTAGE_MUSIC_DIR=~/Music # a folder of DRM-free tracks (optional)
export PHOTO_MONTAGE_ALBUM="Montages"  # default publish album (optional)
```

## Use

Ask your agent, e.g.:

> "Make a 60–90s cinematic reel from my Lake Tahoe trip and put it in Photos."

The agent proposes a **storyboard** for your sign-off, then renders and publishes.

You can also run steps directly:

```bash
uv run scripts/preflight.py --pool 60
uv run scripts/select_photos.py --output-dir projects/tahoe --from-date 2026-08-01 --to-date 2026-08-05 --download-missing
# … clip → consolidate → plan_edit → build_reel → publish_photos
```

## Privacy & credits

On-device by design; media is read-only and never uploaded. Cover cards and generated music are new AI assets — real photos are never generatively altered. Uses Google Gemini / Lyria / Nano Banana (via the Gemini API or Vertex AI), [osxphotos](https://github.com/RhetTbull/osxphotos), and FFmpeg.

## License

[MIT](LICENSE)
