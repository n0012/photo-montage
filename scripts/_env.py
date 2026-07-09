"""Shared, shareable configuration for the photo-montage skill — all via env vars.

No user-specifics are hardcoded. Every knob is an environment variable with a
sensible fallback (Vertex project falls back to your gcloud ADC default, so the
skill works with zero setup once `gcloud auth application-default login` is done).

Env vars:
  GEMINI_API_KEY                 Optional. If set, uses the Gemini Developer API;
                                 otherwise Vertex via ADC.
  PHOTO_MONTAGE_VERTEX_PROJECT   GCP project for Vertex. Falls back to
                                 GOOGLE_CLOUD_PROJECT, then ANTHROPIC_VERTEX_PROJECT_ID
                                 (back-compat), then the ADC default project.
  VERTEX_MODEL_LOCATION          Vertex endpoint for Gemini (default: global —
                                 required for Gemini 3.x).
  PHOTO_MONTAGE_GEMINI_MODEL     Director/clipper model (default: gemini-3.5-flash).
  PHOTO_MONTAGE_ALBUM            Default Photos album to publish into (default: Montages).
  PHOTO_MONTAGE_SHARED_ALBUMS    Comma-separated shared iCloud album names to also pull.
  PHOTO_MONTAGE_MUSIC_DIR        Local/mounted music library dir the agent picks tracks from.
"""

from __future__ import annotations

import os


def vertex_project() -> str:
    for k in ("PHOTO_MONTAGE_VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT",
              "ANTHROPIC_VERTEX_PROJECT_ID"):
        v = os.environ.get(k)
        if v:
            return v
    # Fall back to the ADC default project so no config is needed.
    try:
        import google.auth
        _, proj = google.auth.default()
        return proj or ""
    except Exception:
        return ""


def vertex_location() -> str:
    return os.environ.get("VERTEX_MODEL_LOCATION") or "global"


def gemini_model() -> str:
    return os.environ.get("PHOTO_MONTAGE_GEMINI_MODEL") or "gemini-3.5-flash"


def default_album() -> str:
    return os.environ.get("PHOTO_MONTAGE_ALBUM") or "Montages"


def shared_albums() -> list[str]:
    return [s.strip() for s in os.environ.get("PHOTO_MONTAGE_SHARED_ALBUMS", "").split(",") if s.strip()]


def music_dir() -> str:
    return os.environ.get("PHOTO_MONTAGE_MUSIC_DIR", "")
