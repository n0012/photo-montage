---
name: photo-montage
description: "Create a tasteful, cinematic montage reel from your own Apple Photos — Gemini clips videos to their best moments and directs the edit, with AI music, chronological ordering, cross-dissolves/fades, color grade + vignette, and a generated cover card — sized for YouTube Shorts / Reels / TikTok, then published back into Photos. Use when asked to make a montage, memory reel, highlight video, recap, or short from photos/videos of an event, trip, weekend, person, or date range (e.g. 'make a reel from my 4th of July weekend'). macOS + Apple Photos only; local-first (AI steps send downscaled proxies/thumbnails to Gemini; nothing posted publicly)."
version: 2.6.0
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
  `PHOTO_MONTAGE_GEMINI_MODEL` (default `gemini-3.5-flash`, used by the flash steps:
  clip-trim + review). Director overrides: `PHOTO_MONTAGE_DIRECTOR_MODEL` (default
  `gemini-3.1-pro-preview`) and `PHOTO_MONTAGE_DIRECTOR_FALLBACK` (default
  `gemini-2.5-flash`, used on PRO 429/error).
- `PHOTO_MONTAGE_ALBUM` (default publish album), `PHOTO_MONTAGE_SHARED_ALBUMS`
  (comma-separated shared iCloud albums to also pull), `PHOTO_MONTAGE_MUSIC_DIR`
  (a local/mounted folder of DRM-free tracks).

## Scripts (all `uv run`, self-contained inline deps)
Live in `scripts/`. `ffmpeg` + `sips` (macOS) on PATH. `scripts/_env.py` holds
the shared env config.

