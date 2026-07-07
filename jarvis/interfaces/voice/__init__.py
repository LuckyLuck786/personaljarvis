"""Voice interface: wake word ("Jarvis") → STT → chat agent → TTS.

Design: the heavy audio models (openWakeWord, faster-whisper, Piper) are
OPTIONAL dependencies. Each component detects whether its backend is
installed and reports capability honestly; nothing here is faked. The voice
loop talks to the hub over the same authenticated HTTP /chat endpoint every
other interface uses, so it runs on the MacBook (where a mic and the STT/TTS
models live) pointing at the hub — the hub itself needs no audio stack.

Install the extras with:  pip install -e ".[voice]"
"""
