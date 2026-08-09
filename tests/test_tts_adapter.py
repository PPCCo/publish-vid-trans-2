"""Phase 4 tests: the TTS subprocess adapter's provider resolution and graceful degradation.

No TTS engine is installed in test; these assert the adapter *chooses* correctly and raises
EngineUnavailableError rather than crashing — the same posture as the ASR adapter.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house.engines import tts  # noqa: E402
from video_translation_house.errors import (  # noqa: E402
    EngineUnavailableError,
    VideoTranslationHouseError,
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    return root


def test_known_providers_and_binaries_aligned():
    for provider in tts.KNOWN_PROVIDERS:
        assert provider in tts._PROVIDER_BINARY


def test_unknown_provider_rejected(repo: Path):
    with pytest.raises(VideoTranslationHouseError) as exc:
        tts._resolve_provider("nope-tts", "en", root=repo)
    assert "Unknown TTS provider" in str(exc.value)


def test_requested_but_uninstalled_raises_engine_unavailable(repo: Path, monkeypatch):
    monkeypatch.setattr(tts, "available_tts_providers", lambda *a, **k: [])
    with pytest.raises(EngineUnavailableError) as exc:
        tts._resolve_provider("piper", "fa", root=repo)
    assert "not on PATH" in str(exc.value)


def test_no_engine_installed_raises_engine_unavailable(repo: Path, monkeypatch):
    monkeypatch.setattr(tts, "available_tts_providers", lambda *a, **k: [])
    with pytest.raises(EngineUnavailableError) as exc:
        tts._resolve_provider(None, "fa", root=repo)
    assert "No TTS engine installed" in str(exc.value)


def test_per_language_config_default_chosen_when_installed(repo: Path, monkeypatch):
    # tools.default.json maps fa -> piper. Pretend piper (and xtts) are installed.
    monkeypatch.setattr(tts, "available_tts_providers", lambda *a, **k: ["xtts", "piper"])
    assert tts._resolve_provider(None, "fa", root=repo) == "piper"
    # en -> xtts per config
    assert tts._resolve_provider(None, "en", root=repo) == "xtts"


def test_falls_back_to_first_available_when_config_provider_missing(repo: Path, monkeypatch):
    # fa's configured provider is piper; only xtts installed -> first available.
    monkeypatch.setattr(tts, "available_tts_providers", lambda *a, **k: ["xtts"])
    assert tts._resolve_provider(None, "fa", root=repo) == "xtts"


def test_synthesize_cue_without_engine_degrades(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tts, "available_tts_providers", lambda *a, **k: [])
    with pytest.raises(EngineUnavailableError):
        tts.synthesize_cue("hello", tmp_path / "out.wav", language="en", root=repo)
