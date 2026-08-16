"""Language-spec validation — fail fast, before any download / engine subprocess.

Resolves a video's raw config (sourceLang / targetLangs / dubLangs / closedCaptions /
per_language_provider / clone) into a validated, normalized ``LangSpec`` describing
exactly what to translate, caption, and dub. The rules (from the plan):

  * ``sourceLang`` appearing in ``targetLangs`` is DROPPED from the effective translate
    set — the source is never self-translated; it still gets VERBATIM captions
    (``target_text == source_text``) and can still be dubbed.
  * A ``dubLangs`` / ``closedCaptions`` entry NOT in the effective language set (source
    ∪ translated targets) is IGNORED with a note (you can't dub/caption a language you
    aren't producing text for).
  * A ``per_language_provider`` entry forcing a NON-piper engine on a piper-only language
    (``fa`` / ``ur`` — no XTTS coverage) is a HARD error (``LangSpecError``).
  * Clone-vs-piper is read LIVE from company config (``dubbing.clone_languages``) — a dub
    language in that set defaults to XTTS voice-clone unless ``clone`` is disabled or an
    explicit piper provider override is given for it. Voices / clone_languages /
    freeze_trim_languages are never hardcoded here.

Unlike the framework, yt-app has NO voice_clone_consent gate — it is a local operator
tool with no rights record; clone defaults on for clone-languages (opt out with
``clone.enabled_by_default=false`` or ``--no-clone``). This is a deliberate deviation
from framework rule 5, documented loudly in the user guide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Languages with NO XTTS voice-clone coverage — piper is their only engine. Forcing a
# non-piper provider on these is a hard error. (fa additionally has no staged piper voice
# yet; that surfaces later as a loud dub-time failure from resolve_dub_voice.)
PIPER_ONLY_LANGUAGES = frozenset({"fa", "ur"})


class LangSpecError(ValueError):
    """A language spec is invalid in a way that must stop the run before any work."""


@dataclass
class LangSpec:
    source_lang: str
    # Languages we translate the source INTO (source excluded — never self-translate).
    translate_langs: list[str]
    # Every language that ends up with a caption doc: source (verbatim) + translated.
    caption_langs: list[str]
    # Languages we render a dub for (subset of caption_langs).
    dub_langs: list[str]
    # provider per language for dubbing (may be absent -> resolver picks it).
    providers: dict[str, str] = field(default_factory=dict)
    # dub languages that resolve to XTTS voice-clone (off the source speaker).
    clone_langs: list[str] = field(default_factory=list)
    # human-readable notes about ignored/adjusted inputs (surfaced, not fatal).
    notes: list[str] = field(default_factory=list)

    def is_clone(self, language: str) -> bool:
        return language in self.clone_langs


def _norm_list(values: Any) -> list[str]:
    """Normalize a list-ish of language codes: strip, lowercase, dedupe, keep order."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        code = str(v).strip().lower()
        if code and code not in out:
            out.append(code)
    return out


def _clone_languages(root: Path) -> set[str]:
    """Live company ``dubbing.clone_languages`` (never hardcoded)."""
    from video_translation_house.dubbing import clone_languages  # lazy

    return set(clone_languages(root))


