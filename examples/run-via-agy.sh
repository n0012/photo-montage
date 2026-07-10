#!/usr/bin/env bash
#
# run-via-agy.sh — drive the photo-montage skill headlessly with the
# Antigravity CLI (`agy`). A worked example of using this skill from an agent
# harness instead of an interactive chat.
#
# NOTE: The skill is agent-agnostic — the same SKILL.md runs under Claude Code
# or any Agent-Skills-compatible agent. `agy` is just ONE headless harness shown
# here; nothing about the skill depends on it. Swap in your agent of choice.
#
# Prerequisites:
#   - Antigravity CLI installed + signed in:  curl -fsSL https://antigravity.google/cli/install.sh | bash
#   - This skill on a skills path agy reads:  ~/.gemini/config/skills/photo-montage
#   - macOS + Apple Photos, ffmpeg, uv; a Gemini API key or gcloud ADC (see .env.example)
#
# Usage:
#   ./run-via-agy.sh                       # default: ~40s reel from the last 7 days
#   ./run-via-agy.sh "PROMPT…"             # your own natural-language montage request
#
# Optional env:
#   WORKDIR=/path/to/out                   # where the mp4 + log land (default ~/photo-montage-out)
#   PHOTO_MONTAGE_SHARED_ALBUMS="Family"   # also pull from these shared iCloud albums
#   MONTAGE_MUSIC=/path/to/track.mp3       # score with YOUR OWN track instead of AI (Lyria)
#
# Note: --dangerously-skip-permissions lets agy run the skill's uv/ffmpeg steps
# without prompting. It's your call to enable it; drop it to approve each step.

set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

WORKDIR="${WORKDIR:-$HOME/photo-montage-out}"
mkdir -p "$WORKDIR"; cd "$WORKDIR"
RUNSTAMP=$(date +%Y%m%d-%H%M%S)               # one stamp shared by this run's log + reel
OUT_BASE="${OUT_BASE:-montage}"               # output name prefix
LOG="$WORKDIR/agy-run-$RUNSTAMP.log"

# Music: your own track (MONTAGE_MUSIC) if set, otherwise AI-generated (Lyria).
if [ -n "${MONTAGE_MUSIC:-}" ]; then
  MUSIC_STEP="use MY OWN track as the bed, NOT Lyria: ${MONTAGE_MUSIC} (pass it to build_reel via --music)."
  MUSIC_FLAG=" --music ${MONTAGE_MUSIC}"
else
  MUSIC_STEP="run make_music.py for a fitting bed (Google Lyria is fine)."
  MUSIC_FLAG=""
fi

DEFAULT_PROMPT="Use the photo-montage skill to make a cinematic ~40-second vertical (9:16) montage from my most recent Apple Photos (the last 7 days).
Steps:
1. select_photos.py (add --download-missing if originals are iCloud-only; it also reads PHOTO_MONTAGE_SHARED_ALBUMS for shared iCloud albums).
2. clip_videos.py so Gemini trims each video to its best moment(s).
3. consolidate.py, then plan_edit.py (Gemini director) to pick and order shots into a chronological story (let the director choose the duration, ~40s).
4. MUSIC - ${MUSIC_STEP}
5. COVER - do this LAST, after the story is planned. First LOOK AT several of the selected photos to judge the real setting (place, landscape, season, vibe), then generate the title card with make_titlecard.py so its scene MATCHES this trip - put that setting in --style, with a short --text title and a --subtitle for the place/date. Prepend the finished card as the FIRST shot (type image, ~3.5s hold, gentle push_in) before the final build.
6. build_reel.py cinematic: --grade cinematic --vignette --cinematic-motion --dissolve 0.6 --fade-in 0 --fade-out 2.5 --loudnorm${MUSIC_FLAG}.
Save the final mp4 in this directory and do NOT publish to Photos. Print the ABSOLUTE PATH to the final mp4 on its own line."
PROMPT="${1:-$DEFAULT_PROMPT}"

command -v agy >/dev/null || { echo "✗ agy not found on PATH ($HOME/.local/bin). Install: curl -fsSL https://antigravity.google/cli/install.sh | bash"; exit 1; }

# agy runs ONE session at a time. If one is already going, a second invocation
# exits instantly with an empty log — don't stomp the active run.
if pgrep -f "agy .*--print" >/dev/null 2>&1; then
  echo "✗ an agy session is already running — let it finish before starting another. Aborting."
  exit 1
fi

echo "▶ photo-montage via agy (Antigravity)"
echo "  workdir : $WORKDIR"
echo "  log     : $LOG"
echo "  expect  : several minutes (Gemini clip/direct + render; longer if downloading from iCloud)"
echo

STAMP="$WORKDIR/.montage-start.$$"; : > "$STAMP"   # mark run start; only open videos newer than this

agy --dangerously-skip-permissions --print-timeout 28m --print "$PROMPT" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}

echo
echo "──────────────────────────────────────────────"
echo "agy exit status: $status   |   log: $LOG"
# Only consider a video THIS run produced (newer than the start marker) — never
# open a stale reel from a previous run. agy may save to WORKDIR or its scratch.
mp4=$(find "$WORKDIR" "$HOME/.gemini/antigravity-cli/scratch" -maxdepth 1 -name '*.mp4' -newer "$STAMP" 2>/dev/null \
      | while read -r f; do printf '%s\t%s\n' "$(stat -f %m "$f")" "$f"; done | sort -rn | head -1 | cut -f2-)
rm -f "$STAMP"
if [ ! -s "$LOG" ]; then
  echo "⚠ agy produced NO output (empty log). Another agy session may be running, or it failed to start — nothing regenerated (stale reels left untouched)."
elif [ -n "$mp4" ]; then
  # Save every run under a unique timestamped name so iterations don't overwrite
  # each other and you can compare them side by side.
  dest="$WORKDIR/${OUT_BASE}-$RUNSTAMP.mp4"
  cp "$mp4" "$dest"
  echo "✓ new reel: $dest"
  command -v open >/dev/null && open "$dest"
else
  echo "⚠ agy ran but produced no NEW mp4 this run — check the log above (stale reels left untouched)."
fi
