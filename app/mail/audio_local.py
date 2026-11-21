"""Offline CPU worker; killed by the parent at its deadline, never downloads at runtime.

Prepare the model at image build time: python -m app.mail.audio_local base --download
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace


def main() -> None:
    from app.mail import speech

    name = sys.argv[1] if len(sys.argv) > 1 else speech.settings.speech_model
    downloading = "--download" in sys.argv[2:]
    if downloading:
        from faster_whisper import WhisperModel

        WhisperModel(
            name,
            device="cpu",
            compute_type="int8",
            download_root=speech.settings.speech_model_dir or None,
        )
        return
    # Reuse the existing recognizer in a killable process, not a second resident model.
    os.environ["HF_HUB_OFFLINE"] = "1"
    speech.settings = replace(speech.settings, speech_model=name)
    transcript = speech.transcribe(sys.stdin.buffer.read())
    sys.stdout.buffer.write(transcript.encode("utf-8"))


if __name__ == "__main__":
    main()
