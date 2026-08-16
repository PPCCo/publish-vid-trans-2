"""Guardrails added after the first smoke run (post-transcribe language check + per-video
CLI overrides + up-front code validation). All pure logic — no engines, no network."""
from __future__ import annotations

import pytest

from lib import batch as batch_mod
from lib import langspec as ls
from lib import transcriber as tr


# --- up-front language-code validation (langspec, before any download) -----------------
def test_unknown_source_lang_is_rejected(synth_root):
    with pytest.raises(ls.LangSpecError) as exc:
        ls.build_langspec({"sourceLang": "urdu", "targetLangs": ["en"]}, synth_root)
    assert "unrecognized language code" in str(exc.value)


def test_unknown_target_lang_is_rejected(synth_root):
    with pytest.raises(ls.LangSpecError):
        ls.build_langspec({"sourceLang": "ur", "targetLangs": ["en", "eng"]}, synth_root)


def test_known_codes_pass_validation(synth_root):
    spec = ls.build_langspec(
        {"sourceLang": "ur", "targetLangs": ["en", "ar"], "dubLangs": ["en"]}, synth_root)
    assert spec.source_lang == "ur"
    assert "en" in spec.caption_langs and "ar" in spec.caption_langs


def test_missing_source_lang_still_errors(synth_root):
    with pytest.raises(ls.LangSpecError):
        ls.build_langspec({"targetLangs": ["en"]}, synth_root)


# --- post-transcribe detected-language check (pure helpers) ----------------------------
def test_same_language_exact():
    assert tr._same_language("ur", "ur") is True


def test_same_language_alias_zh():
    # whisper often reports zh-cn; declaring zh must NOT be a false mismatch.
    assert tr._same_language("zh-cn", "zh") is True
    assert tr._same_language("zh", "zh-tw") is True


def test_different_language_is_mismatch():
    assert tr._same_language("fa", "ur") is False


def test_language_mismatch_error_message_names_both():
    err = tr.LanguageMismatchError(declared="fa", detected="ur")
    msg = str(err)
    assert "fa" in msg and "ur" in msg
    assert "--source-lang ur" in msg  # points at the fix
    assert "--force" in msg           # and the override


# --- per-video CLI overrides reach the entry (batch.build_entries) ---------------------
def test_single_ref_overrides_merge_into_entry():
    entries = batch_mod.build_entries(ref="https://youtu.be/DRsOfjpacoc",
                                      overrides={"sourceLang": "ur", "dubLangs": ["en", "ur"]})
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == "DRsOfjpacoc"  # derived from the /tail
    assert e["overrides"]["sourceLang"] == "ur"
    assert e["overrides"]["dubLangs"] == ["en", "ur"]


def test_watch_url_derives_id_from_v_param():
    entries = batch_mod.build_entries(ref="https://www.youtube.com/watch?v=DRsOfjpacoc")
    assert entries[0]["id"] == "DRsOfjpacoc"


def test_overrides_rejected_with_list(tmp_path):
    import json

    lst = tmp_path / "l.json"
    lst.write_text(json.dumps({"videos": [{"id": "a", "url": "ua"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        batch_mod.build_entries(list_path=str(lst), overrides={"sourceLang": "ur"})


def test_no_overrides_single_ref_has_no_overrides_key():
    entries = batch_mod.build_entries(ref="abc123")
    assert "overrides" not in entries[0] or not entries[0]["overrides"]


# --- fetch-flag preflight (fail loud before the pool if a download is needed) ----------
def test_pending_downloads_lists_videos_without_completed_download(tmp_path):
    # video "a" has no status.json → needs download; "b" is recorded done → exempt.
    from lib import status as status_mod

    (tmp_path / "b" / "source").mkdir(parents=True)
    mp4 = tmp_path / "b" / "source" / "b.mp4"
    mp4.write_bytes(b"\x00")
    doc = status_mod.new_status("b", url="ub")
    status_mod.mark_done(doc, "download", artifact=str(mp4))
    status_mod.save_status(tmp_path / "b", doc)

    pending = batch_mod._pending_downloads(
        [{"id": "a", "url": "ua"}, {"id": "b", "url": "ub"}], tmp_path)
    assert pending == ["a"]  # only the un-downloaded video


def test_run_batch_preflights_fetch_flag(tmp_path, monkeypatch):
    """A needed download + disabled fetch flag raises BEFORE any pipeline work, naming the fix."""
    monkeypatch.setattr(batch_mod.config_mod, "load_defaults", lambda: {"parallel_executions": 1})
    monkeypatch.setattr(batch_mod, "_pending_downloads", lambda cfgs, out: ["a"])
    from lib import downloader
    monkeypatch.setattr(downloader, "fetch_enabled", lambda: False)

    called = {"ran": False}
    monkeypatch.setattr(batch_mod.ytpipe, "run_video",
                        lambda *a, **k: called.__setitem__("ran", True))

    with pytest.raises(RuntimeError) as exc:
        batch_mod.run_batch(tmp_path, ref="a", out_root=tmp_path, log=lambda *a: None)
    msg = str(exc.value)
    assert "VIDTRANS_FETCH_ENABLED=1" in msg  # names the fix
    assert not called["ran"]                  # never spun up the pool


def test_run_batch_no_preflight_when_flag_on(tmp_path, monkeypatch):
    """Flag on → preflight passes and the pool runs (run_video is invoked)."""
    monkeypatch.setattr(batch_mod.config_mod, "load_defaults", lambda: {"parallel_executions": 1})
    monkeypatch.setattr(batch_mod, "_pending_downloads", lambda cfgs, out: ["a"])
    from lib import downloader
    monkeypatch.setattr(downloader, "fetch_enabled", lambda: True)
    monkeypatch.setattr(batch_mod.ytpipe, "run_video", lambda *a, **k: {"ok": True})

    summary = batch_mod.run_batch(tmp_path, ref="a", out_root=tmp_path, log=lambda *a: None)
    assert summary["total"] == 1 and summary["succeeded"] == ["a"]


def test_run_batch_no_preflight_when_nothing_to_download(tmp_path, monkeypatch):
    """All downloads already done → flag NOT required (resume to finish translate/dub/mux)."""
    monkeypatch.setattr(batch_mod.config_mod, "load_defaults", lambda: {"parallel_executions": 1})
    monkeypatch.setattr(batch_mod, "_pending_downloads", lambda cfgs, out: [])  # nothing pending
    from lib import downloader
    monkeypatch.setattr(downloader, "fetch_enabled", lambda: False)  # off, but irrelevant
    monkeypatch.setattr(batch_mod.ytpipe, "run_video", lambda *a, **k: {"ok": True})

    summary = batch_mod.run_batch(tmp_path, ref="a", out_root=tmp_path, log=lambda *a: None)
    assert summary["succeeded"] == ["a"]  # ran despite flag off


# --- effective_config carries the override through to a top-level force flag -----------
def test_force_override_lands_top_level():
    from lib import config as cfg_mod

    merged = cfg_mod.effective_config(
        {"sourceLang": "fa", "targetLangs": ["en"]},
        {"id": "v1", "overrides": {"sourceLang": "ur", "force": True}})
    assert merged["sourceLang"] == "ur"
    assert merged["force"] is True
