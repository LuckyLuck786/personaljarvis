"""Wake-word detection via openWakeWord. Optional; when unavailable the
voice loop uses push-to-talk (Enter to record) instead, announced honestly.
"""

from __future__ import annotations

from jarvis.core.logging import get_logger

log = get_logger(__name__)


class WakeWord:
    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.5):
        self.model_name = model
        self.threshold = threshold
        self._model = None

    @property
    def available(self) -> bool:
        try:
            import openwakeword  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure(self):
        if self._model is None:
            from openwakeword.model import Model

            self._model = Model(wakeword_models=[self.model_name])
            log.info("wakeword_loaded", model=self.model_name)
        return self._model

    def detect(self, audio_frame) -> bool:
        """audio_frame: 16 kHz int16 mono chunk. True if the wake word fired."""
        model = self._ensure()
        scores = model.predict(audio_frame)
        return any(s >= self.threshold for s in scores.values())
