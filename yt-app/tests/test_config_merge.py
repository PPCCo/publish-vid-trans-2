"""Per-video effective config = deep_merge(defaults, overrides) (plan §9)."""
from __future__ import annotations

from lib import config as cfg_mod


def test_overrides_replace_lists_wholesale():
    defaults = {"targetLangs": ["en", "fa", "ar", "es"], "dubLangs": ["en", "ar"]}
    entry = {"id": "v1", "overrides": {"targetLangs": ["en", "ar"], "dubLangs": ["en"]}}
    merged = cfg_mod.effective_config(defaults, entry)
    # lists are replaced, not concatenated (framework deep_merge semantics).
    assert merged["targetLangs"] == ["en", "ar"]
    assert merged["dubLangs"] == ["en"]


def test_nested_dicts_merge_key_by_key():
    defaults = {"per_language_provider": {"en": "xtts", "fa": "piper"}}
    entry = {"id": "v1", "overrides": {"per_language_provider": {"en": "piper"}}}
    merged = cfg_mod.effective_config(defaults, entry)
    # en is overridden, fa is preserved (dict-merge, not replace).
    assert merged["per_language_provider"] == {"en": "piper", "fa": "piper"}


def test_id_and_url_carried_onto_result():
    merged = cfg_mod.effective_config({"sourceLang": "fa"},
                                      {"id": "abc", "url": "http://x"})
    assert merged["id"] == "abc"
    assert merged["url"] == "http://x"


def test_no_overrides_returns_defaults_verbatim():
    defaults = {"sourceLang": "fa", "targetLangs": ["en"]}
    merged = cfg_mod.effective_config(defaults, {"id": "v1"})
    assert merged["sourceLang"] == "fa"
    assert merged["targetLangs"] == ["en"]


def test_matches_framework_deep_merge_directly():
    from video_translation_house.util import deep_merge

    defaults = {"a": {"x": 1, "y": 2}, "lst": [1, 2], "s": "keep"}
    overrides = {"a": {"y": 20, "z": 3}, "lst": [9]}
    expected = deep_merge(defaults, overrides)
    merged = cfg_mod.effective_config(defaults, {"overrides": overrides})
    for k, v in expected.items():
        assert merged[k] == v
