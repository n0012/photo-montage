# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build a tasteful, social-ready montage reel from a curated, ordered manifest.

Assembles stills (gentle alternating Ken Burns) and trimmed video segments into
a vertical (or landscape/square) reel with:
  - a uniform color grade (warm / cinematic / neutral),
  - beat-synced shot holds (from analyze_audio.py output),
  - subject-aware crops for reframing (from smart_crop.py output),
  - a music bed and/or ducked voiceover,
  - loudness normalization + faststart for YouTube Shorts / Reels / TikTok.

Pure ffmpeg (+ macOS sips for HEIC) — deterministic, no AI, no cloud. The AI
pieces (clip selection, music, narration) are produced by sibling scripts and
handed in as files here.

Manifest (JSON) — from select_photos.py / clip_videos.py, a list of paths, or:
    [{"path": "a.jpg", "hold": 3.0, "motion": "push_in"},
     {"path": "seg01.mp4", "type": "video"}]
motion ∈ {push_in, pull_out, drift_left, drift_right, none}.

Example:
    uv run build_reel.py --manifest order.json --output reel.mp4 --aspect vertical \
        --grade warm --beats-file beats.json --crop-map crops.json \
        --music music.wav --voiceover vo.wav --loudnorm
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

ASPECTS = {"vertical": (1080, 1920), "landscape": (1920, 1080), "square": (1080, 1080)}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
MOTIONS = ["push_in", "pull_out", "drift_left", "drift_right", "none"]

# Uniform color grades (ffmpeg filter fragments applied to every clip).
GRADES = {
    "none": "",
    "neutral": "eq=saturation=1.03:contrast=1.03",
    "warm": "eq=saturation=1.12:contrast=1.05:gamma=1.02,"
            "colorbalance=rs=0.03:rm=0.02:bs=-0.03:bm=-0.02",
    "cinematic": "eq=saturation=0.94:contrast=1.12,"
                 "colorbalance=rs=-0.04:bs=0.05:rh=0.05:bh=-0.04",
}


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sips_to_jpeg(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    return run(["sips", "-s", "format", "jpeg", str(src), "--out", str(dst)], timeout=60).returncode == 0 and dst.exists()


def probe_duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)], timeout=60)
    try:
        return float((p.stdout or "0").strip())
    except ValueError:
        return 0.0


def grade_suffix(grade_filter: str) -> str:
    return ("," + grade_filter) if grade_filter else ""


def kenburns_filter(w: int, h: int, dur: float, fps: int, motion: str, crop: Optional[dict],
                    grade_filter: str, zoom_rate: float = 0.0012, zoom_max: float = 1.12) -> str:
    frames = max(1, int(round(dur * fps)))
    pre = ""
    if crop:
        pre = f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']},"
    big_w, big_h = int(w * 1.5), int(h * 1.5)
    base = pre + f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
    if motion == "push_in":
        zp = f"zoompan=z='min(zoom+{zoom_rate},{zoom_max})':d={frames}:s={w}x{h}:fps={fps}"
    elif motion == "pull_out":
        zp = f"zoompan=z='if(eq(on,0),{zoom_max},max(zoom-{zoom_rate},1.0))':d={frames}:s={w}x{h}:fps={fps}"
    elif motion == "drift_left":
        zp = f"zoompan=z=1.1:x='iw*0.10*(1-on/{frames})':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={fps}"
    elif motion == "drift_right":
        zp = f"zoompan=z=1.1:x='iw*0.10*(on/{frames})':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={fps}"
    else:
        zp = f"zoompan=z=1.0:d={frames}:s={w}x{h}:fps={fps}"
    return base + zp + ",setsar=1" + grade_suffix(grade_filter)


