"""Text-to-speech via Piper. Optional; degrades to a truthful no-op (text
only) when Piper isn't installed. Piper is a small, fast, local neural TTS —
a good fit for the calm-butler voice without cloud round-trips.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

from jarvis.core.logging import get_logger

log = get_logger(__name__)


class PiperTTS:
    def __init__(self, voice_model: str | None = None):
        # path to a downloaded .onnx voice, e.g. en_GB-alan-medium.onnx —
        # British male suits the butler brief
        self.voice_model = voice_model

    @property
    def available(self) -> bool:
        return shutil.which("piper") is not None and bool(self.voice_model) \
            and Path(self.voice_model).exists()

    def synthesize(self, text: str, out_path: str | Path) -> Path | None:
        if not self.available:
            log.info("tts_unavailable_text_only", text=text[:80])
            return None
        out_path = Path(out_path)
        proc = subprocess.run(
            ["piper", "--model", self.voice_model, "--output_file", str(out_path)],
            input=text.encode(), capture_output=True,
        )
        if proc.returncode != 0:
            log.warning("piper_failed", stderr=proc.stderr.decode()[:200])
            return None
        return out_path

    def speak(self, text: str, out_path: str | Path) -> bool:
        wav = self.synthesize(text, out_path)
        if wav is None:
            return False
        # play via afplay (macOS) / aplay (linux) if present
        player = shutil.which("afplay") or shutil.which("aplay")
        if player:
            subprocess.run([player, str(wav)], capture_output=True)
        return True


def wav_duration_s(path: str | Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / float(w.getframerate())
