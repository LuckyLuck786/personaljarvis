"""Voice interface tests. The audio backends are optional and not installed
in CI, so these verify honest capability reporting + the fallback paths and
the hub round-trip — never faking a model that isn't there."""

import httpx
import pytest

from jarvis.interfaces.voice.loop import VoiceCapabilities, VoiceLoop
from jarvis.interfaces.voice.stt import WhisperSTT
from jarvis.interfaces.voice.tts import PiperTTS
from jarvis.interfaces.voice.wakeword import WakeWord


def test_capabilities_report_is_truthful():
    """Whatever is / isn't installed, the report reflects reality — no faking."""
    loop = VoiceLoop("http://127.0.0.1:8700", "k")
    caps = loop.capabilities()
    assert caps.stt == WhisperSTT().available
    assert caps.wakeword == (WakeWord().available and caps.mic)


def test_tts_unavailable_without_model():
    # no voice model path → not available, and speak() is a truthful no-op
    tts = PiperTTS(voice_model=None)
    assert tts.available is False
    assert tts.synthesize("hello", "/tmp/none.wav") is None
    assert tts.speak("hello", "/tmp/none.wav") is False


def test_capabilities_summary_marks_fallbacks():
    caps = VoiceCapabilities(mic=False, wakeword=False, stt=False, tts=False)
    s = caps.summary()
    assert "off (fallback)" in s
    caps2 = VoiceCapabilities(mic=True, wakeword=True, stt=True, tts=True)
    assert "on" in caps2.summary() and "fallback" not in caps2.summary()


def test_ask_round_trips_to_hub(monkeypatch):
    loop = VoiceLoop("http://hub:8700", "secret-key")
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"reply": "At your service, sir."}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    reply = loop.ask("what's on my plate today?")
    assert reply == "At your service, sir."
    assert captured["url"] == "http://hub:8700/chat"
    assert captured["headers"]["x-api-key"] == "secret-key"  # authenticated like everything else
    assert captured["json"]["session"] == "voice"


def test_ask_handles_hub_unreachable(monkeypatch):
    loop = VoiceLoop("http://hub:8700", "k")

    def fake_post(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", fake_post)
    reply = loop.ask("hello")
    assert "couldn't reach the hub" in reply.lower()  # honest failure, not a crash


def test_stt_transcribe_requires_backend():
    stt = WhisperSTT()
    if not stt.available:
        with pytest.raises(ImportError):
            stt._ensure()
