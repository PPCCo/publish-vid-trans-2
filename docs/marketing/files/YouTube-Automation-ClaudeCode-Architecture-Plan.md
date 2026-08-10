# YouTube Publishing & Promotion Automation — Claude Code Architecture Plan
### PLANNING DOCUMENT ONLY — nothing here is implemented yet

This mirrors the deterministic, gate-based lifecycle pattern you already use in `translate-house` and `publishing-house`: canonical source → generated artifacts → human authorization gate → external publish. Same philosophy, applied to YouTube + cross-platform promotion.

---

## 1. GOALS

Automate everything that is *safe and policy-compliant* to automate:
- Metadata generation (title/description/tags/category/thumbnail concepts) from the source video/transcript
- SEO keyword research to inform metadata
- Upload + scheduling to YouTube via official API
- Playlist assignment, end-screen/card configuration
- Cross-posting short clips to Telegram/Instagram/TikTok (where officially supported)
- Analytics pull-back for a feedback loop (which titles/thumbnails perform, feed back into future generation)

Keep human-in-the-loop for: final publish approval, thumbnail final pick, anything touching monetization settings, and any content-sensitive judgment calls (see Section 6, Risks).

---

## 2. WHY THIS FITS YOUR EXISTING PATTERNS

Your `translate-house` and `publishing-house` frameworks already establish:
- **Deterministic CLI/lifecycle state machine** (not free-form agent looping)
- **Human-authorization gates** before anything goes external
- **Multi-vendor fallbacks** for each processing step
- **Compliance-first design**

This plan reuses that exact skeleton, with YouTube-specific states:

```
DRAFT → METADATA_GENERATED → METADATA_REVIEWED (gate) → THUMBNAIL_GENERATED →
THUMBNAIL_REVIEWED (gate) → UPLOADED (private/unlisted) → SCHEDULED →
PUBLISHED (gate: final go/no-go) → PROMOTED (cross-platform) → ANALYZED
```

Each state transition is a discrete, resumable CLI command — exactly like your existing lifecycle scripts — so a failure at "THUMBNAIL_GENERATED" doesn't force a re-run of transcription/translation.

---

## 3. COMPONENT BREAKDOWN

### 3.1 Metadata Generation Skill (`yt-metadata`)
- **Input**: final video transcript/subtitle file (already an output of `translate-house`), series/playlist context, target language.
- **Process**: Claude generates 3–5 title candidates, a description (with auto-timestamps derived from subtitle cue points), 10–15 tags, and category recommendation, following the SEO conventions in the Operator's Guide (Part 4).
- **Output**: a structured JSON/YAML metadata draft file per video, stored alongside the video asset.
- **Gate**: human reviews/edits the JSON before it can progress — this is the natural checkpoint, same as your existing "human-authorization gates."

### 3.2 SEO Keyword Research Automation (`yt-seo-research`)
- Pulls suggested search terms via:
  - **YouTube's `search.list` autocomplete-adjacent signal** (or scraping-free approach: use the Data API `search.list` sparingly — it's the most expensive call at ~100 units/request, so budget it — see Section 5)
  - Free tools with APIs/exports: Google Trends (`pytrends` unofficial Python package — no key needed but unofficial/rate-limited), TubeBuddy/VidIQ (no public API on free tier — likely manual input only)
- Output: ranked keyword candidates feeding into `yt-metadata` as context.

### 3.3 Thumbnail Generation Skill (`yt-thumbnail`)
- Generates 2–3 thumbnail concepts (text overlay + background selection) using your consistent brand template (Canva-style layout, but generated programmatically via HTML/CSS → image render, or Pillow in Python for simple text-on-image composition).
- **Gate**: human picks final thumbnail (subjective/branding judgment — keep this manual).

### 3.4 Upload & Scheduling Workflow (`yt-upload`)
- Wraps the **YouTube Data API v3** `videos.insert` (resumable upload) + `videos.update` (metadata/thumbnail/category) + `playlistItems.insert` (add to series playlist) + `videos.update` for scheduled publish time.
- Uploads as **private/unlisted first**, only flips to scheduled/public after the final human gate — this gives you a safety window to catch errors before the audience sees anything.
- OAuth2 flow: one-time authorization per channel, refresh token stored securely (never in the repo — use environment variable/secrets manager, not committed config).

