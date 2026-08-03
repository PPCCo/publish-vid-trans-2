# Distribution Extension Plan — YouTube Upload & Promotion (`publish-vid-trans`)

> **Status: Phase 6 — separately authorized.** This extension is **out of scope** for the
> core `publish-vid-trans` build (Phases 0–5), which stops at `READY_FOR_REVIEW` and
> produces upload-ready packages without touching any external platform. Nothing here is
> built until Phases 0–5 are complete and stable **and** you explicitly authorize live
> external-write credentials. It is factored into its own file precisely because it adds a
> different risk class (live OAuth tokens, platform ToS, quota) than the core framework.
>
> This was extracted from §12 of `publish-vid-trans-plan.md`. The CLI is `vid_cli.py`
> (package `video_translation_house/`), consistent with the core build.

Everything in the core plan (§1–§11) stops at `READY_FOR_REVIEW` and produces upload-ready
packages without touching any external platform. This extension extends the lifecycle to
actually publish and promote, while keeping the same governing principle as the rest of the
framework: **nothing leaves the repository without a human-bound approval record tied to an
exact artifact hash.**

## Prerequisites (before any of this is built)

- Phases 0–5 complete: a project can reach `READY_FOR_REVIEW` with validated final
  deliverables and a full artifact/provenance chain.
- `rights_status` for the project is set to a distributable value by a human (not
  `unreviewed` / `do-not-distribute`).
- Explicit human authorization to enable live external writes (a new env flag, e.g.
  `VIDTRANS_PUBLISH_ENABLED`, defaulting off, gating the one sanctioned upload module —
  mirroring the ingest fetch flag).

## E.1 Extended lifecycle

```
... FINAL_APPROVAL
-> PLATFORM_PACKAGING
-> RELEASE_AUTHORIZATION     # human-only; binds an approver to the exact final video hash
-> YOUTUBE_UPLOAD             # upload-youtube skill
-> PROMOTION_QUEUE            # promote-content skill drafts platform-specific posts
-> PROMOTION_REVIEW           # human approves the queued posts
-> PROMOTION_PUBLISHED        # automated posts fire; manual-platform tasks handed off as a checklist
-> MONITORING
```

`video-release-authorize` and `video-promote-approve` are **human-only** skills
(`disable-model-invocation: true`) — Claude can prepare everything up to that point but
cannot invoke them, exactly as with the core gate skills.

## E.2 `upload-youtube` skill

**Global channel config** (`config/channels.config.json`, gitignored credentials, tracked
structure):

```json
{
  "channels": [
    { "channelId": "UCxxxxEN1", "language": "en", "displayName": "Main Channel EN",
      "default": true, "credentials_ref": "YT_EN_MAIN", "privacy_default": "unlisted" },
    { "channelId": "UCxxxxEN2", "language": "en", "displayName": "Backup Channel EN",
      "default": false, "credentials_ref": "YT_EN_ALT" },
    { "channelId": "UCxxxxAR", "language": "ar", "displayName": "Arabic Channel",
      "default": true, "credentials_ref": "YT_AR_MAIN" },
    { "channelId": "UCxxxxFA", "language": "fa", "displayName": "Farsi Channel",
      "default": true, "credentials_ref": "YT_FA_MAIN" }
  ]
}
```

