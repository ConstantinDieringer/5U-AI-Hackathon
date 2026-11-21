"""Offline CPU worker; killed by the parent at its deadline, never downloads at runtime.

Prepare the model at image build time: python -m app.mail.audio_local base --download
"""

from __future__ import annotations

import io
import sys


def main() -> None:
    from faster_whisper import WhisperModel

    name = sys.argv[1] if len(sys.argv) > 1 else "base"
    downloading = "--download" in sys.argv[2:]
    model = WhisperModel(
        name,
        device="cpu",
        compute_type="int8",
        cpu_threads=2,
        local_files_only=not downloading,
    )
    if downloading:
        return
    segments, _ = model.transcribe(
        io.BytesIO(sys.stdin.buffer.read()),
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    transcript = " ".join(s.text.strip() for s in segments).strip()
    if not transcript:
        raise ValueError("empty transcript")
    sys.stdout.buffer.write(transcript.encode("utf-8"))


if __name__ == "__main__":
    main()
