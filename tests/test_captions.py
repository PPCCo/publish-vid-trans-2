"""Phase 3 caption tests: script-class wrapping, byte-stable SRT/VTT rendering, and
deterministic readability validation (Latin + CJK paths)."""
from __future__ import annotations

import json
import shutil
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


# --- long-cue splitting ------------------------------------------------------

def _long_cue(**over):
    cue = {"id": 5, "start_ms": 1000, "end_ms": 22000,  # 21s span
           "source_text": "Yek. Do. Seh.",
           "target_text": "One sentence. Two sentence. Three sentence."}
    cue.update(over)
    return cue


def test_split_contiguous_bounded_and_covers_span():
    out = captions.split_long_cues([_long_cue()], max_ms=7000, language="en")
    assert len(out) == 3  # ceil(21000/7000)
    assert all((c["end_ms"] - c["start_ms"]) <= 7000 for c in out)
    # contiguous, and the union exactly covers the original [1000, 22000]
    assert out[0]["start_ms"] == 1000 and out[-1]["end_ms"] == 22000
    assert all(out[i]["end_ms"] == out[i + 1]["start_ms"] for i in range(len(out) - 1))


def test_split_prefers_sentence_boundaries_and_preserves_text():
    out = captions.split_long_cues([_long_cue()], max_ms=7000, language="en")
    # each 7s slice lands one whole sentence; no text lost across the join
    assert [c["target_text"] for c in out] == [
        "One sentence.", "Two sentence.", "Three sentence.",
    ]
    joined = " ".join(c["target_text"] for c in out).split()
    assert joined == _long_cue()["target_text"].split()
    assert all(c["target_text"].strip() for c in out)  # no empty sub-cue text


def test_split_passthrough_when_within_cap():
    cue = {"id": 0, "start_ms": 0, "end_ms": 5000, "source_text": "s", "target_text": "short"}
    out = captions.split_long_cues([cue], max_ms=7000, language="en")
    assert len(out) == 1
    assert out[0]["start_ms"] == 0 and out[0]["end_ms"] == 5000
    assert out[0]["target_text"] == "short"


def test_split_caps_subcue_count_at_token_count():
    # A 21s cue with only two words cannot fill three slices -> at most two sub-cues, no blanks.
    cue = {"id": 0, "start_ms": 0, "end_ms": 21000, "source_text": "x y", "target_text": "just two"}
    out = captions.split_long_cues([cue], max_ms=7000, language="en")
    assert len(out) == 2
    assert all(c["target_text"].strip() for c in out)


def test_split_single_token_passes_through():
    cue = {"id": 0, "start_ms": 0, "end_ms": 21000, "source_text": "x", "target_text": "word"}
    out = captions.split_long_cues([cue], max_ms=7000, language="en")
    assert len(out) == 1 and out[0]["target_text"] == "word"


def test_split_renumbers_ids_contiguously_in_mixed_list():
    cues = [
        {"id": 0, "start_ms": 0, "end_ms": 3000, "source_text": "a", "target_text": "short"},
        _long_cue(id=1, start_ms=3000, end_ms=24000),
    ]
    out = captions.split_long_cues(cues, max_ms=7000, language="en")
    assert [c["id"] for c in out] == list(range(len(out)))
    assert len(out) == 4  # 1 short + 3 split


def test_split_is_deterministic():
    a = captions.split_long_cues([_long_cue()], max_ms=7000, language="en")
    b = captions.split_long_cues([_long_cue()], max_ms=7000, language="en")
    assert a == b


def test_split_carries_flags_and_context_note_drops_lines_and_backtranslation():
    cue = _long_cue(flags=["editorial-religious"], context_note="note", lines=["x"],
                    back_translation="bt")
    out = captions.split_long_cues([cue], max_ms=7000, language="en")
    assert len(out) > 1
    for c in out:
        assert c["flags"] == ["editorial-religious"]
        assert c["context_note"] == "note"
        assert "lines" not in c
        assert "back_translation" not in c


def test_split_cjk_uses_characters():
    text = "这是一段很长的中文字幕" * 4  # 44 chars, no spaces
    cue = {"id": 0, "start_ms": 0, "end_ms": 21000, "source_text": text, "target_text": text}
    out = captions.split_long_cues([cue], max_ms=7000, language="zh")
    assert len(out) == 3
    assert "".join(c["target_text"] for c in out) == text  # no chars lost, no spaces added


