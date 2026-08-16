"""Translate a source caption doc into per-language caption docs, cues in parallel.

Given the source-language caption doc (from ``transcriber``), produce one caption doc per
target language:

  * A **translated** target (``lang != source``): ``mt.translate_text`` per cue, filling
    ``target_text`` while keeping ``id``/``start_ms``/``end_ms``/``source_text`` verbatim
    (timing is the contract — never re-timed, merged, or split here; rule 12's split already
    ran in the transcriber). Cues translate concurrently via a thread pool (each
    ``translate_text`` is a blocking engine subprocess that releases the GIL).
  * The **source** language: verbatim captions — ``target_text == source_text`` — no engine
    call at all (rule 7: the source is never self-translated).

Each produced doc is returned as ``{lang: caption_doc}``; the caller persists them to
``captions/<lang>.json``. Framework imports are lazy.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def _verbatim_doc(source_doc: dict[str, Any], language: str) -> dict[str, Any]:
    """A caption doc for the source language: target_text mirrors source_text."""
    doc = copy.deepcopy(source_doc)
    doc["language"] = language
    for cue in doc.get("cues", []):
        cue["target_text"] = cue.get("source_text", "") or ""
    return doc


def _translate_doc(
    source_doc: dict[str, Any],
    *,
    src_lang: str,
    tgt_lang: str,
    root,
    provider: str | None,
    model: str | None,
    parallel: int,
    timeout: int,
) -> dict[str, Any]:
    """Translate every cue's ``source_text`` into ``tgt_lang``; preserve timing/ids."""
    from video_translation_house.engines import mt as mt_mod  # lazy

    doc = copy.deepcopy(source_doc)
    doc["language"] = tgt_lang
    cues = doc.get("cues", [])

    def _one(text: str) -> str:
        # mt.translate_text short-circuits empty/whitespace to "" itself.
        return mt_mod.translate_text(
            text, src_lang, tgt_lang,
            provider=provider, model=model, root=root, timeout=timeout,
        )

    texts = [cue.get("source_text", "") or "" for cue in cues]
    workers = max(1, int(parallel))
    if workers == 1 or len(texts) <= 1:
        translated = [_one(t) for t in texts]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            translated = list(pool.map(_one, texts))

    for cue, txt in zip(cues, translated):
        cue["target_text"] = txt
    return doc


def translate_all(
    source_doc: dict[str, Any],
    *,
    src_lang: str,
    caption_langs: list[str],
    root,
    provider: str | None = None,
    model: str | None = None,
    parallel: int = 4,
    timeout: int = 600,
) -> dict[str, dict[str, Any]]:
    """Produce a caption doc for every language in ``caption_langs``.

    ``caption_langs`` is the full produced set (source + translated targets, already
    filtered by ``langspec``). The source language yields verbatim captions; every other
    language is translated cue-by-cue in parallel. Returns ``{lang: caption_doc}``.

    Note: languages translate sequentially at this level; parallelism is *within* a
    language (across its cues). The batch runner parallelizes across videos, not across a
    single video's target languages — translation is cheap relative to dub/mux and this
    keeps the engine's memory footprint bounded.
    """
    src_lang = str(src_lang).strip().lower()
    out: dict[str, dict[str, Any]] = {}
    for lang in caption_langs:
        lang = str(lang).strip().lower()
        if lang == src_lang:
            out[lang] = _verbatim_doc(source_doc, lang)
        else:
            out[lang] = _translate_doc(
                source_doc,
                src_lang=src_lang, tgt_lang=lang, root=root,
                provider=provider, model=model, parallel=parallel, timeout=timeout,
            )
    return out
