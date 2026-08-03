"""Caption line-wrapping, deterministic SRT/VTT rendering, and readability validation.

Everything here is pure and deterministic so rendered subtitle files hash reproducibly:
renderers are hand-written (no pysrt / webvtt-py dependency), always emit ``\\n`` newlines,
and derive every byte from the canonical ``captions.<lang>.json``. Re-rendering the same
doc yields identical bytes -> identical SHA-256.

Script-class branching (ANALYSIS B7):
  * latin / cyrillic / rtl  -> measured in words, wrapped on whitespace, ~32-42 chars/line.
  * cjk                     -> measured in characters, wrapped without whitespace, ~13-16
    chars/line, reading speed measured in chars/min.
"""
from __future__ import annotations

from typing import Any

from .errors import CaptionError
from .util import CJK_LANGUAGES, RTL_LANGUAGES

# --- readability thresholds (plan §8/§11) ------------------------------------
MAX_CHARS_PER_LINE_LATIN = 42
MAX_CHARS_PER_LINE_CJK = 16
MAX_LINES_PER_CUE = 2
MIN_CUE_MS = 1000
MAX_CUE_MS = 7000
# Reading speed ceilings. Alphabetic scripts in words/min; CJK in characters/min.
MAX_WPM_LATIN = 180
MAX_CPM_CJK = 480  # ~16 chars/line * 2 lines readable in ~4s -> generous chars/min ceiling
CYRILLIC_LANGUAGES = {"ru", "uk", "bg", "sr", "mk"}


def script_class(language: str) -> str:
    lang = language.lower()
    if lang in CJK_LANGUAGES:
        return "cjk"
    if lang in RTL_LANGUAGES:
        return "rtl"
    if lang in CYRILLIC_LANGUAGES:
        return "cyrillic"
    return "latin"


def _max_chars(cls: str) -> int:
    return MAX_CHARS_PER_LINE_CJK if cls == "cjk" else MAX_CHARS_PER_LINE_LATIN


def wrap_lines(text: str, language: str, *, max_lines: int = MAX_LINES_PER_CUE) -> list[str]:
    """Wrap one cue's text into <= max_lines display lines for its script class.

    Whitespace-delimited scripts wrap on word boundaries; CJK wraps on character count
    (no inter-character whitespace). Deterministic and greedy: same input -> same output.
    """
    cls = script_class(language)
    limit = _max_chars(cls)
    text = text.strip()
    if not text:
        return []
    if cls == "cjk":
        chunks = [text[i:i + limit] for i in range(0, len(text), limit)]
    else:
        chunks = _wrap_words(text, limit)
    # Collapse overflow into the last allowed line rather than dropping content.
    if len(chunks) > max_lines:
        head = chunks[: max_lines - 1]
        tail_sep = "" if cls == "cjk" else " "
        head.append(tail_sep.join(chunks[max_lines - 1:]))
        chunks = head
    return chunks


