# photo-montage

**Turn your own Apple Photos into a cinematic, professionally-edited memory reel — with Gemini as the editor — then publish it back to Photos. macOS · local-first · no public uploads.**

`photo-montage` is an [agent skill](https://docs.anthropic.com/en/docs/claude-code) (works with Claude Code and other skill-aware AI agents). You describe the reel in plain language — *"make a reel from my 4th of July weekend"* — and the agent drives a mostly-local pipeline: it finds your best shots, uses **Gemini** to trim videos to their strongest moments and direct the edit, scores it with AI or your own music, and renders a vertical, social-ready cut for YouTube Shorts / Reels / TikTok.

**Where your media goes:** selection, culling, rendering, and the publish-back all run **locally on your Mac**. The AI steps (clip-trimming, the director, the self-review) send **downscaled proxies, thumbnails, and sampled frames** to Google **Gemini** — via your Gemini API key, or your own Google Cloud **Vertex** project if you'd rather keep processing in your own tenancy. Full-res originals and the finished render stay local, and nothing is ever posted publicly — the only "publish" is back into *your own* Photos library.

![Apple Photos → Curate · Plan · Edit · Compose · Publish → back to Photos](docs/workflow.png)

## What makes it good

- **Gemini clips your videos to the best moment** — no more 40-second raw clips; it finds the laugh, the jump, the reveal.
- **Gemini directs the edit** — reviews every candidate, picks and orders the shots, chooses the ideal length the material earns (it won't pad).
- **Chronological by EXIF** — a true multi-day story arc (the next morning lands after last night).
- **Cinematic finish** — color grade + vignette, smooth continuous motion on stills, cross-dissolves, fades, loudness-normalized audio.
- **AI or your own music** — Google **Lyria** (copyright-clean) or a track from your own library.
- **AI cover card** — a generated title card (Nano Banana), full-frame 9:16.
- **Auto self-review** — a Gemini critic flags oddball/duplicate/blurry shots and pacing problems *before* you watch it.
- **Local-first, no public uploads** — read-only on your library; editing/rendering happen on your Mac; only downscaled proxies/thumbnails go to Gemini for the AI steps (your key, or your own Vertex project).

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

## Inspiration & credits

This project stands on two shoulders:

- **[Co-Director](https://co-director-agent.github.io/)** — a Google research project on *agentic generative video storytelling*, where a multi-agent system works like a film crew with a built-in auditor that catches inconsistencies **before** rendering (I was part of the Co-Director team). `photo-montage` brings that ethos — an agent that *directs* rather than *concatenates*, and reviews its own cut — down to everyday life: your Apple Photos, on your Mac.
- **[OpenMontage](https://github.com/calesthio/OpenMontage)** — an open-source, agentic video-production system. Several patterns here were inspired by it: the director + **self-review** pass, a **slideshow-risk** score, and single-**workspace consolidation** so no candidate gets stranded.

Built on Google **Gemini / Lyria / Nano Banana** (via the Gemini API or Vertex AI), [osxphotos](https://github.com/RhetTbull/osxphotos), and **FFmpeg**.

## Privacy

Local-first, not fully offline. Your library is read **read-only**; selection, rendering, and the publish-back happen **on your Mac**, and nothing is ever posted publicly (the only "publish" is into your own Photos library). The AI steps do send data to Google: **downscaled video proxies, still thumbnails, and sampled frames** go to **Gemini** for clipping, directing, and self-review. You choose the channel — a **Gemini API key**, or your own **Vertex AI / Google Cloud project** to keep processing in your own tenancy (review Google's data-use terms for whichever you pick). Full-res originals and the final render stay local. Cover cards and generated music are new AI assets — your real photos are never generatively altered.

## License

[MIT](LICENSE)
