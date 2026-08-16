"""Freeze/trim running-gap algorithm (plan §7) — pure, no config, no ffmpeg."""
from __future__ import annotations

from lib.dubber import _lead_silence_ms, _natural_pause_before, plan_freezes


def _measure(cid, cap_start, cap_end, rendered_start, natural_ms):
    return {
        "id": cid,
        "caption_start_ms": cap_start,
        "caption_end_ms": cap_end,
        "rendered_start_ms": rendered_start,
        "rendered_end_ms": rendered_start + natural_ms,
        "natural_ms": natural_ms,
    }


def test_freeze_when_audio_runs_late():
    # cue0 slot 0-1000 but its audio is 1500 long; cue1 caption starts at 1000 but audio
    # can't start until 1500 → picture must freeze 500ms at the cue1 boundary.
    measures = [
        _measure(0, 0, 1000, 0, 1500),
        _measure(1, 1000, 2000, 1500, 1000),
    ]
    plan = plan_freezes(measures, trim=False)
    freezes = plan["freezes"]
    assert plan["trims"] == []
    assert any(f["at_ms"] == 1000 and f["freeze_ms"] == 500 for f in freezes)


def test_hold_only_never_trims_even_on_negative_gap():
    # cue1 audio starts EARLY relative to its caption (rendered 800 vs caption 1000): gap<0.
    # In hold-only mode this is left as an honest residual — NO trim emitted.
    measures = [
        _measure(0, 0, 1000, 0, 800),
        _measure(1, 1000, 2000, 800, 900),
    ]
    plan = plan_freezes(measures, trim=False)
    assert plan["trims"] == []


def test_trim_mode_trims_on_negative_gap():
    measures = [
        _measure(0, 0, 1000, 0, 800),
        _measure(1, 1000, 2000, 800, 900),
    ]
    plan = plan_freezes(measures, trim=True)
    # gap at cue1 = 800 - (1000 + 0 - 0) = -200 → trim 200ms.
    assert any(t["at_ms"] == 1000 and t["trim_ms"] == 200 for t in plan["trims"])


def test_tail_reconciliation_freezes_when_audio_outlasts_picture():
    # One cue: audio 6000 long, source video only 5000 → picture must freeze 1000ms at the tail.
    measures = [_measure(0, 0, 5000, 0, 6000)]
    plan = plan_freezes(measures, trim=False, source_duration_ms=5000)
    tail = [f for f in plan["freezes"] if f.get("tail")]
    assert tail and tail[0]["freeze_ms"] == 1000
    assert tail[0]["at_ms"] == 5000


def test_tail_reconciliation_trims_when_picture_outlasts_audio():
    # Audio 4000, source 5000, trim mode → trim 1000ms off the tail so picture end == audio end.
    measures = [_measure(0, 0, 5000, 0, 4000)]
    plan = plan_freezes(measures, trim=True, source_duration_ms=5000)
    tail = [t for t in plan["trims"] if t.get("tail")]
    assert tail and tail[0]["trim_ms"] == 1000


def test_cumulative_freeze_accounts_for_prior_holds():
    # Two late cues; the second gap is measured against the ALREADY-shifted picture (cum_freeze),
    # so a running backlog doesn't double-count.
    measures = [
        _measure(0, 0, 1000, 0, 1500),   # freeze 500 at 1000
        _measure(1, 1000, 2000, 1500, 1500),  # rendered_start 1500; gap = 1500-(1000+500)=0 → no freeze
    ]
    plan = plan_freezes(measures, trim=False)
    freezes = plan["freezes"]
    assert len(freezes) == 1
    assert freezes[0]["freeze_ms"] == 500


def test_lead_silence_video_uses_absolute_gap():
    cues = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1200, "end_ms": 2000}]
    # video mode: silence = caption_start - current timeline.
    assert _lead_silence_ms(cues, 1, timeline_ms=1000, is_still_image=False) == 200


def test_lead_silence_still_image_only_honors_real_pauses():
    cues = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1100, "end_ms": 2000}]
    # still-image, gap 100ms < 700 threshold → no lead silence (relative gap only).
    assert _lead_silence_ms(cues, 1, timeline_ms=999, is_still_image=True) == 0
    # a real >=700ms pause is preserved.
    cues2 = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1800, "end_ms": 2500}]
    assert _lead_silence_ms(cues2, 1, timeline_ms=999, is_still_image=True) == 800


def test_natural_pause_before():
    cues = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1800, "end_ms": 2000}]
    assert _natural_pause_before(cues, 0) is True   # first cue is always a "pause"
    assert _natural_pause_before(cues, 1) is True    # 800ms gap >= 700
    cues2 = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1100, "end_ms": 2000}]
    assert _natural_pause_before(cues2, 1) is False  # 100ms gap < 700