def _wrap_words(text: str, limit: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _cue_lines(cue: dict[str, Any], language: str) -> list[str]:
    """Use author-supplied ``lines`` verbatim when present, else wrap on demand."""
    pre = cue.get("lines")
    if pre:
        return [str(line) for line in pre]
    return wrap_lines(cue.get("target_text", ""), language)


# --- timestamp formatting ----------------------------------------------------

def _fmt_ts(ms: int, *, sep: str) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def render_srt(caption_doc: dict[str, Any]) -> str:
    """SubRip. 1-based sequential indices, ``HH:MM:SS,mmm`` timestamps, blank-line separated."""
    language = caption_doc["language"]
    blocks: list[str] = []
    for index, cue in enumerate(caption_doc.get("cues", []), start=1):
        lines = _cue_lines(cue, language)
        body = "\n".join(lines) if lines else ""
        start = _fmt_ts(cue["start_ms"], sep=",")
        end = _fmt_ts(cue["end_ms"], sep=",")
        blocks.append(f"{index}\n{start} --> {end}\n{body}\n")
    return "\n".join(blocks)


def render_vtt(caption_doc: dict[str, Any]) -> str:
    """WebVTT. ``HH:MM:SS.mmm`` timestamps, ``WEBVTT`` header, blank-line separated cues."""
    language = caption_doc["language"]
    blocks: list[str] = ["WEBVTT\n"]
    for cue in caption_doc.get("cues", []):
        lines = _cue_lines(cue, language)
        body = "\n".join(lines) if lines else ""
        start = _fmt_ts(cue["start_ms"], sep=".")
        end = _fmt_ts(cue["end_ms"], sep=".")
        blocks.append(f"{start} --> {end}\n{body}\n")
    return "\n".join(blocks)


def slice_caption_doc(
    caption_doc: dict[str, Any],
    *,
    start_ms: int,
    end_ms: int,
    offset_ms: int = 0,
) -> dict[str, Any]:
    """Return a copy of ``caption_doc`` containing only cues overlapping [start_ms, end_ms),
    re-timed onto a target timeline.

    Used at PACKAGE for selection cut/join: each cue overlapping the window is clipped to the
    window, then shifted so ``start_ms`` maps to ``offset_ms`` on the output timeline. For a
    single clip pass ``offset_ms=0`` (clip-local zero); for a joined output pass the running
    duration of the clips already placed so cues land continuously across the seam. Cue ids
    are renumbered from 0. Pure/deterministic: identical input -> identical bytes downstream.
    """
    start, end, offset = int(start_ms), int(end_ms), int(offset_ms)
    out_cues: list[dict[str, Any]] = []
    for cue in caption_doc.get("cues", []) or []:
        cs, ce = int(cue["start_ms"]), int(cue["end_ms"])
        if ce <= start or cs >= end:
            continue  # no overlap with the window
        new_start = (max(cs, start) - start) + offset
        new_end = (min(ce, end) - start) + offset
        if new_end <= new_start:
            continue
        new_cue = dict(cue)
        new_cue["id"] = len(out_cues)
        new_cue["start_ms"] = new_start
        new_cue["end_ms"] = new_end
        out_cues.append(new_cue)
    out = dict(caption_doc)
    out["cues"] = out_cues
    return out


RENDERERS = {"srt": render_srt, "vtt": render_vtt}


def render(caption_doc: dict[str, Any], fmt: str) -> str:
    fmt = fmt.lower()
    if fmt not in RENDERERS:
        raise CaptionError(f"unknown caption format: {fmt} (want one of {sorted(RENDERERS)})")
    return RENDERERS[fmt](caption_doc)


# --- readability validation --------------------------------------------------

def _measure(text: str, cls: str) -> int:
    """Units for reading-speed: word count for alphabetic scripts, char count for CJK."""
    return len(text) if cls == "cjk" else len(text.split())


def validate_captions(caption_doc: dict[str, Any]) -> dict[str, Any]:
    """Pure readability + structural check -> {decision, findings, metrics}.

    Blocker -> FAIL (timing corrupt / empty); major -> CONDITIONAL_PASS (too-fast /
    too-long/short cues, over-long lines); else PASS.
    """
    language = caption_doc["language"]
    cls = caption_doc.get("script_class") or script_class(language)
    cues = caption_doc.get("cues", [])
    findings: list[dict[str, Any]] = []

    if not cues:
        return {
            "decision": "FAIL",
            "findings": [{"severity": "blocker", "category": "empty",
                          "summary": "captions have no cues"}],
            "metrics": {"cue_count": 0},
        }

    limit = _max_chars(cls)
    max_rate = MAX_CPM_CJK if cls == "cjk" else MAX_WPM_LATIN
    prev_end = 0
    fast_cues = 0
    for cue in cues:
        cid = cue.get("id")
        start, end = cue["start_ms"], cue["end_ms"]
        text = cue.get("target_text", "")
        if end < start:
            findings.append(_f("blocker", "timing",
                               f"cue {cid} ends before it starts ({start}->{end}ms)", cid, start))
            prev_end = max(prev_end, end)
            continue
        if start < prev_end:
            findings.append(_f("minor", "timing",
                               f"cue {cid} overlaps the previous cue by {prev_end - start}ms",
                               cid, start))
        dur = end - start
        if dur and dur < MIN_CUE_MS:
            findings.append(_f("minor", "duration",
                              f"cue {cid} is very short ({dur}ms < {MIN_CUE_MS}ms)", cid, start))
        if dur > MAX_CUE_MS:
            findings.append(_f("minor", "duration",
                              f"cue {cid} is very long ({dur}ms > {MAX_CUE_MS}ms)", cid, start))
        # reading speed
        if dur > 0 and text.strip():
            units = _measure(text, cls)
            rate = units / (dur / 60000.0)
            if rate > max_rate:
                fast_cues += 1
                unit_name = "cpm" if cls == "cjk" else "wpm"
                findings.append(_f("major", "reading-speed",
                                  f"cue {cid} too fast to read ({rate:.0f} {unit_name} > {max_rate})",
                                  cid, start, evidence=text[:120]))
        # line geometry
        lines = _cue_lines(cue, language)
        if len(lines) > MAX_LINES_PER_CUE:
            findings.append(_f("minor", "line-count",
                              f"cue {cid} wraps to {len(lines)} lines (> {MAX_LINES_PER_CUE})",
                              cid, start))
        for line in lines:
            if len(line) > limit:
                findings.append(_f("minor", "line-length",
                                  f"cue {cid} line exceeds {limit} chars ({len(line)})", cid, start,
                                  evidence=line[:120]))
                break
        prev_end = max(prev_end, end)

    metrics = {
        "cue_count": len(cues),
        "script_class": cls,
        "too_fast_cues": fast_cues,
    }
    severities = {f["severity"] for f in findings}
    if "blocker" in severities:
        decision = "FAIL"
    elif "major" in severities:
        decision = "CONDITIONAL_PASS"
    else:
        decision = "PASS"
    return {"decision": decision, "findings": findings, "metrics": metrics}


def _f(severity: str, category: str, summary: str, cue_id: Any = None,
       timestamp_ms: Any = None, evidence: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"severity": severity, "category": category, "summary": summary}
    if cue_id is not None:
        out["cue_id"] = cue_id
    if timestamp_ms is not None:
        out["timestamp_ms"] = timestamp_ms
    if evidence is not None:
        out["evidence"] = evidence
    return out
