"""status.json resume logic (plan §9) — pure, uses a tmp video dir + real artifact files."""
from __future__ import annotations

from pathlib import Path

from lib import status as st


def _touch(path: Path, content: str = "x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_fresh_status_runs_everything(tmp_path):
    doc = st.load_status(tmp_path, "vid1")
    for stage in st.STAGES:
        assert st.should_run(doc, stage)


def test_done_with_present_artifact_skips(tmp_path):
    art = _touch(tmp_path / "source" / "video.mp4")
    doc = st.new_status("vid1")
    st.mark_done(doc, "download", artifact=art, now="t0")
    assert st.should_run(doc, "download") is False


def test_done_with_missing_artifact_reruns(tmp_path):
    doc = st.new_status("vid1")
    st.mark_done(doc, "download", artifact=str(tmp_path / "gone.mp4"), now="t0")
    assert st.should_run(doc, "download") is True


def test_done_with_empty_artifact_reruns(tmp_path):
    empty = tmp_path / "empty.wav"
    empty.write_text("", encoding="utf-8")  # zero bytes → not "present"
    doc = st.new_status("vid1")
    st.mark_done(doc, "extract_audio", artifact=str(empty), now="t0")
    assert st.should_run(doc, "extract_audio") is True


def test_running_at_load_is_treated_as_pending(tmp_path):
    doc = st.new_status("vid1")
    st.mark_running(doc, "transcribe", now="t0")
    st.save_status(tmp_path, doc)
    reloaded = st.load_status(tmp_path, "vid1")
    assert reloaded["stages"]["transcribe"]["status"] == st.PENDING
    assert st.should_run(reloaded, "transcribe") is True


def test_per_language_independence(tmp_path):
    en_art = _touch(tmp_path / "captions" / "en.json")
    doc = st.new_status("vid1")
    st.mark_done(doc, "translate", language="en", artifact=en_art, now="t0")
    # en is done+present → skip; ar untouched → run.
    assert st.should_run(doc, "translate", language="en") is False
    assert st.should_run(doc, "translate", language="ar") is True


def test_scoped_parent_rolls_up_from_present_children(tmp_path):
    # The parent status reflects the per-language children RECORDED so far (the status doc has
    # no advance knowledge of which langs are planned — the pipeline records them as it goes).
    a = _touch(tmp_path / "captions" / "en.json")
    b = _touch(tmp_path / "captions" / "ar.json")
    doc = st.new_status("vid1")
    st.mark_running(doc, "translate", language="ar", now="t0")  # ar in flight
    st.mark_done(doc, "translate", language="en", artifact=a, now="t0")
    # en done but ar still running → parent is running, not done.
    assert doc["stages"]["translate"]["status"] == st.RUNNING
    st.mark_done(doc, "translate", language="ar", artifact=b, now="t0")
    assert doc["stages"]["translate"]["status"] == st.DONE


def test_scoped_parent_failed_if_any_lang_failed(tmp_path):
    a = _touch(tmp_path / "captions" / "en.json")
    doc = st.new_status("vid1")
    st.mark_done(doc, "dub", language="en", artifact=a, now="t0")
    st.mark_failed(doc, "dub", language="ar", error="boom", now="t0")
    assert doc["stages"]["dub"]["status"] == st.FAILED


def test_atomic_save_and_reload_roundtrip(tmp_path):
    art = _touch(tmp_path / "source" / "v.mp4")
    doc = st.new_status("vid1", url="http://x")
    st.mark_done(doc, "download", artifact=art, now="t0")
    st.save_status(tmp_path, doc)
    assert st.status_path(tmp_path).is_file()
    reloaded = st.load_status(tmp_path, "vid1")
    assert reloaded["url"] == "http://x"
    assert st.should_run(reloaded, "download") is False


def test_corrupt_status_falls_back_to_fresh(tmp_path):
    st.status_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    st.status_path(tmp_path).write_text("{ not json", encoding="utf-8")
    doc = st.load_status(tmp_path, "vid1")
    assert doc["video_id"] == "vid1"
    assert st.should_run(doc, "download") is True
