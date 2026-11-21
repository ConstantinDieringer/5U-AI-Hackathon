"""Voice messages to text, locally. The 5U gateway refuses audio input on every model
("Audio input is not available", 21 Nov 2026), so voice offers and voice RFQs are read by a
small Whisper model in this process: no gateway call, no cost, a few seconds per message.

The model is loaded once (Docker bakes it into the image, see Dockerfile) and shared by all
threads; `preload()` warms it at startup so the first voice offer of a round does not pay
the load time."""

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
        io.BytesIO(data), beam_size=1, language="en" if english_only else None
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    if not text:
        raise ValueError("empty transcript")
    return text