### 3.5 End Screens / Cards Automation (`yt-endscreen`)
- The Data API doesn't currently expose a clean endpoint for programmatically setting End Screens/Cards (these are historically UI-only features with limited API surface) — **flag this as a likely manual step** or requiring Studio automation via browser tooling (e.g. Claude in Chrome) rather than the Data API. Verify current API coverage before building; this is the kind of detail that needs a fresh check against `developers.google.com/youtube/v3` at build time, not assumed now.

### 3.6 Cross-Platform Promotion (`yt-crosspost`)
| Platform | API | Automation feasibility | Notes |
|---|---|---|---|
| **Telegram** | Telegram Bot API (free, simple) | High — fully automatable | Post clip + caption + link to new video on publish |
| **Instagram** | Meta Graph API (Instagram Content Publishing API) | Medium — requires Business/Creator account + Meta App Review for some permissions | Reels publishing via API is supported but has stricter review; budget setup time |
| **TikTok** | TikTok Content Posting API | Medium — requires app approval, rate limits, unaudited apps get sandbox-only access initially | Build against sandbox first |
| **Spotify for Podcasters / Apple Podcasts** | RSS feed based, not a push API | High — generate RSS feed programmatically, both platforms just poll it | Good automation candidate: convert audio, update RSS XML, both platforms pick it up automatically |
| **Reddit/Facebook Groups** | Reddit has a public API; Facebook Groups posting via API is heavily restricted for non-Page content | Low/Medium | Likely keep manual — spammy-looking automated posts to community groups risk bans and reputational harm to a religious brand |

### 3.7 Analytics Feedback Loop (`yt-analytics`)
- Pull via **YouTube Analytics API** (separate quota from Data API) on a schedule (e.g. weekly): views, average view duration, CTR, traffic source breakdown per video.
- Feed results back into `yt-metadata` as historical context ("titles using pattern X historically got higher CTR") — a lightweight, transparent heuristic layer, not a black-box model.

---

## 4. PROPOSED CLAUDE CODE STRUCTURE

Following your existing repo conventions:

```
youtube-publisher/
├── SKILL.md                      # top-level skill description/router
├── commands/
│   ├── metadata-generate.md
│   ├── seo-research.md
│   ├── thumbnail-generate.md
│   ├── upload.md
│   ├── schedule.md
│   ├── crosspost.md
│   └── analytics-pull.md
├── scripts/
│   ├── yt_auth.py                # OAuth2 flow, token refresh
│   ├── yt_metadata_gen.py
│   ├── yt_seo_research.py
│   ├── yt_thumbnail_gen.py
│   ├── yt_upload.py               # resumable upload wrapper
│   ├── yt_playlist.py
│   ├── yt_analytics.py
│   ├── telegram_post.py
│   ├── instagram_post.py
│   └── tiktok_post.py
├── state/
│   └── lifecycle.py               # state machine, same pattern as translate-house
├── config/
│   ├── channel_config.yaml        # per-channel branding, category defaults, keyword list
│   └── quota_budget.yaml          # daily quota allocation across operations
└── gates/
    ├── metadata_review.md         # human gate instructions/checklist
    ├── thumbnail_review.md
    └── publish_review.md
```

---

## 5. API / QUOTA BUDGETING (Data API v3)