| Script | Does | Auth |
|--------|------|------|
| `preflight.py` | Check ffmpeg/osxphotos/Photos + probe Gemini model/endpoint; **recommend model** (pro for big pools) | local + probe |
| `select_photos.py` | Query library (dates/albums/persons/**shared albums**), rank by Apple aesthetic score, cull screenshots/bursts, HEIC→JPG (EXIF-orientation-safe thumbnails via sips), `--download-missing` (PhotoKit, **default on**) → JSON + thumbnails | local |
| `clip_videos.py` | **Gemini** → best segment(s) per video, ffmpeg-trims originals | key / ADC |
| `consolidate.py` | Merge **all** photo+segment manifests in a workspace → one deduped pool | local |
| `plan_edit.py` | **Gemini "director"** → edit plan (selection, holds, motion, transitions, **chosen duration**, vibe, music prompt, narration, slideshow-risk); uses EXIF date/place/**labels**/aspect; enforces **chronological order** (`--strict-chronological`, default on); **defaults to PRO** (`gemini-3.1-pro-preview`, auto flash fallback); names the **`arc`** and flags **`missing_beats`** (e.g. a build with no reveal shot) | key / ADC |
| `make_titlecard.py` | **Nano Banana** cover card, full-frame 9:16 (no crop) | key / ADC |
| `make_music.py` | **Lyria** text-to-music, or fit a library track | ADC |
| `analyze_audio.py` | librosa beats (optional; omit for cinematic) | local |
| `smart_crop.py` | Subject-aware crop for vertical: frontal+profile **face**, **upper-body**, then **detail-saliency** fallback, with headroom | local |
| `make_voiceover.py` | Narration WAV (optional): Gemini TTS / Cloud TTS | key / ADC |
| `build_reel.py` | Assemble → grade, vignette, cinematic motion, dissolves, fades, music/VO, loudnorm | local |
| `review_reel.py` | **Auto self-review**: Gemini critiques sampled frames (off-story/dupe/blurry, pacing, ending, **rotated/sideways**) + slideshow-risk; **stays flash on purpose** — an independent critic shouldn't share the PRO director's blind spots (`--model` to raise for a hero cut) | key / ADC |
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
(or set `PHOTO_MONTAGE_SHARED_ALBUMS`) for family shots. **`--download-missing` is
ON by default** (PhotoKit) so iCloud-only originals get pulled — this matters most
for **videos**, since the payoff/reveal footage is often iCloud-only and was
silently skipped before, leaving the director blind to it. Use
`--no-download-missing` only to restrict to already-local originals (fast, offline).
Pull photos and videos in **separate passes**. There's no label-based "oddball"
cull — the PRO director decides what's on-story from the images + brief (a fixed label
heuristic can't tell clutter from a subject the reel is actually about), and the review
pass flags anything genuinely off-story.

### 2. Clip every video
`clip_videos.py --from-select videos.json --output-dir clips/`.

### 3. Consolidate → director → storyboard (sign off BEFORE rendering)
`consolidate.py --workspace projects/<event>` merges ALL candidates into one
deduped pool. Then `plan_edit.py --from-select all_photos.json --segments
all_segments.json --min-duration 60 --max-duration 90 --brief "<tone/arc>"` →
Gemini picks shots/holds/motion/transitions, the **ideal duration**, a
`music_prompt`, `narration`, and a slideshow-risk score. **Present the
storyboard** and adjust before building.

**Model strategy — PRO where it thinks, flash where it checks.** The director is
the one step that *looks at every candidate image and reasons about the whole
story at once*, so it defaults to **PRO** (`gemini-3.1-pro-preview`; one call per
reel, auto-falls back to flash on 429). It returns an **`arc`** (the story it
built) and **`missing_beats`** — always read these: if `missing_beats` names a gap
like *"build has no finished-result/reveal shot"*, the reel can't land its payoff
until you supply that shot. Don't just report it — **go get the footage**: widen the
date range, pull **videos** (a reveal/payoff is usually a motion clip, often
iCloud-only), `clip_videos` them, and re-run the director. Only fall back to a weaker
closer if the shot genuinely doesn't exist. The
review pass deliberately **stays on flash** so the critic is an *independent* model
from the author (catches blind spots a same-model reviewer would rubber-stamp).
Clip-trim/crop stay flash (mechanical). Override any step with `--model`.

### 4. Music
Prefer a local/mounted library at `PHOTO_MONTAGE_MUSIC_DIR` — pick a DRM-free
track (`.mp3/.m4a/.flac`; skip `.m4p`) that fits the vibe. For a full-length
song, run it through `make_music.py --library-track <file> --duration <reel_s>`:
it analyzes the track and lifts its **hook/chorus** (the strongest ~reel-length
window, snapped to a nearby onset) and fits it to length, so the *recognizable*
part lands on the reel instead of a slow intro — then pass that bed to
`build_reel --music`. (Force a spot with `--music-start <s>`, or keep the top
with `--from-start`. Handing a raw song straight to `build_reel --music` just
uses it from 0:00.) No library? `make_music.py --prompt "<mood>"` (Lyria,
copyright-clean). **Lyria blocks named IP** — a prompt naming a specific
film, artist, or track 400-errors with *"Music generation failed"*. Describe the
**sound** instead (mood, instruments, tempo, era), not a known work. **Make the track
≥ the reel length** (pass `--duration` a few seconds over the planned reel) — a short
track leaves the tail silent. *Copyright:* a commercial track is fine for a personal/Photos
reel; for social it may be muted — export music-free + add in-app, or use Lyria.

### 5. Order chronologically by EXIF, pin the finale
`plan_edit.py` now enforces this automatically: every candidate — stills **and
clips** — carries EXIF capture time, and `--strict-chronological` (default ON)
stable-sorts the final timeline by capture date, so multi-day arcs come out right
(the next-morning boat trip lands *after* the prior night's fireworks, not
before). It also feeds the director EXIF **place + Apple scene `labels`** to group
scenes/pick establishing shots, and **orientation → aspect/portrait** to prefer
full-frame portrait shots for the 9:16 reel. Use `--no-strict-chronological` only
for a deliberately non-linear cut. Place the chosen closer last (the user may name
it, e.g. a wide lake/mountain vista); the cover is prepended in step 6.

**Mind the closer's last frame:** when the finale is a video, the *end* of the clip —
not its opening — is the last thing the viewer is left with. Check the closer's end
frame, and pick/trim the segment (and match `--fade-out`) so it lands on an *intentional*
final beat rather than a weak or accidental one. What "strong" means depends on the reel
(a calm held moment or an active one) — the point is to choose it deliberately.

### 6. Cover LAST, from the finished story
First **look at the selected photos** to read the reel's *real* setting — the
specific place/landscape/season you actually see (mountains vs. beach vs. city,
golden hour, snow, etc.) — and put THAT in `--style` so the card matches the
trip, not a generic template. `make_titlecard.py --text "<title for the whole
event>" --subtitle "<place · date>" --style "<the real setting you saw>"` —
full-frame 9:16, no crop. Build `--style` from the photos, whatever the event:
e.g. *snow-capped peaks at golden hour* for a mountain trip, *palm-lined beach at
sunset* for the shore, *string lights over a backyard* for a birthday, *city
skyline at dusk* for a weekend away. This card becomes the reel's **opening
frame** (§7 `--fade-in 0`), i.e. the social thumbnail — so make it strong.

### 7. Build — cinematic recipe
```
build_reel.py --manifest order.json --output reel.mp4 --aspect vertical \
  --grade cinematic --vignette --cinematic-motion \
  --dissolve 0.6 --fade-in 0 --fade-out 2.5 \
  --crop-map crops.json --music track.mp3 --loudnorm
```
Cinematic: drop `--beats-file` (let holds breathe), `--dissolve 0.5–0.7`,
`--cinematic-motion`, `--grade cinematic --vignette`, long `--fade-out`;
`--hold-scale 1.2+` slows further. Punchy/social: add `--beats-file`, shorter
dissolves.

**Open on the cover, not black:** when the first shot is the title card, keep
`--fade-in 0`. Social platforms (YouTube/Reels/TikTok) grab the **first frame**
as the thumbnail — a fade-from-black posts as a blank/black thumbnail, so let the
splash cover be frame one. (Fade-*out* to black at the end is fine.)

### 8. Self-review, then deliver
`review_reel.py --reel reel.mp4 --order order.json` → act on `verdict:"fix"`
(drop flagged off-story/dupe/blurry shots, fix any rotated shot, adjust pacing) and rebuild before
showing the user. **When the critic and the director disagree — trust the critic**,
especially on the ending and off-story shots: it's an independent model looking at the
*actual rendered frames*, so it catches what the author talked itself into (e.g. a
charming but weak/dark closing shot the director rationalized). Note the reviewer samples
frames mid-render, so "distorted overlay" flags landing on a cross-dissolve are sampling
artifacts, not defects.
Then `publish_photos.py --video reel.mp4 --album "<event>"`
(defaults to `PHOTO_MONTAGE_ALBUM`), keep a durable copy, and write a
`caption.txt` (caption + 3–8 hashtags) for social.

## The FINAL pass: consolidate EVERYTHING
Before the final director run, `consolidate.py` sweeps the whole workspace so a
shot clipped in one sub-batch never gets stranded (dedups photos by `(date,w,h)`,
segments by source). Use
`gemini-3.1-pro-preview` for big pools (flash 429s). (Gotcha: `/tmp` is a
symlink — `find /tmp …` returns nothing; `consolidate.py` walks dirs directly.)

## Platform specs
9:16 vertical 1080×1920, 30fps, AAC 48kHz, `+faststart`, ~-14 LUFS. Length
sweet spot 30–90s.

## Gotchas (learned the hard way)
- **EXIF orientation — trust the pixels, IGNORE the tag.** osxphotos exports
  already-upright pixels but can leave a **stale EXIF Orientation tag** (e.g. a
  correct-landscape shot tagged orientation-8). Tools that *honor* the tag then rotate
  a correct image 90° sideways. So everything in the render path must ignore it:
  `build_reel` passes ffmpeg **`-noautorotate`** on stills, `smart_crop` reads with
  **`cv2.IMREAD_IGNORE_ORIENTATION`**, and `select_photos` makes thumbnails with
  **`sips`** (honors EXIF correctly for the *director's* view). Do NOT "fix" a sideways
  shot by applying `exif_transpose`/honoring the tag — that double-rotates it, and the
  rotated crop dims can overflow the raw frame so the shot silently drops from the reel.
  Keep `smart_crop` and `build_reel` in the **same pixel space** (both raw) or crops
  won't line up. Videos are different — `make_video_clip` keeps autorotate, since phone
  video rotation metadata is legitimate. The `review_reel` **orientation flag** is the
  backstop for anything that still slips through (it sees the final render).
- **The director is blind to what you don't pull.** It only considers exported,
  local candidates. iCloud-only originals (default-pulled now) and un-clipped videos
  are invisible to it — a missing reveal is usually a missing *pull*, not a bad edit.
- See also: `Lyria blocks named IP` (§4), music-length ≥ reel (§4), `/tmp` symlink
  (consolidate section), author-vs-critic (§8).

## Guardrails
- **Privacy:** local-first, not fully offline. The library is read read-only and
  rendering/publish happen locally, but the AI steps send downscaled
  proxies/thumbnails/frames to Gemini (Gemini API or the user's Vertex project).
  Nothing is posted publicly; the only write is the import back into the user's
  own library. Be accurate about this — don't claim "100% on-device."
- **Taste > score:** Gemini/aesthetic score is input, not verdict — enforce
  variety, chronology, a real story. No auto-dump.
- **Keep it real:** cover cards / music are fine (new assets); do NOT
  style-transfer or generatively alter real photos.
- **Cull oddballs by story, not by label:** the director drops off-story shots
  using the images + brief (no fixed label/"oddball" heuristic — it can't tell clutter
  from a subject the reel is actually about); the self-review pass catches any that slip
  through, judging relevance against the reel's subject.
- **Music-led:** voiceover only when asked, and short.

## Requirements
macOS + Apple Photos; `uv`; `ffmpeg`; Full Disk Access (read) + Automation→Photos
(publish); a `GEMINI_API_KEY` or gcloud ADC (Vertex). See `.env.example`.
