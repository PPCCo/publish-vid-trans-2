# Stage argos-translate models via gh release (un-proxied machine)

HuggingFace / the model index is policy-blocked on the proxied work machine, so the
argos-translate `.argosmodel` language packages have to be mirrored through a GitHub Release
from a machine with **unrestricted network**. Same steps-only pattern as
`gh-release-caffeinate.md`.

**Everything else is already done on the work machine** — `argostranslate` + `argospm` are
installed in the repo `.venv`, the `argos-translate` stdin/stdout wrapper is in `.venv/bin`,
and `tools.local.json` pins `translation.mt_baseline: "argos"`. The **only** thing missing
there is the model *data*, which is what this doc stages.

`.argosmodel` files are **per-direction** — one package per `source→target` pair. Mirror a
`<src>→en` package for every source language you translate (`ur→en`, `fa→en`, `ar→en`, …); en
is the pivot the pipeline needs. A two-way language (e.g. en⇄es) needs **both**
`translate-en_es` and `translate-es_en`.

## 1. Create repo + push README

```bash
export GH_OWNER="PPCCo"
export GH_REPO="argos-models-mirror"
export REL_TAG="v1"

mkdir -p ~/argos-stage && cd ~/argos-stage

gh repo create ${GH_OWNER}/${GH_REPO} --public --confirm

echo "# ${GH_REPO}" > README.md
git init
git add README.md
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/${GH_OWNER}/${GH_REPO}.git
git push -u origin main
```

## 2. Download the `.argosmodel` packages + checksums

Use argos's own package manager to fetch the models (each is a single small zip, tens of MB —
no splitting needed). Grab one per direction you need.

```bash
cd ~/argos-stage

python3 -m pip install argostranslate   # just to use its package manager here

argospm update
argospm download translate-ur_en   # Urdu    → English
argospm download translate-fa_en   # Persian → English
argospm download translate-ar_en   # Arabic  → English
# (add any others: translate-es_en, translate-en_es, translate-en_fr, …)

# checksums so the work machine can verify the transfer
for f in *.argosmodel; do sha256sum "$f" > "$f.sha256"; done
ls -la *.argosmodel *.sha256
```

## 3. Create the release, upload 3 files at a time

Adjust the filenames to whatever `argospm download` actually produced.

```bash
cd ~/argos-stage

# batch 1 — creates the release (the models)
caffeinate -i gh release create ${REL_TAG} \
  translate-ur_en.argosmodel translate-fa_en.argosmodel translate-ar_en.argosmodel \
  --repo ${GH_OWNER}/${GH_REPO} \
  --title "Release ${REL_TAG}" \
  --notes "argos-translate .argosmodel packages; verify with .sha256"

# batch 2 — the checksums
caffeinate -i gh release upload ${REL_TAG} \
  translate-ur_en.argosmodel.sha256 translate-fa_en.argosmodel.sha256 translate-ar_en.argosmodel.sha256 \
  --repo ${GH_OWNER}/${GH_REPO} --clobber
```

If a batch fails mid-upload, just re-run that same line — `--clobber` makes it safe to repeat.

---

## Then, back on the work machine

Only two commands remain there (both offline — `argospm install` reads a local file, no
network). Downloading the release itself goes through GitHub, not the blocked model index.

```bash
cd ~/Dev/my-repos/pub/publish-vid-trans

# pull the models from the release
mkdir -p ~/argos-download && cd ~/argos-download
export GH_OWNER="PPCCo" GH_REPO="argos-models-mirror" REL_TAG="v1"
caffeinate -i gh release download ${REL_TAG} --repo ${GH_OWNER}/${GH_REPO} --pattern '*.argosmodel*'
for f in *.argosmodel; do sha256sum -c "$f.sha256"; done   # verify

# install each from its local file into the repo venv (offline)
cd ~/Dev/my-repos/pub/publish-vid-trans
for f in ~/argos-download/*.argosmodel; do .venv/bin/argospm install "$f"; done
.venv/bin/argospm list   # should list ur→en, fa→en, ar→en, …

# smoke-test one direction
printf '%s' 'یہ ایک آزمائشی جملہ ہے۔' | .venv/bin/argos-translate --from-code ur --to-code en
```

Once that prints an English sentence, the yt-app batch runner (and `project autopilot --mt` /
`run_pipeline.sh --mt`) will fill translation worksheets with no Claude tokens.

**Note:** filenames/pairs above are illustrative — swap in whatever `argospm download` actually
produced and adjust the batch groupings accordingly.
