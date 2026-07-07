"""Speech-to-text via faster-whisper. Optional dependency; if not installed,
`available` is False and the caller falls back to push-to-talk typed input.
"""

from __future__ import annotations

from jarvis.core.logging import get_logger

log = get_logger(__name__)


class WhisperSTT:
    def __init__(self, model_size: str = "base.en", device: str = "auto",
                 compute_type: str = "int8"):
        self.model_size = model_size
        self._model = None
        self._device = device
        self._compute_type = compute_type

    @property
    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            # int8 keeps this light; on the MacBook it can use metal/cpu fine
            self._model = WhisperModel(self.model_size, device=self._device,
                                       compute_type=self._compute_type)
            log.info("whisper_loaded", model=self.model_size)
        return self._model

    def transcribe(self, audio) -> str:
        """audio: float32 numpy array at 16 kHz mono, or a path to a wav."""
        model = self._ensure()
        segments, _ = model.transcribe(audio, language="en", beam_size=1)
        return " ".join(seg.text for seg in segments).strip()
