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

import math
import re
from pathlib import Path
from typing import Any

from .errors import CaptionError
from .util import CJK_LANGUAGES, RTL_LANGUAGES, load_company_config

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


# --- long-cue splitting (caption timing) -------------------------------------
#
# The ASR/transcript cue-list can contain very long cues (a merge over a long uninterrupted
# passage). Carried unchanged into captions, those render as multi-minute subtitle blocks. At
# caption-build time we deterministically split any cue longer than the configured cap into N
# proportional sub-cues so downstream validation/render/dub inherit readable cues. This is
# CAPTION-ONLY: the approved source transcript (and the transcript-gate English gloss built from
# it) are never re-timed here.

# Sentence-final punctuation across the scripts we handle (Latin + Persian/Arabic + CJK):
# '.', '!', '?', '…', Arabic full stop '۔', Arabic question mark '؟', CJK '。！？'.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…۔؟。！？])\s+")


def max_cue_ms(root: Path) -> int:
    """The caption cue-length cap (ms), read from company config, falling back to MAX_CUE_MS.

    Mirrors the config-reader idiom in budget.py / dubbing.py — never hardcode the bar. The
    same value drives both the readability 'very long' finding and the auto-split below.
    """
    caps = load_company_config(root).get("quality_bars", {}).get("captions", {})
    value = caps.get("max_cue_duration_ms", MAX_CUE_MS)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return MAX_CUE_MS
    return value if value > 0 else MAX_CUE_MS


def max_reading_rate(root: Path, cls: str) -> int:
    """Reading-speed ceiling for a script class, read from company config with a constant
    fallback (same idiom as ``max_cue_ms``). CJK is chars/min (``max_reading_speed_cpm``);
    every other script is words/min (``max_reading_speed_wpm``). Never hardcode the bar at
    the call site — a project that translates fast-paced oratory raises it via config overlay
    rather than editing approved caption artifacts (rules 6/8)."""
    caps = load_company_config(root).get("quality_bars", {}).get("captions", {})
    if cls == "cjk":
        key, default = "max_reading_speed_cjk_cpm", MAX_CPM_CJK
    else:
        key, default = "max_reading_speed_wpm", MAX_WPM_LATIN
    value = caps.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _tokenize_for_split(text: str, cls: str) -> tuple[list[str], str]:
    """Tokens to distribute across sub-cues, plus the join separator. Sentences first; if a
    single 'sentence' (no sentence-final punctuation) it falls back to words, and CJK (no word
    spaces) to characters. Returns (tokens, sep)."""
    tokens = [t for t in _SENTENCE_END_RE.split(text) if t.strip()]
    if len(tokens) <= 1:
        tokens = list(text) if cls == "cjk" else text.split()
    return tokens, ("" if cls == "cjk" else " ")


def _split_text(text: str, n: int, cls: str) -> list[str]:
    """Split ``text`` into exactly ``n`` chunks by proportional character share, preferring
    sentence then word (then character) boundaries.

    Assigns whole tokens to chunks so each chunk ≈ 1/n of the text and every chunk is non-empty
    whenever there is text (the caller guarantees ``n <= token_count`` so no chunk starves).
    Content is never dropped: the final chunk absorbs any remainder.
    """
    text = (text or "").strip()
    if n <= 1 or not text:
        return [text] + [""] * (n - 1) if n > 1 else [text]

    tokens, sep = _tokenize_for_split(text, cls)
    # If sentence tokens are too few to fill n chunks, refine to word (or char, for CJK)
    # granularity so no chunk starves. Sentence boundaries are kept whenever there are enough
    # sentences for n (the common, nicer case); we only go finer when forced to.
    if len(tokens) < n:
        finer = list(text) if cls == "cjk" else text.split()
        if len(finer) > len(tokens):
            tokens = finer
    total = sum(len(t) for t in tokens)
    if not tokens or total == 0:
        return [text] + [""] * (n - 1)

    chunks: list[list[str]] = [[] for _ in range(n)]
    acc = 0
    ci = 0
    remaining = len(tokens)
    for tok in tokens:
        # Reserve at least one token for each still-empty later chunk so none starves.
        empty_later = sum(1 for c in chunks[ci + 1:] if not c)
        if ci < n - 1 and remaining <= empty_later:
            ci += 1
        chunks[ci].append(tok)
        acc += len(tok)
        remaining -= 1
        target = round(total * (ci + 1) / n)
        if ci < n - 1 and acc >= target and chunks[ci]:
            ci += 1
    return [sep.join(c).strip() for c in chunks]


