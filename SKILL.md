---
name: photo-montage
description: "Create a tasteful, cinematic montage reel from your own Apple Photos — Gemini clips videos to their best moments and directs the edit, with AI music, chronological ordering, cross-dissolves/fades, color grade + vignette, and a generated cover card — sized for YouTube Shorts / Reels / TikTok, then published back into Photos. Use when asked to make a montage, memory reel, highlight video, recap, or short from photos/videos of an event, trip, weekend, person, or date range (e.g. 'make a reel from my 4th of July weekend'). macOS + Apple Photos only; media stays on-device."
version: 2.4.0
author: nick
---

# Photo Montage — Apple Photos → cinematic reel → back to Photos / social

Turn your own Apple Photos into a memory film that looks professionally edited:
real video clips trimmed to their best moments, stills with slow continuous
motion, a music bed, a chronological story, cross-dissolves + fades, a cinematic
grade, and a generated cover card. Then publish it back into Photos and hand over
a file + caption for Shorts / Reels / TikTok.

**Gemini is the editor; the agent orchestrates + gets sign-off.** Scripts do the
mechanics + AI calls; Gemini selects/orders shots; the agent runs a storyboard
past the user before the heavy render.

## Configuration (env vars — nothing hardcoded)
All user-specifics are environment variables with sensible fallbacks; see
`.env.example`. Key ones:
- **AI:** `GEMINI_API_KEY` (Developer API), *or* just gcloud ADC for Vertex
  (`gcloud auth application-default login`). Project resolves from
  `PHOTO_MONTAGE_VERTEX_PROJECT` → `GOOGLE_CLOUD_PROJECT` → ADC default.
- `VERTEX_MODEL_LOCATION` (default `global` — required for Gemini 3.x),
  `PHOTO_MONTAGE_GEMINI_MODEL` (default `gemini-3.5-flash`).
- `PHOTO_MONTAGE_ALBUM` (default publish album), `PHOTO_MONTAGE_SHARED_ALBUMS`
  (comma-separated shared iCloud albums to also pull), `PHOTO_MONTAGE_MUSIC_DIR`
  (a local/mounted folder of DRM-free tracks).

## Scripts (all `uv run`, self-contained inline deps)
Live in `scripts/`. `ffmpeg` + `sips` (macOS) on PATH. `scripts/_env.py` holds
the shared env config.

