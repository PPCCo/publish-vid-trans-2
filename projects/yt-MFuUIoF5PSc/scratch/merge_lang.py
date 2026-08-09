#!/usr/bin/env python3
"""Throwaway merge helper (rule 16 scaffolding): merge a {cues:[{id,target_text}]} map
into captions/<lang>.worksheet.json by cue id. Refuses to write if any Arabic-script
Quranic text remains (guillemet block or bare Arabic run), except it allows native
Perso-Arabic Urdu (checked by absence of the «» guillemet verse blocks for ur).

Usage: merge_lang.py <lang> <map.json>
"""
import json, re, sys

PROJ = "projects/yt-MFuUIoF5PSc"
AR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
GUILL = re.compile(r"«[^»]*»")

def main():
    lang, mappath = sys.argv[1], sys.argv[2]
    wspath = f"{PROJ}/captions/{lang}.worksheet.json"
    ws = json.load(open(wspath))
    m = json.load(open(mappath))
    cues = m["cues"] if isinstance(m, dict) and "cues" in m else m
    by_id = {int(c["id"]): c["target_text"] for c in cues}
    applied = 0
    for c in ws["cues"]:
        if c["id"] in by_id:
            c["target_text"] = by_id[c["id"]]
            applied += 1
    # verification
    empty = [c["id"] for c in ws["cues"] if not str(c["target_text"]).strip()]
    # guillemet-Arabic verse blocks are forbidden for ALL langs (incl ur)
    guill_ar = [c["id"] for c in ws["cues"]
                for b in GUILL.findall(c["target_text"]) if AR.search(b)]
    # bare Arabic (outside guillemets) forbidden for non-ur langs
    bare_ar = []
    if lang != "ur":
        for c in ws["cues"]:
            stripped = GUILL.sub("", c["target_text"])
            if AR.search(stripped):
                bare_ar.append(c["id"])
    print(f"{lang}: applied={applied}/{len(ws['cues'])} empty={empty} "
          f"guillemet-arabic={guill_ar} bare-arabic={bare_ar}")
    if empty or guill_ar or bare_ar:
        print(f"{lang}: NOT WRITTEN — issues found")
        sys.exit(1)
    json.dump(ws, open(wspath, "w"), ensure_ascii=False, indent=2)
    print(f"{lang}: WROTE {wspath}")

if __name__ == "__main__":
    main()
