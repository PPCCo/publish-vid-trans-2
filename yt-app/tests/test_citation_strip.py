"""Citation strip (plan §7, rule 16) — the (Quran s:a) marker is removed from the TTS copy only."""
from __future__ import annotations

from lib.dubber import _strip_citations


def test_strips_citation_without_double_space():
    out = _strip_citations("By time (Quran 103:1), man is in loss.")
    assert "(Quran" not in out
    assert "  " not in out  # no doubled whitespace left behind
    assert out == "By time, man is in loss."


def test_strips_trailing_citation():
    out = _strip_citations("And the racers, racing (Quran 100:1)")
    assert out == "And the racers, racing"


def test_multiple_citations_all_removed():
    out = _strip_citations("A (Quran 1:1) then B (Quran 2:255) end.")
    assert "(Quran" not in out
    assert "  " not in out


def test_no_citation_is_identity():
    text = "A plain sentence with no citation."
    assert _strip_citations(text) == text


def test_empty_string():
    assert _strip_citations("") == ""