def build_langspec(cfg: dict[str, Any], root: Path) -> LangSpec:
    """Validate + normalize one video's language config into a ``LangSpec``.

    ``cfg`` is the effective per-video config (defaults + overrides). Raises
    ``LangSpecError`` on the hard errors above; records soft adjustments in ``notes``.
    """
    notes: list[str] = []

    source_lang = str(cfg.get("sourceLang") or "").strip().lower()
    if not source_lang:
        raise LangSpecError(
            "sourceLang is required (the language actually spoken in the video); "
            "ASR language auto-detect is not available in yt-app."
        )

    targets = _norm_list(cfg.get("targetLangs"))
    dub_req = _norm_list(cfg.get("dubLangs"))
    cc_req = _norm_list(cfg.get("closedCaptions"))
    providers_raw = cfg.get("per_language_provider") or {}
    providers = {str(k).strip().lower(): str(v).strip().lower()
                 for k, v in providers_raw.items()}

    # 1. Source is never self-translated: drop it from the translate set.
    translate_langs = [t for t in targets if t != source_lang]
    if source_lang in targets:
        notes.append(
            f"sourceLang {source_lang!r} appears in targetLangs; it is not "
            f"self-translated (verbatim source captions are produced instead)."
        )

    # The full set of languages that will have a caption doc: the source (verbatim) plus
    # every translated target. This is what dub/cc requests are validated against.
    caption_set = [source_lang, *translate_langs]
    # closedCaptions, if given, selects WHICH of the produced caption docs to emit as
    # sidecar .srt/.vtt; an entry outside caption_set is ignored.
    if cc_req:
        cc_ignored = [c for c in cc_req if c not in caption_set]
        for c in cc_ignored:
            notes.append(
                f"closedCaptions entry {c!r} is not in the produced language set "
                f"({caption_set}); ignored."
            )
        caption_langs = [c for c in caption_set if c in cc_req]
        # A requested CC language that IS produced but was filtered out by the intersection
        # above is intentional; if cc_req names none of the produced set, fall back to all.
        if not caption_langs:
            notes.append(
                "no closedCaptions entry matched a produced language; emitting captions "
                "for every produced language."
            )
            caption_langs = list(caption_set)
    else:
        caption_langs = list(caption_set)

    # 2. dubLangs outside the produced language set are ignored (can't dub text you don't
    #    produce).
    dub_langs: list[str] = []
    for d in dub_req:
        if d not in caption_set:
            notes.append(
                f"dubLangs entry {d!r} is not in the produced language set "
                f"({caption_set}); ignored."
            )
            continue
        dub_langs.append(d)

    # 3. per_language_provider forcing a non-piper engine on a piper-only language is fatal.
    clone_set = _clone_languages(root)
    clone_enabled = bool((cfg.get("clone") or {}).get("enabled_by_default", True))
    # Optional explicit per-language clone map: voice_clone[lang] = true|false. It OVERRIDES
    # both the global clone toggle and the company clone_languages default for that language
    # (false => piper, true => clone if the language has XTTS coverage). A piper-only lang
    # requesting clone (voice_clone[fa]=true) is a hard error, same as a provider override.
    voice_clone = {str(k).strip().lower(): bool(v)
                   for k, v in (cfg.get("voice_clone") or {}).items()}
    resolved_clone: list[str] = []
    for lang in dub_langs:
        forced = providers.get(lang)
        explicit_clone = voice_clone.get(lang)  # True / False / None (unset)
        if lang in PIPER_ONLY_LANGUAGES and forced and forced != "piper":
            raise LangSpecError(
                f"per_language_provider[{lang!r}] = {forced!r}, but {lang!r} is piper-only "
                f"(no XTTS voice-clone coverage). Use 'piper' or drop the override."
            )
        if lang in PIPER_ONLY_LANGUAGES and explicit_clone:
            raise LangSpecError(
                f"voice_clone[{lang!r}] = true, but {lang!r} is piper-only (no XTTS "
                f"voice-clone coverage). Set it false or drop the entry."
            )
        # Clone-vs-piper resolution, most specific first:
        #   1. an explicit piper provider override always wins -> no clone
        #   2. explicit voice_clone[lang] (True/False) overrides the global + company default
        #   3. otherwise: clone iff globally enabled AND lang is in the company clone set
        if forced == "piper":
            continue
        if explicit_clone is True:
            resolved_clone.append(lang)
        elif explicit_clone is False:
            continue
        elif clone_enabled and lang in clone_set:
            resolved_clone.append(lang)

    return LangSpec(
        source_lang=source_lang,
        translate_langs=translate_langs,
        caption_langs=caption_langs,
        dub_langs=dub_langs,
        providers=providers,
        clone_langs=resolved_clone,
        notes=notes,
    )