def test_split_long_source_short_target_never_blanks_target():
    # Regression: a token-rich source over a long span must not force more sub-cues than the
    # (shorter, fewer-sentence) target can fill, which used to leave blank target_text. Here the
    # target is one long sentence (many words, ONE sentence token) over a 112s span -> ceil=16
    # slices; word-refinement + min-clamp must keep every target_text non-empty.
    src = " ".join(f"واژه{i}" for i in range(60)) + "."  # 60-word single-sentence Persian-ish source
    tgt = " ".join(f"word{i}" for i in range(60)) + "."  # 60-word single English sentence
    cue = {"id": 0, "start_ms": 0, "end_ms": 112000, "source_text": src, "target_text": tgt}
    out = captions.split_long_cues([cue], max_ms=7000, language="en")
    assert len(out) > 1
    assert all(c["target_text"].strip() for c in out)   # <-- the bug: no blank target
    assert all(c["source_text"].strip() for c in out)
    # text preserved on join (word order intact)
    assert " ".join(c["target_text"] for c in out).split() == tgt.split()
    # cap honored: 60 words / 112s -> 16 slices all fit under 7s
    assert all((c["end_ms"] - c["start_ms"]) <= 7000 for c in out)


def test_split_refines_single_sentence_to_words_when_n_exceeds_sentences():
    # A single long sentence (no sentence-final punctuation to split on) over a long span must
    # still divide into multiple non-empty word chunks, not one filled + rest blank.
    tgt = " ".join(f"w{i}" for i in range(40))  # 40 words, zero sentence breaks
    cue = {"id": 0, "start_ms": 0, "end_ms": 21000, "source_text": tgt, "target_text": tgt}
    out = captions.split_long_cues([cue], max_ms=7000, language="en")
    assert len(out) == 3  # ceil(21000/7000); 40 words easily fills 3
    assert all(c["target_text"].strip() for c in out)
    assert " ".join(c["target_text"] for c in out).split() == tgt.split()


def test_split_long_source_short_target_is_deterministic():
    src = " ".join(f"s{i}" for i in range(50)) + "."
    tgt = " ".join(f"t{i}" for i in range(50)) + "."
    cue = {"id": 0, "start_ms": 0, "end_ms": 90000, "source_text": src, "target_text": tgt}
    a = captions.split_long_cues([cue], max_ms=7000, language="en")
    b = captions.split_long_cues([cue], max_ms=7000, language="en")
    assert a == b and all(c["target_text"].strip() for c in a)


def test_split_preserves_inter_cue_silence_gaps():
    # Real transcripts have silence gaps between cues (cue A ends before cue B starts). The
    # splitter must tile only WITHIN each cue's window and never invent time across a gap.
    cues = [
        {"id": 0, "start_ms": 0, "end_ms": 21000,  # long -> splits into 3
         "source_text": "One. Two. Three.", "target_text": "One. Two. Three."},
        {"id": 1, "start_ms": 25000, "end_ms": 27000,  # 4s silence before it; short
         "source_text": "later", "target_text": "later"},
    ]
    out = captions.split_long_cues(cues, max_ms=7000, language="en")
    # sub-cues of cue 0 tile [0, 21000] exactly and contiguously
    first = [c for c in out if c["end_ms"] <= 21000]
    assert first[0]["start_ms"] == 0 and first[-1]["end_ms"] == 21000
    assert all(first[i]["end_ms"] == first[i + 1]["start_ms"] for i in range(len(first) - 1))
    # the 21000->25000 silence gap is preserved, not filled
    assert out[len(first)]["start_ms"] == 25000 and out[len(first)]["end_ms"] == 27000


def test_split_output_passes_validation_without_very_long_finding():
    out = captions.split_long_cues([_long_cue()], max_ms=7000, language="en")
    result = captions.validate_captions(_doc(out))
    assert not any("very long" in f["summary"] for f in result["findings"])
    assert not any(f["category"] == "empty" for f in result["findings"])


def test_max_cue_ms_reads_config_with_fallback(tmp_path):
    root = tmp_path / "repo"
    (root / ".claude" / "config").mkdir(parents=True)
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config", dirs_exist_ok=True)
    # default config carries quality_bars.captions.max_cue_duration_ms = 7000
    assert captions.max_cue_ms(root) == 7000
    # a custom cap is honored
    cfg_path = root / ".claude" / "config" / "company.default.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["quality_bars"]["captions"]["max_cue_duration_ms"] = 5000
    cfg_path.write_text(json.dumps(cfg))
    assert captions.max_cue_ms(root) == 5000
    # a bad value falls back to the module default
    cfg["quality_bars"]["captions"]["max_cue_duration_ms"] = 0
    cfg_path.write_text(json.dumps(cfg))
    assert captions.max_cue_ms(root) == captions.MAX_CUE_MS