def _fillable_token_count(text: str, cls: str) -> int:
    """How many non-empty chunks ``text`` can be split into at its finest useful granularity
    (words for Latin/Arabic, characters for CJK). This is the max number of sub-cues that side
    can fill without a blank."""
    text = (text or "").strip()
    if not text:
        return 0
    atoms = list(text) if cls == "cjk" else text.split()
    return max(1, len(atoms))


def _text_token_count(cue: dict[str, Any], cls: str) -> int:
    """The most sub-cues we can create while keeping every side that has text non-empty: the
    MIN over the non-empty sides of their word/char-level fillable token counts. Using min (not
    max) is what prevents a long, token-rich source from forcing more sub-cues than a shorter
    target can fill — which would leave blank ``target_text`` (see split_long_cues)."""
    counts = [
        _fillable_token_count(cue.get(key) or "", cls)
        for key in ("target_text", "source_text")
    ]
    counts = [c for c in counts if c > 0]
    return min(counts) if counts else 1


def _carry_fields(cue: dict[str, Any]) -> dict[str, Any]:
    """Optional per-cue fields that survive a split (each sub-cue keeps them). Excludes
    ``lines`` (must re-wrap for the new text) and ``back_translation`` (round-trip no longer
    aligns to the sub-cue text)."""
    out: dict[str, Any] = {}
    for key in ("translator_confidence", "flags", "context_note"):
        if cue.get(key) not in (None, [], ""):
            out[key] = cue[key]
    return out


def split_long_cues(cues: list[dict[str, Any]], *, max_ms: int, language: str) -> list[dict[str, Any]]:
    """Split any cue whose span exceeds ``max_ms`` into proportional <=max_ms sub-cues.

    Pure and deterministic (no clock/random): identical input+cap -> identical output, so
    re-rendered captions hash stably. Time is divided into equal integer-ms slices (the last
    slice ends exactly at the original ``end_ms``); text is divided proportionally at
    sentence/word boundaries (see ``_split_text``). All output cues are renumbered id=0..M.
    Cues at or under the cap pass through unchanged (aside from renumbering).
    """
    if max_ms <= 0:
        raise CaptionError(f"max_ms must be positive, got {max_ms}")
    cls = script_class(language)
    out: list[dict[str, Any]] = []
    for cue in cues:
        start, end = int(cue["start_ms"]), int(cue["end_ms"])
        span = end - start
        if span <= max_ms:
            out.append({**cue, "id": len(out)})
            continue
        # As many slices as the cap requires, but never more than the SPARSEST non-empty side
        # can fill at word/char granularity (see _text_token_count) — otherwise a token-rich
        # source over a long span would force more sub-cues than a shorter target can fill,
        # leaving blank target_text. A genuinely word-sparse long cue thus becomes fewer,
        # longer sub-cues, every one carrying text, rather than any blank ones.
        n = min(math.ceil(span / max_ms), _text_token_count(cue, cls))
        if n <= 1:
            out.append({**cue, "id": len(out)})
            continue
        src_chunks = _split_text(cue.get("source_text", ""), n, cls)
        tgt_chunks = _split_text(cue.get("target_text", ""), n, cls)
        carried = _carry_fields(cue)
        for i in range(n):
            sub_start = start + (span * i) // n
            sub_end = end if i == n - 1 else start + (span * (i + 1)) // n
            out.append({
                "id": len(out),
                "start_ms": sub_start,
                "end_ms": sub_end,
                "source_text": src_chunks[i],
                "target_text": tgt_chunks[i],
                **carried,
            })
    return out


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


def validate_captions(
    caption_doc: dict[str, Any], *, max_reading_rate_override: int | None = None
) -> dict[str, Any]:
    """Pure readability + structural check -> {decision, findings, metrics}.

    Blocker -> FAIL (timing corrupt / empty); major -> CONDITIONAL_PASS (too-fast /
    too-long/short cues, over-long lines); else PASS.

    ``max_reading_rate_override`` (words/min for alphabetic, chars/min for CJK) lets the
    caller supply the config-derived ceiling; when ``None`` the module default for the
    script class is used. Kept pure — the caller reads config and passes the value in.
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
    if max_reading_rate_override is not None:
        max_rate = max_reading_rate_override
    else:
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