- Default quota: **10,000 units/day per Google Cloud project**, resets midnight Pacific Time.
- Approximate costs (verify current values at build time — YouTube has changed these more than once in 2025–2026): reads ~1 unit, search ~100 units, writes/updates ~50 units, video upload historically ~1,600 units (some reporting suggests this has been reduced/moved to a separate bucket in 2026 — **confirm in Cloud Console before finalizing daily-upload capacity planning**).
- Practical implication: with a single project, you can comfortably do several video publishes/day plus modest metadata/analytics calls without hitting the ceiling — but **avoid heavy `search.list` usage** in the SEO research script (it's the most expensive call type); cache results aggressively rather than re-querying.
- **With nine confirmed channels (en/zh/ar/fa/ur/fr/es/pt/ru), use a separate GCP project per channel from the start**, not as a later optimization — each channel gets its own independent 10,000-unit/day pool, so one language's heavy upload/analytics day never starves another's. This also cleanly separates OAuth credentials per channel, which matters for the credential-security point in Section 6.
- `channel_config.yaml` (Section 4) should be keyed by language code and include: GCP project ID, OAuth client credentials reference, category defaults, brand keyword list, and — critically for fa/ru — a `primary_distribution` field distinguishing "YouTube-first" languages from "Telegram/VK-first" languages, since the lifecycle state machine's promotion step (`PROMOTED`) should weight cross-posting differently per channel rather than treating all nine identically.

---

## 6. RISKS / THINGS TO DESIGN AROUND CAREFULLY

1. **YouTube ToS on automation**: uploading via the official Data API with OAuth you control is explicitly supported and is how every professional multi-channel network operates — this is *not* a gray area, unlike scraping or fake-engagement automation. Stay within the official API surface.
2. **Never fully auto-publish without a human gate** — a mistranslation, mis-transliterated name, or a metadata error in religious content can cause real reputational/theological harm; keep the `PUBLISHED` gate as a hard stop, matching your existing `publishing-house` philosophy exactly.
3. **Meta/TikTok API approval cycles** can take weeks and get rejected on first submission — budget for this in any timeline; don't assume day-one automation there.
4. **Content sensitivity review**: an automated metadata generator could technically produce a title/description that inadvertently misrepresents a scholarly position or oversimplifies a fiqh nuance — this is exactly the kind of judgment call that should stay at a gate, not be auto-approved even if it "looks fine" statistically.
5. **Credential security**: OAuth refresh tokens and API keys must never be committed to the repo; use environment variables or a secrets manager, and rotate periodically.
6. **Rate limits vs. quota**: quota is daily-unit based, but there are also **per-minute rate limits** — a burst of cross-posting on publish day could hit `rateLimitExceeded` (different from `quotaExceeded`) and needs its own backoff/retry logic, not just "wait until midnight."

---

## 7. BUILD SEQUENCING (when you're ready to implement)

1. `yt_auth.py` + a manual test upload (private) — prove the OAuth/upload path works end-to-end first, before building anything else on top of it.
2. `yt-metadata` skill + human gate — this is highest-leverage per unit of effort and has zero platform-approval dependency.
3. `yt-upload` + `yt-playlist` — wire metadata output into an actual scheduled private upload.
4. `yt-analytics` — start collecting data early even before other automation is done, so you have a baseline before you start optimizing.
5. `yt-thumbnail` — lower priority than metadata; thumbnails benefit most from human creative judgment anyway.
6. Telegram cross-post (highest ROI, lowest API friction) — build before attempting Instagram/TikTok. Given the language mix, this is worth prioritizing even higher than "generic step 6" for the **fa and ru channels specifically**, since Telegram is their primary distribution surface, not a nice-to-have (see the Language-Specific Audience Targeting Guide).
7. Instagram/TikTok — only after Telegram pipeline is proven, given the app-review overhead. Lower priority for fa/ru given Instagram's restricted status in both Iran and Russia — don't invest setup time there for those two channels specifically.
8. **VK integration** — not covered in Section 3.6 above; add as a research spike before committing build time, since VK's API and developer-approval process are unfamiliar territory relative to Meta/Telegram/TikTok. Relevant only for the `ru` channel.

---

## 8. NOT COVERED HERE (intentionally out of scope for automation)

- End screens/cards (Section 3.5) — needs a fresh API-capability check
- Community Tab posts — no reliable public API for this; keep manual
- Comment moderation — recommend YouTube Studio's built-in held-for-review word filters rather than a custom bot, at least initially
- Thumbnail *final* selection — keep human judgment in the loop permanently, not just during a bootstrap phase
