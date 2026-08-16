"""LangSpec validation (plan §8/R6) — all pure logic against a synthetic company config."""
from __future__ import annotations

import pytest

from lib.langspec import LangSpecError, build_langspec


def _cfg(**over):
    base = {
        "sourceLang": "fa",
        "targetLangs": ["en", "fa", "ar", "es"],
        "dubLangs": ["en", "ar"],
        "closedCaptions": ["en", "fa", "ar", "es"],
        "clone": {"enabled_by_default": True},
    }
    base.update(over)
    return base


def test_source_in_targets_is_dropped_from_translate(synth_root):
    spec = build_langspec(_cfg(), synth_root)
    # fa is the source and appears in targetLangs → not self-translated.
    assert "fa" not in spec.translate_langs
    assert spec.translate_langs == ["en", "ar", "es"]
    # but it still gets a (verbatim) caption doc.
    assert "fa" in spec.caption_langs
    assert any("self-translated" in n for n in spec.notes)


def test_ur_xtts_provider_is_hard_error(synth_root):
    with pytest.raises(LangSpecError):
        build_langspec(_cfg(targetLangs=["ur"], dubLangs=["ur"],
                            per_language_provider={"ur": "xtts"}), synth_root)


def test_fa_xtts_provider_is_hard_error(synth_root):
    with pytest.raises(LangSpecError):
        build_langspec(_cfg(targetLangs=["fa"], dubLangs=["fa"],
                            per_language_provider={"fa": "xtts"}), synth_root)


def test_fa_voice_clone_true_is_hard_error(synth_root):
    # fa is piper-only; requesting a clone for it is fatal, same as a provider override.
    with pytest.raises(LangSpecError):
        build_langspec(_cfg(targetLangs=["fa"], dubLangs=["fa"],
                            voice_clone={"fa": True}), synth_root)


def test_ur_piper_provider_is_allowed(synth_root):
    spec = build_langspec(_cfg(targetLangs=["ur"], dubLangs=["ur"],
                               per_language_provider={"ur": "piper"}), synth_root)
    assert "ur" in spec.dub_langs
    assert "ur" not in spec.clone_langs  # piper-only, never clone


def test_dub_lang_not_in_produced_set_is_ignored(synth_root):
    # ru is neither source nor a translated target → can't dub it; ignored with a note.
    spec = build_langspec(_cfg(dubLangs=["en", "ru"]), synth_root)
    assert "ru" not in spec.dub_langs
    assert "en" in spec.dub_langs
    assert any("ru" in n and "ignored" in n for n in spec.notes)


def test_cc_not_in_produced_set_is_ignored(synth_root):
    spec = build_langspec(_cfg(closedCaptions=["en", "de"]), synth_root)
    assert "de" not in spec.caption_langs
    assert any("de" in n and "ignored" in n for n in spec.notes)


def test_clone_lang_autodetected_from_company_set(synth_root):
    # en and ar are in the synthetic clone_languages set → clone; source fa never clones here.
    spec = build_langspec(_cfg(), synth_root)
    assert "en" in spec.clone_langs
    assert "ar" in spec.clone_langs


def test_clone_disabled_globally_yields_no_clone(synth_root):
    spec = build_langspec(_cfg(clone={"enabled_by_default": False}), synth_root)
    assert spec.clone_langs == []


def test_explicit_voice_clone_false_overrides_company_default(synth_root):
    # ar is in the company clone set, but voice_clone[ar]=false forces piper.
    spec = build_langspec(_cfg(voice_clone={"ar": False}), synth_root)
    assert "ar" in spec.dub_langs
    assert "ar" not in spec.clone_langs
    assert "en" in spec.clone_langs  # unaffected


def test_missing_source_lang_is_error(synth_root):
    with pytest.raises(LangSpecError):
        build_langspec(_cfg(sourceLang=""), synth_root)
