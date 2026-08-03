"""Engine adapters (ASR / MT / TTS).

Every adapter here is *optional* and *opt-in*. Adapters shell out to the engine's own
CLI in a subprocess — the deterministic CLI process itself never imports torch, mlx,
whisper, or any heavyweight ML dependency (a hard rule from ANALYSIS.md D). When the
requested engine is not installed, adapters raise ``EngineUnavailableError`` so callers
degrade gracefully to a human step rather than crash.
"""
from __future__ import annotations

from .asr import ASRResult, TranscriptCue, available_asr_providers, transcribe

__all__ = [
    "ASRResult",
    "TranscriptCue",
    "available_asr_providers",
    "transcribe",
]
