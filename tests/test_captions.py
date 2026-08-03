"""Phase 3 caption tests: script-class wrapping, byte-stable SRT/VTT rendering, and
deterministic readability validation (Latin + CJK paths)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import captions  # noqa: E402


def _doc(cues, language="en", script_class="latin"):
    return {
        "schema_version": "1.0", "project_id": "x", "language": language,
        "source_language": "fa", "script_class": script_class, "cues": cues, "created_at": "t",
    }


# --- script class + wrapping -------------------------------------------------

def test_script_class_branches():
    assert captions.script_class("en") == "latin"
    assert captions.script_class("ru") == "cyrillic"
    assert captions.script_class("fa") == "rtl"
    assert captions.script_class("zh") == "cjk"
    assert captions.script_class("ja") == "cjk"


def test_latin_wraps_on_words_two_lines():
    text = "This is a fairly long caption line that should wrap onto two display lines here."
    lines = captions.wrap_lines(text, "en")
    assert len(lines) <= 2
    assert all(len(line) <= captions.MAX_CHARS_PER_LINE_LATIN for line in lines[:-1])
    assert " ".join(lines).split() == text.split()  # no words lost


def test_cjk_wraps_on_characters():
    text = "这是一段需要按照字符数量来换行的中文字幕文本内容示例"
    lines = captions.wrap_lines(text, "zh")
    assert all(len(line) <= captions.MAX_CHARS_PER_LINE_CJK for line in lines[:-1])
    assert "".join(lines) == text  # no characters lost, no spaces introduced


def test_prewrapped_lines_used_verbatim():
    cue = {"id": 0, "start_ms": 0, "end_ms": 2000, "source_text": "s",
           "target_text": "ignored", "lines": ["Line one", "Line two"]}
    assert captions._cue_lines(cue, "en") == ["Line one", "Line two"]


# --- deterministic rendering -------------------------------------------------

def test_srt_is_byte_stable_and_well_formed():
    doc = _doc([
        {"id": 0, "start_ms": 0, "end_ms": 2000, "source_text": "a", "target_text": "Hello."},
        {"id": 1, "start_ms": 2000, "end_ms": 3500, "source_text": "b", "target_text": "World."},
    ])
    out1 = captions.render_srt(doc)
    out2 = captions.render_srt(doc)
    assert out1 == out2
    assert out1.startswith("1\n00:00:00,000 --> 00:00:02,000\nHello.\n")
    assert "2\n00:00:02,000 --> 00:00:03,500\nWorld.\n" in out1


def test_vtt_header_and_timestamp_format():
    doc = _doc([{"id": 0, "start_ms": 60000, "end_ms": 63200, "source_text": "a",
                 "target_text": "Minute one."}])
    out = captions.render_vtt(doc)
    assert out.startswith("WEBVTT\n")
    assert "00:01:00.000 --> 00:01:03.200" in out


# --- readability validation --------------------------------------------------

def test_validate_flags_too_fast_cue_conditional():
    # ~20 words in 1s -> way over 180 wpm -> major -> CONDITIONAL_PASS
    fast = "one two three four five six seven eight nine ten eleven twelve"
    doc = _doc([{"id": 0, "start_ms": 0, "end_ms": 1000, "source_text": "s", "target_text": fast}])
    result = captions.validate_captions(doc)
    assert result["decision"] == "CONDITIONAL_PASS"
    assert any(f["category"] == "reading-speed" for f in result["findings"])


def test_validate_backwards_timing_fails():
    doc = _doc([{"id": 0, "start_ms": 5000, "end_ms": 1000, "source_text": "s", "target_text": "x"}])
    result = captions.validate_captions(doc)
    assert result["decision"] == "FAIL"
    assert any(f["severity"] == "blocker" for f in result["findings"])


def test_validate_empty_fails():
    assert captions.validate_captions(_doc([]))["decision"] == "FAIL"


def test_validate_cjk_reading_speed_path():
    # A dense CJK cue crammed into a short window trips the chars/min ceiling.
    dense = "这是一段非常密集的中文字幕内容需要很快阅读完毕否则观众跟不上节奏啊"
    doc = _doc([{"id": 0, "start_ms": 0, "end_ms": 800, "source_text": "s", "target_text": dense}],
               language="zh", script_class="cjk")
    result = captions.validate_captions(doc)
    assert result["metrics"]["script_class"] == "cjk"
    assert any(f["category"] == "reading-speed" for f in result["findings"])


def test_validate_clean_latin_passes():
    doc = _doc([{"id": 0, "start_ms": 0, "end_ms": 3000, "source_text": "s",
                 "target_text": "A calm, readable line."}])
    assert captions.validate_captions(doc)["decision"] == "PASS"