| Script | Does | Auth |
|--------|------|------|
| `preflight.py` | Check ffmpeg/osxphotos/Photos + probe Gemini model/endpoint; **recommend model** (pro for big pools) | local + probe |
| `select_photos.py` | Query library (dates/albums/persons/**shared albums**), rank by Apple aesthetic score, cull screenshots/bursts/**mechanical (AC/engine) shots**, HEIC→JPG, `--download-missing` (PhotoKit) → JSON + thumbnails | local |
| `clip_videos.py` | **Gemini** → best segment(s) per video, ffmpeg-trims originals | key / ADC |
| `consolidate.py` | Merge **all** photo+segment manifests in a workspace → one deduped pool | local |
| `plan_edit.py` | **Gemini "director"** → ordered edit plan (selection, holds, motion, transitions, **chosen duration**, vibe, music prompt, narration, slideshow-risk) | key / ADC |
| `make_titlecard.py` | **Nano Banana** cover card, full-frame 9:16 (no crop) | key / ADC |
| `make_music.py` | **Lyria** text-to-music, or fit a library track | ADC |
| `analyze_audio.py` | librosa beats (optional; omit for cinematic) | local |
| `smart_crop.py` | Subject-aware (face) crop for vertical | local |
| `make_voiceover.py` | Narration WAV (optional): Gemini TTS / Cloud TTS | key / ADC |
| `build_reel.py` | Assemble → grade, vignette, cinematic motion, dissolves, fades, music/VO, loudnorm | local |
| `review_reel.py` | **Auto self-review**: Gemini critiques sampled frames (mechanical/dupe/blurry, pacing, ending) + slideshow-risk | key / ADC |
| `publish_photos.py` | Import the reel back into a Photos album (photoscript) | local |

## Workflow

### 0. Preflight + workspace
`preflight.py --pool <N>` → confirm capabilities + get the recommended model
(`gemini-3.1-pro-preview` for pools >~60). Pick a **workspace**:
`projects/<event>/` — send every `--output-dir` under it so nothing scatters.
**Resume:** stage outputs persist; pass `--skip-existing` to
`plan_edit`/`build_reel` (and downloads in `select_photos`) to skip redone work.

### 1. Scope → pull comprehensively
Map the ask to `select_photos.py`: `--from-date/--to-date`, `--albums`,
`--persons`, `--favorites-only`, `--since-days`. Add `--shared-albums "<name>"`
(or set `PHOTO_MONTAGE_SHARED_ALBUMS`) for family shots. On "Optimize Mac
Storage" libraries add **`--download-missing`** (PhotoKit; the AppleScript path
hangs). Pull photos and videos in **separate passes**. Mechanical AC/engine
shots are dropped automatically (`--keep-mechanical` to override).

### 2. Clip every video
`clip_videos.py --from-select videos.json --output-dir clips/`.

### 3. Consolidate → director → storyboard (sign off BEFORE rendering)
`consolidate.py --workspace projects/<event>` merges ALL candidates into one
deduped pool. Then `plan_edit.py --from-select all_photos.json --segments
all_segments.json --min-duration 60 --max-duration 90 --brief "<tone/arc>"` →
Gemini picks shots/holds/motion/transitions, the **ideal duration**, a
`music_prompt`, `narration`, and a slideshow-risk score. **Present the
storyboard** and adjust before building.

### 4. Music
Prefer a local/mounted library at `PHOTO_MONTAGE_MUSIC_DIR` — pick a DRM-free
track (`.mp3/.m4a/.flac`; skip `.m4p`) that fits the vibe and pass it to
`build_reel --music`. (If it's on a NAS, mount the share first.) Fallbacks:
`make_music.py --prompt "<mood>"` (Lyria, copyright-clean) or `--library-track`.
*Copyright:* a commercial track is fine for a personal/Photos reel; for social it
may be muted — export music-free + add in-app, or use Lyria.

### 5. Order chronologically by EXIF, pin the finale
Order selected shots by **EXIF capture time** (parse tz), not the director's
guess — fixes multi-day arcs (next-morning shots land after the prior night).
Place the chosen closer last (the user may name it, e.g. a wide lake/mountain
vista). Prepend the cover.

### 6. Cover LAST, from the finished story
`make_titlecard.py --text "<title for the whole event>" --subtitle "<date>"
--style "<vibe>"` — full-frame 9:16, no crop.

### 7. Build — cinematic recipe
```
build_reel.py --manifest order.json --output reel.mp4 --aspect vertical \
  --grade cinematic --vignette --cinematic-motion \
  --dissolve 0.6 --fade-in 0.6 --fade-out 2.5 \
  --crop-map crops.json --music track.mp3 --loudnorm
```
Cinematic: drop `--beats-file` (let holds breathe), `--dissolve 0.5–0.7`,
`--cinematic-motion`, `--grade cinematic --vignette`, long `--fade-out`;
`--hold-scale 1.2+` slows further. Punchy/social: add `--beats-file`, shorter
dissolves.

### 8. Self-review, then deliver
`review_reel.py --reel reel.mp4 --order order.json` → act on `verdict:"fix"`
(drop flagged mechanical/dupe/blurry shots, adjust pacing) and rebuild before
showing the user. Then `publish_photos.py --video reel.mp4 --album "<event>"`
(defaults to `PHOTO_MONTAGE_ALBUM`), keep a durable copy, and write a
`caption.txt` (caption + 3–8 hashtags) for social.

## The FINAL pass: consolidate EVERYTHING
Before the final director run, `consolidate.py` sweeps the whole workspace so a
shot clipped in one sub-batch never gets stranded (dedups photos by `(date,w,h)`,
segments by source, drops mechanical/non-event clips). Use
`gemini-3.1-pro-preview` for big pools (flash 429s). (Gotcha: `/tmp` is a
symlink — `find /tmp …` returns nothing; `consolidate.py` walks dirs directly.)

## Platform specs
9:16 vertical 1080×1920, 30fps, AAC 48kHz, `+faststart`, ~-14 LUFS. Length
sweet spot 30–90s.

## Guardrails
- **Privacy:** on-device; scripts read the library read-only; the only write is
  the import back into the user's own library. No uploads without explicit OK.
- **Taste > score:** Gemini/aesthetic score is input, not verdict — enforce
  variety, chronology, a real story. No auto-dump.
- **Keep it real:** cover cards / music are fine (new assets); do NOT
  style-transfer or generatively alter real photos.
- **Cull oddballs:** mechanical shots are label-filtered by default; the
  self-review pass catches any that slip through.
- **Music-led:** voiceover only when asked, and short.

## Requirements
macOS + Apple Photos; `uv`; `ffmpeg`; Full Disk Access (read) + Automation→Photos
(publish); a `GEMINI_API_KEY` or gcloud ADC (Vertex). See `.env.example`.
