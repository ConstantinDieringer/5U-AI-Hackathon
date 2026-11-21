"""Existing local Whisper recognizer, reused by audio_local's bounded worker.

The image bakes in the model; the active audio chain runs this in a child process so a
timeout can terminate inference. Direct callers may still share _load()/preload(), but
the mail process no longer keeps a second unused model while Groq is primary.
"""

from __future__ import annotations

import io
import logging
import threading

from app.config import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None


def _load():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(
                settings.speech_model,
                device="cpu",
                compute_type="int8",
                cpu_threads=2,
                download_root=settings.speech_model_dir or None,
                # baked into the image: never reach out to the model hub at runtime
                local_files_only=bool(settings.speech_model_dir),
            )
            log.info("speech model %s loaded", settings.speech_model)
    return _model


def preload() -> None:
    """Load the model in the background; a failure is logged, the next call retries."""

    def run() -> None:
        try:
            _load()
        except Exception:  # voice offers then show as unreadable
            log.exception("speech model %s could not be loaded", settings.speech_model)

    threading.Thread(target=run, name="speech-preload", daemon=True).start()


def transcribe(data: bytes) -> str:
    """Plain text of one audio file (mp3/wav/...); raises on an empty transcript."""
    english_only = settings.speech_model.endswith(".en")
    segments, _ = _load().transcribe(
        io.BytesIO(data),
        beam_size=1,
        language="en" if english_only else None,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    if not text:
        raise ValueError("empty transcript")
    return text
