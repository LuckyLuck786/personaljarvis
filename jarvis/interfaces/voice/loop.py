"""Voice loop orchestrator.

Full path (all extras installed + mic):
    wake word "Jarvis" → record until silence → Whisper STT → hub /chat →
    Piper TTS → speak the reply.

Degraded paths, each announced honestly:
    * no mic / no sounddevice → push-to-talk: type your utterance, spoken reply
    * no wake word           → push-to-talk trigger (Enter), rest of pipeline intact
    * no STT                 → typed input
    * no TTS                 → text reply printed, not spoken

The loop calls the hub's authenticated /chat, so all cognition/memory/tools
happen server-side; this process only does audio I/O. Runs on the MacBook.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import httpx

from jarvis.core.logging import get_logger
from jarvis.interfaces.voice.stt import WhisperSTT
from jarvis.interfaces.voice.tts import PiperTTS
from jarvis.interfaces.voice.wakeword import WakeWord

log = get_logger(__name__)

SAMPLE_RATE = 16000


@dataclass
class VoiceCapabilities:
    mic: bool
    wakeword: bool
    stt: bool
    tts: bool

    def summary(self) -> str:
        def mark(b):
            return "on" if b else "off (fallback)"
        return (f"mic={mark(self.mic)} wakeword={mark(self.wakeword)} "
                f"stt={mark(self.stt)} tts={mark(self.tts)}")


def _mic_available() -> bool:
    try:
        import sounddevice  # noqa: F401
        return True
    except (ImportError, OSError):
        return False


class VoiceLoop:
    def __init__(self, hub_url: str, api_key: str, tts_voice: str | None = None,
                 whisper_model: str = "base.en"):
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key
        self.wakeword = WakeWord()
        self.stt = WhisperSTT(whisper_model)
        self.tts = PiperTTS(tts_voice)

    def capabilities(self) -> VoiceCapabilities:
        return VoiceCapabilities(
            mic=_mic_available(),
            wakeword=self.wakeword.available and _mic_available(),
            stt=self.stt.available,
            tts=self.tts.available,
        )

    # -- hub round-trip --------------------------------------------------------

    def ask(self, text: str) -> str:
        try:
            resp = httpx.post(
                f"{self.hub_url}/chat",
                json={"text": text, "session": "voice"},
                headers={"x-api-key": self.api_key}, timeout=300,
            )
            resp.raise_for_status()
            return resp.json()["reply"]
        except httpx.HTTPError as exc:
            log.warning("voice_chat_failed", error=type(exc).__name__)
            return "I couldn't reach the hub just now, sir."

    def respond(self, text: str) -> None:
        print(f"JARVIS: {text}")
        if self.tts.available:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
                self.tts.speak(text, f.name)

    # -- record helpers --------------------------------------------------------

    def _record_until_silence(self, max_s: float = 8.0, silence_s: float = 1.0):
        """Record from the mic, stopping after a pause. Returns float32 @16k."""
        import numpy as np
        import sounddevice as sd

        block = int(SAMPLE_RATE * 0.1)
        collected, silent = [], 0.0
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
            for _ in range(int(max_s / 0.1)):
                data, _ = stream.read(block)
                collected.append(data[:, 0].copy())
                if float(np.abs(data).mean()) < 0.01:
                    silent += 0.1
                    if silent >= silence_s and len(collected) > 5:
                        break
                else:
                    silent = 0.0
        return np.concatenate(collected)

    # -- run -------------------------------------------------------------------

    def run(self) -> None:
        caps = self.capabilities()
        print(f"Voice loop up. Capabilities: {caps.summary()}")
        if not caps.mic:
            print("No microphone/sounddevice — push-to-talk (type, spoken reply "
                  "if TTS is on). Ctrl-C to quit.")
            self._run_push_to_talk(caps)
        elif not caps.wakeword or not caps.stt:
            print("Wake word or STT unavailable — press Enter to record, or type. "
                  "Ctrl-C to quit.")
            self._run_push_to_talk(caps)
        else:
            print('Say "Jarvis" to wake me. Ctrl-C to quit.')
            self._run_wakeword(caps)

    def _run_push_to_talk(self, caps: VoiceCapabilities) -> None:
        while True:
            try:
                typed = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not typed and caps.mic and caps.stt:
                print("(recording — speak now)")
                audio = self._record_until_silence()
                typed = self.stt.transcribe(audio)
                print(f"you (heard)> {typed}")
            if typed:
                self.respond(self.ask(typed))

    def _run_wakeword(self, caps: VoiceCapabilities) -> None:  # pragma: no cover - needs mic
        import numpy as np
        import sounddevice as sd

        block = int(SAMPLE_RATE * 0.08)
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as stream:
            while True:
                try:
                    data, _ = stream.read(block)
                    if self.wakeword.detect(np.frombuffer(data, dtype=np.int16)):
                        print("(woke — listening)")
                        audio = self._record_until_silence()
                        text = self.stt.transcribe(audio)
                        if text:
                            print(f"you> {text}")
                            self.respond(self.ask(text))
                except KeyboardInterrupt:
                    print()
                    return


def run_voice(hub_url: str, api_key: str, tts_voice: str | None = None) -> None:
    if not api_key:
        sys.exit("JARVIS_API_KEY not set")
    VoiceLoop(hub_url, api_key, tts_voice=tts_voice).run()