- `credentials_ref` is a name pointing at an OAuth2 client + refresh token stored outside
  the repo (env vars or a secrets manager) — never inline in JSON. Initial OAuth consent
  for each channel is a one-time **manual** human action (Google's consent screen); it
  cannot and should not be automated.
- Resolution logic: a project targeting language `L` uploads to the channel where
  `language == L and default == true`. A per-project override
  (`project.yaml: distribution.youtube.channelId`) takes precedence. `vid_cli.py framework
  validate` rejects a config with two `default:true` channels for the same language, and
  rejects a project override pointing at a `channelId` absent from the global config.
- Upload steps: resolve channel → load credentials → build metadata (templated
  title/description/tags per language, category, default `privacyStatus: unlisted` unless
  the release authorization explicitly set `public`) → resumable `videos.insert` upload
  (YouTube Data API v3) → upload the caption track via `captions.insert` (in addition to
  any burned-in captions — this makes the translation searchable and toggleable, not just
  baked into pixels) → optional `thumbnails.set` → optional `playlistItems.insert` into a
  per-language "latest releases" playlist.
- Idempotency: skip re-upload if this exact artifact hash was already recorded as uploaded
  to this channel; record `videoId`, channel, timestamp, and privacy status in
  `artifacts/manifest.json` and `events.ndjson`.
- Quota discipline: a video upload costs ~1600 of the default 10,000 daily API units per
  Google Cloud project; if several channels share one Cloud project's API registration,
  budget cumulative daily uploads across all of them, not per channel.

## E.3 `promote-content` skill

Fires only after `PROMOTION_REVIEW` approval, and only for videos that already have a live
`YOUTUBE_UPLOAD` record to link to.

**Config** (`config/promotion.config.json`): per-platform enable flags, per-language target
handles/channels/pages, message templates, and rate limits (daily cap per platform, minimum
interval between posts).

**Automatable via official APIs, on surfaces you own — safe to include in the automated queue:**

- **Telegram** (Bot API, posting to your own channel) — no meaningful restriction, straightforward.
- **Discord** (webhook into your own server) — same.
- **RSS/Atom feed regeneration** — entirely yours; also useful as a podcast feed for the dubbed audio track.
- **YouTube** — add to a playlist automatically; a Community-tab post if your channel
  currently has API access to that surface (historically limited/allowlisted, so treat as
  "automate if available, else fold into the manual list").
- **X (Twitter)** — posting to an account you own via API v2 is permitted, but confirm
  current API tier/rate limits before depending on it for regular cadence; free-tier volume
  has changed repeatedly and may not cover daily posting.
- **Meta — Facebook Page / linked Instagram Business account you administer** — Graph API
  supports scheduled posting to Pages and Business accounts you own; requires app review for
  some permission scopes.
- **Mailing list / newsletter** to opted-in subscribers via your ESP's API — fully automatable.

**Not recommended to automate — provide a manual guide instead:**

- **Reddit.** Reddit's site-wide content policy and the overwhelming majority of individual
  subreddit rules treat repeated self-promotional posting — even of good content — as spam;
  the informal but widely-enforced norm is that no more than ~10% of an account's activity
  should be its own links, accounts that read as broadcast channels get shadowbanned or
  suspended, and moderators/AutoModerator actively hunt bot-like posting patterns.
  Automating Reddit submissions would likely burn the account and possibly violate Reddit's
  platform rules, so this framework should **not** attempt it. Instead, ship a
  `PROMOTION_GUIDE_REDDIT.md`: a curated list of candidate subreddits per language/topic, a
  running self-promotion budget tracker (so a human stays under the 1-in-10 norm), a note to
  build genuine comment history before posting links, required-flair reminders per
  subreddit, and a post-template that's explicitly a **starting point for a human to
  personalize**, not text to paste verbatim.
- **Facebook groups/profiles you don't administer, personal Instagram, WhatsApp broadcast**
  — the Graph API doesn't support posting into groups or accounts you don't own, and any
  workaround (browser automation, scraping) breaks platform ToS. Manual only.
- **Topic-specific forums without a public API** (e.g. Islamic-studies or academic
  discussion forums, plausible for this content) — manual only; worth a short per-site
  posting guide the same way as Reddit.

**Guardrails that apply regardless of platform:**

- Generate a distinct message per platform rather than copy-pasting one blob everywhere —
  identical cross-posted text is itself a spam signal on several platforms' detection systems.
- Every queued post goes through a lightweight human review batch before firing, even though
  the video itself was already verified — a mistranslated or oddly-toned promo line is far
  more visible, and harder to walk back, than a caption typo.
- Per-platform, per-channel rate limits and cooldowns to stay well clear of automated spam
  detection thresholds.
- Full audit trail (`promotion-manifest.json` + `events.ndjson`) of what was posted where,
  when, and whether it was fired automatically or completed manually off the checklist.
- No auto-DM, auto-comment-and-run, or vote/engagement manipulation on any platform, ever —
  that crosses from promotion into manipulation and is against every relevant platform's
  policy without exception, regardless of how "automatable" the underlying API technically
  makes it.