def make_still_clip(img: Path, out: Path, w: int, h: int, dur: float, fps: int,
                    motion: str, crop: Optional[dict], grade_filter: str, workdir: Path,
                    zoom_rate: float = 0.0012, zoom_max: float = 1.12) -> bool:
    src = img
    if img.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        norm = workdir / (img.stem + ".jpg")
        if sips_to_jpeg(img, norm):
            src = norm
    vf = kenburns_filter(w, h, dur, fps, motion, crop, grade_filter, zoom_rate, zoom_max)
    # -noautorotate: ffmpeg otherwise auto-rotates stills from the EXIF Orientation tag.
    # osxphotos already bakes rotation into the exported pixels but can leave a STALE tag
    # (e.g. a correctly-oriented landscape shot tagged orientation-8), so honoring the tag
    # rotates a correct image 90° sideways. We trust the raw pixels — matching smart_crop,
    # which reads raw via cv2 — so crop coords and render stay in the same space. (This
    # flag is image-only; make_video_clip keeps autorotate, since video rotation metadata
    # from phones is legitimate.)
    return run(["ffmpeg", "-y", "-loglevel", "error", "-noautorotate", "-loop", "1", "-i", str(src),
                "-t", f"{dur:.3f}", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", str(fps), str(out)]).returncode == 0 and out.exists()


def make_video_clip(vid: Path, out: Path, w: int, h: int, dur: Optional[float], fps: int, grade_filter: str) -> bool:
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps}" + grade_suffix(grade_filter)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(vid)]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vf", vf, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), str(out)]
    return run(cmd).returncode == 0 and out.exists()


def load_manifest(args) -> list[dict[str, Any]]:
    if args.manifest:
        data = json.loads(Path(args.manifest).expanduser().read_text(encoding="utf-8"))
        if isinstance(data, dict) and "shots" in data:
            data = data["shots"]  # plan_edit.py output (already {path,hold,motion,type})
        elif isinstance(data, dict) and "items" in data:
            data = [{"path": it["path"], "type": it.get("type")} for it in data["items"]]
        elif isinstance(data, dict) and "segments" in data:
            data = [{"path": s["path"], "type": "video"} for s in data["segments"]]
        norm = []
        for entry in data:
            if isinstance(entry, str):
                norm.append({"path": entry})
            elif isinstance(entry, dict) and entry.get("path"):
                norm.append(entry)
        return norm
    media = Path(args.media_dir).expanduser()
    files = sorted([p for p in media.iterdir() if p.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS])
    return [{"path": str(f)} for f in files]


def compute_holds(items: list[dict], beats: list[float], default_hold: float, min_hold: float = 0.8) -> list[float]:
    """Snap each shot's cut boundary to a musical beat when beats are provided.

    Video segments keep their own duration; stills get beat-aligned holds.
    """
    holds: list[float] = []
    if not beats:
        for it in items:
            holds.append(float(it.get("hold") or default_hold))
        return holds
    beats = sorted(beats)
    cum = 0.0
    for it in items:
        want = float(it.get("hold") or default_hold)
        target = cum + want
        # candidate beats past a minimum hold from the current cut point
        cands = [b for b in beats if b > cum + min_hold]
        if not cands:
            hold = want
        else:
            beat = min(cands, key=lambda b: abs(b - target))
            hold = max(min_hold, beat - cum)
        holds.append(hold)
        cum += hold
    return holds


def concat_dissolve(clips: list[Path], D: float, out: Path, fps: int) -> bool:
    """Concatenate clips with an xfade cross-dissolve of D seconds between each."""
    if len(clips) == 1:
        clips[0].replace(out)
        return out.exists()
    durs = [probe_duration(c) for c in clips]
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    parts: list[str] = []
    prev = "[0:v]"
    cum = durs[0]
    for j in range(1, len(clips)):
        off = max(0.0, cum - D)
        lbl = f"[v{j}]"
        parts.append(f"{prev}[{j}:v]xfade=transition=fade:duration={D}:offset={off:.3f}{lbl}")
        prev = lbl
        cum = cum + durs[j] - D
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + \
          ["-filter_complex", ";".join(parts), "-map", prev,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
           "-movflags", "+faststart", str(out)]
    return run(cmd, timeout=600).returncode == 0 and out.exists()


