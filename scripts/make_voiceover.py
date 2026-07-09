# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.0.0", "google-cloud-texttospeech>=2.16"]
# ///
"""Generate an optional voiceover narration (auto-selects backend).

The agent writes a short narration script from the event context (place, date,
people, and the clip scene descriptions from clip_videos.py); this speaks it and
writes a 24kHz WAV that build_reel.py mixes under the music (with ducking).
Voiceover is opt-in — most memory reels are music-led.

Auth (auto-selected, prefers Gemini-native TTS):
- If GEMINI_API_KEY is set -> Gemini TTS (Developer API).
- Else -> Gemini TTS on Vertex via your gcloud ADC (no key, no GCS; uses the
  already-enabled aiplatform API).
- If Vertex Gemini TTS is unavailable in your project/region -> Cloud
  Text-to-Speech (Chirp3-HD) via ADC as a fallback.

Degrades gracefully: no key AND no ADC -> note + exit 3.

Example:
    uv run make_voiceover.py --text "Summer, 2026." --voice Charon --output vo.wav
"""

from __future__ import annotations

import argparse
import json
import os
import _env
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 24000  # Gemini TTS emits 24kHz, 16-bit, mono PCM.


def write_wav_pcm(pcm: bytes, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def _gemini_generate_audio(client, text: str, voice: str, model: str, out: Path) -> float:
    from google.genai import types
    resp = client.models.generate_content(
        model=model, contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    if not pcm:
        raise RuntimeError("no audio returned")
    write_wav_pcm(pcm, out)
    return round(len(pcm) / (2 * SAMPLE_RATE), 2)


def gemini_tts_dev(text: str, voice: str, model: str, out: Path) -> float:
    """Gemini TTS via the Developer API (GEMINI_API_KEY)."""
    from google import genai
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"])
    return _gemini_generate_audio(client, text, voice, model, out)


def gemini_tts_vertex(text: str, voice: str, model: str, out: Path,
                      project: str, locations: list[str]) -> float:
    """Gemini TTS via Vertex (ADC, no key). Tries locations until one works."""
    from google import genai
    last = None
    for loc in locations:
        try:
            client = genai.Client(vertexai=True, project=project, location=loc)
            return _gemini_generate_audio(client, text, voice, model, out)
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"Vertex Gemini TTS unavailable in {locations}: {last}")


def cloud_tts(text: str, voice: str, language_code: str, out: Path, quota_project: str = "") -> float:
    """Cloud TTS Chirp3-HD via ADC. LINEAR16 output is a complete WAV file."""
    from google.cloud import texttospeech
    import google.auth
    creds, adc_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    project = quota_project or adc_project
    # User ADC needs an explicit quota/consumer project for Cloud TTS.
    if project and hasattr(creds, "with_quota_project"):
        creds = creds.with_quota_project(project)
    client = texttospeech.TextToSpeechClient(credentials=creds)
    # Accept a bare name ("Charon") or a full voice id ("en-US-Chirp3-HD-Charon").
    name = voice if voice.lower().startswith(language_code.lower()) or "chirp" in voice.lower() \
        else f"{language_code}-Chirp3-HD-{voice}"
    resp = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(language_code=language_code, name=name),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=SAMPLE_RATE
        ),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.audio_content)  # LINEAR16 includes a WAV header
    # Duration from PCM payload (header is 44 bytes).
    return round(max(0, len(resp.audio_content) - 44) / (2 * SAMPLE_RATE), 2)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Generate a voiceover (Gemini TTS or Cloud TTS).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--script-file")
    ap.add_argument("--output", required=True)
    ap.add_argument("--voice", default="Charon",
                    help="Voice name (e.g. Charon, Kore, Aoede, Puck). Works for both backends.")
    ap.add_argument("--language-code", default="en-US", help="Cloud TTS language code.")
    ap.add_argument("--model", default="gemini-2.5-flash-preview-tts",
                    help="Gemini TTS model id (verify current id).")
    ap.add_argument("--style", default="", help="Delivery direction for Gemini TTS, e.g. 'warm, unhurried'.")
    ap.add_argument("--project", help="GCP project for Vertex/Cloud TTS (default: PHOTO_MONTAGE_VERTEX_PROJECT).")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    text = args.text if args.text else Path(args.script_file).expanduser().read_text(encoding="utf-8")
    out = Path(args.output).expanduser()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    spoken = f"{args.style.rstrip(':')}: {text}" if args.style else text
    project = args.project or _env.vertex_project()
    region = os.environ.get("CLOUD_ML_REGION", "")
    # Gemini TTS preview models are region-limited; try a few sensible endpoints.
    locations = list(dict.fromkeys([l for l in [region, "global", "us-central1"] if l]))

    backend = model = None
    try:
        if api_key:
            dur = gemini_tts_dev(spoken, args.voice, args.model, out)
            backend, model = "gemini_dev", args.model
        else:
            if not project:
                print(json.dumps({
                    "error": "no_auth",
                    "message": "Set GEMINI_API_KEY, or a Vertex project via --project / "
                               "PHOTO_MONTAGE_VERTEX_PROJECT (uses gcloud ADC). Reel proceeds "
                               "music-only without narration.",
                }, indent=2))
                return 3
            # Prefer Gemini-native TTS on Vertex; fall back to Cloud TTS Chirp3.
            try:
                dur = gemini_tts_vertex(spoken, args.voice, args.model, out, project, locations)
                backend, model = "gemini_vertex", args.model
            except Exception as ge:
                dur = cloud_tts(text, args.voice, args.language_code, out, quota_project=project)
                backend, model = "cloud_tts", f"{args.language_code}-Chirp3-HD"
                print(json.dumps({"note": f"Gemini TTS on Vertex unavailable, used Cloud TTS. ({ge})"}),
                      file=sys.stderr)
    except Exception as e:
        print(json.dumps({"error": f"tts failed: {type(e).__name__}: {e}"}))
        return 1

    print(json.dumps({"output": str(out), "backend": backend, "voice": args.voice,
                      "model": model, "duration_seconds": dur}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