def build_audio(video_dur: float, music: Optional[Path], vo: Optional[Path],
                vo_start: float, fade_out: float, loudnorm: bool, out: Path) -> bool:
    """Assemble the final audio track: music bed, optional ducked voiceover."""
    if not music and not vo:
        return False
    inputs: list[str] = []
    parts: list[str] = []
    idx = 0
    music_lbl = vo_lbl = None
    if music:
        inputs += ["-i", str(music)]
        st = max(0.0, video_dur - fade_out)
        parts.append(f"[{idx}:a]atrim=0:{video_dur:.2f},afade=t=out:st={st:.2f}:d={fade_out:.2f}[m0]")
        music_lbl = "[m0]"
        idx += 1
    if vo:
        inputs += ["-i", str(vo)]
        delay = int(vo_start * 1000)
        # Split the delayed VO: one copy padded to full length to drive the
        # sidechain (so ducking runs the whole reel, not just while VO plays),
        # one copy (natural length) mixed in as the actual narration.
        parts.append(f"[{idx}:a]adelay={delay}|{delay},asplit=2[vosc][vomix]")
        parts.append(f"[vosc]apad,atrim=0:{video_dur:.2f}[vopad]")
        vo_lbl = "[vomix]"
        idx += 1

    if music_lbl and vo_lbl:
        parts.append(f"{music_lbl}[vopad]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=300[mduck]")
        parts.append(f"[mduck]{vo_lbl}amix=inputs=2:duration=first:dropout_transition=0[mix]")
    elif music_lbl:
        parts.append(f"{music_lbl}anull[mix]")
    else:
        parts.append(f"{vo_lbl}anull[mix]")

    final = "[mix]"
    if loudnorm:
        parts.append("[mix]loudnorm=I=-14:TP=-1.5:LRA=11[out]")
        final = "[out]"

    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + \
          ["-filter_complex", ";".join(parts), "-map", final,
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)]
    return run(cmd).returncode == 0 and out.exists()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build a social-ready montage reel via ffmpeg.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", help="JSON: paths, {path,hold,motion,type}, or select/clip output.")
    src.add_argument("--media-dir", help="Directory of media in filename order.")
    ap.add_argument("--output", required=True)
    ap.add_argument("--aspect", choices=list(ASPECTS), default="vertical")
    ap.add_argument("--seconds-per-shot", type=float, default=2.5)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--grade", choices=list(GRADES), default="warm")
    ap.add_argument("--beats-file", help="analyze_audio.py JSON to beat-sync shot holds.")
    ap.add_argument("--crop-map", help="smart_crop.py JSON of subject-aware crops.")
    ap.add_argument("--music", help="Music bed (from make_music.py or a library track).")
    ap.add_argument("--voiceover", help="Voiceover WAV (from make_voiceover.py).")
    ap.add_argument("--vo-start", type=float, default=1.0, help="Voiceover start offset (s).")
    ap.add_argument("--music-fade-out", type=float, default=1.5)
    ap.add_argument("--loudnorm", action="store_true", help="Normalize loudness (~-14 LUFS) for social.")
    ap.add_argument("--fade-out", type=float, default=1.0, help="Video fade-to-black seconds at end (0=off).")
    ap.add_argument("--fade-in", type=float, default=0.5, help="Video fade-from-black seconds at start (0=off).")
    ap.add_argument("--dissolve", type=float, default=0.4,
                    help="Cross-dissolve seconds between shots via xfade (0=hard cuts).")
    ap.add_argument("--hold-scale", type=float, default=1.0,
                    help="Multiply every shot's hold for slower/more cinematic pacing (e.g. 1.3).")
    ap.add_argument("--vignette", action="store_true", help="Add a subtle vignette (cinematic).")
    ap.add_argument("--cinematic-motion", action="store_true",
                    help="Smooth continuous slow push across each still's full hold.")
    ap.add_argument("--skip-existing", action="store_true",
                    help="If --output already exists, skip rebuilding (resume).")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.skip_existing and Path(args.output).expanduser().is_file():
        print(json.dumps({"output": str(Path(args.output).expanduser()), "skipped_existing": True}))
        return 0
    w, h = ASPECTS[args.aspect]
    items = load_manifest(args)
    if not items:
        print(json.dumps({"error": "no media in manifest/media-dir"}))
        return 2

    beats = []
    if args.beats_file:
        try:
            beats = json.loads(Path(args.beats_file).expanduser().read_text(encoding="utf-8")).get("beats", [])
        except Exception:
            beats = []
    crop_map = {}
    if args.crop_map:
        try:
            crop_map = json.loads(Path(args.crop_map).expanduser().read_text(encoding="utf-8"))
        except Exception:
            crop_map = {}

    # Cinematic pacing: scale up every shot's target hold before beat-snapping.
    if args.hold_scale and args.hold_scale != 1.0:
        for it in items:
            if it.get("hold"):
                it["hold"] = float(it["hold"]) * args.hold_scale
        args.seconds_per_shot *= args.hold_scale

    # Uniform grade + optional vignette applied to every clip.
    grade_filter = ",".join(x for x in [GRADES.get(args.grade, ""),
                                        "vignette=PI/5" if args.vignette else ""] if x)

    holds = compute_holds(items, beats, args.seconds_per_shot)
    out_path = Path(args.output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="reel_") as tmp:
        workdir = Path(tmp)
        clips_dir = workdir / "clips"
        clips_dir.mkdir()
        concat_lines: list[str] = []
        clip_paths: list[Path] = []
        made = 0
        for i, entry in enumerate(items):
            src = Path(entry["path"]).expanduser()
            if not src.exists():
                continue
            hold = holds[i]
            clip = clips_dir / f"{i:03d}.mp4"
            is_video = (entry.get("type") == "video") or (src.suffix.lower() in VIDEO_EXTS)
            if is_video:
                ok = make_video_clip(src, clip, w, h, hold, args.fps, grade_filter)
            else:
                motion = entry.get("motion") or MOTIONS[i % 4]
                if motion not in MOTIONS:
                    motion = "push_in"
                crop = crop_map.get(str(src)) or crop_map.get(src.name)
                # Cinematic motion: a single smooth push spanning the whole hold.
                if args.cinematic_motion and motion in ("push_in", "pull_out", "none"):
                    zr = max(0.0004, 0.10 / max(1, hold * args.fps))
                    ok = make_still_clip(src, clip, w, h, hold, args.fps,
                                         "push_in" if motion == "none" else motion,
                                         crop, grade_filter, workdir, zoom_rate=zr, zoom_max=1.12)
                else:
                    ok = make_still_clip(src, clip, w, h, hold, args.fps, motion, crop, grade_filter, workdir)
            if ok:
                concat_lines.append(f"file '{clip}'")
                clip_paths.append(clip)
                made += 1

        if made == 0:
            print(json.dumps({"error": "no clips could be built"}))
            return 2

        silent = workdir / "silent.mp4"
        if args.dissolve > 0 and len(clip_paths) > 1:
            ok = concat_dissolve(clip_paths, args.dissolve, silent, args.fps)
            if not ok:  # fall back to hard-cut concat if xfade chain fails
                args.dissolve = 0
        if args.dissolve <= 0 or not silent.exists():
            concat_file = workdir / "concat.txt"
            concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
            p = run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                     "-i", str(concat_file), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-movflags", "+faststart", str(silent)])
            if p.returncode != 0 or not silent.exists():
                print(json.dumps({"error": f"concat failed: {p.stderr[-300:]}"}))
                return 2

        video_dur = probe_duration(silent)

        # Video fade in/out (baked before audio mux, which copies the video stream).
        if args.fade_in > 0 or args.fade_out > 0:
            vf_parts = []
            if args.fade_in > 0:
                vf_parts.append(f"fade=t=in:st=0:d={args.fade_in}")
            if args.fade_out > 0:
                vf_parts.append(f"fade=t=out:st={max(0, video_dur - args.fade_out):.2f}:d={args.fade_out}")
            faded = workdir / "faded.mp4"
            pf = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent),
                      "-vf", ",".join(vf_parts), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                      "-r", str(args.fps), "-movflags", "+faststart", str(faded)])
            if pf.returncode == 0 and faded.exists():
                silent = faded
        music = Path(args.music).expanduser() if args.music else None
        vo = Path(args.voiceover).expanduser() if args.voiceover else None
        if music and not music.is_file():
            print(json.dumps({"error": f"music not found: {music}"})); return 2
        if vo and not vo.is_file():
            print(json.dumps({"error": f"voiceover not found: {vo}"})); return 2

        if music or vo:
            audio = workdir / "audio.m4a"
            if not build_audio(video_dur, music, vo, args.vo_start, args.music_fade_out, args.loudnorm, audio):
                print(json.dumps({"error": "audio assembly failed"})); return 2
            p = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(audio),
                     "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy",
                     "-shortest", "-movflags", "+faststart", str(out_path)])
            if p.returncode != 0 or not out_path.exists():
                print(json.dumps({"error": f"mux failed: {p.stderr[-300:]}"})); return 2
        else:
            silent.replace(out_path)

    dur = probe_duration(out_path)
    print(json.dumps({
        "output": str(out_path), "shots": made, "duration_seconds": round(dur, 2),
        "resolution": f"{w}x{h}", "aspect": args.aspect, "grade": args.grade,
        "beat_synced": bool(beats), "smart_crop": bool(crop_map),
        "music": bool(args.music), "voiceover": bool(args.voiceover), "loudnorm": args.loudnorm,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
