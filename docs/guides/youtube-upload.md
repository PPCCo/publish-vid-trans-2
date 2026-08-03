# YouTube upload guide

This file doubles as the **template** the per-project renderer fills: when you run
`vid_cli.py distribute package <id>`, tokens like `{{TITLE}}` below are replaced with this
video's real metadata and the result is written to
`projects/<id>/distribution/guides/youtube-<lang>.md`. In this generic copy the tokens are shown
literally.

## This upload (per-project fields)
- **Title:** `{{TITLE}}`
- **Language:** `{{LANGUAGE}}`
- **Privacy (default):** `{{PRIVACY}}`
- **Channel:** `{{CHANNEL_ID}}`
- **Video file:** `{{VIDEO_PATH}}`
- **Bound hash:** `{{VIDEO_SHA256}}`
- **Description:**

```
{{DESCRIPTION}}
```

## One-time setup (credentials)
1. In Google Cloud Console, create a project and enable the **YouTube Data API v3**.
2. Create an **OAuth 2.0 Client ID** (Desktop app). Note the client ID + secret.
3. Run the consent flow once to obtain a **refresh token** for the target channel
   (scopes: `youtube.upload`, `youtube.force-ssl`).
4. Put them in your local `.env` (never commit):
   ```
   YT_DEFAULT_CLIENT_ID=...
   YT_DEFAULT_CLIENT_SECRET=...
   YT_DEFAULT_REFRESH_TOKEN=...
   ```
   The env-var prefix (`YT_DEFAULT`) matches the channel's `credentials_ref` in
   `.claude/config/channels.config.json`. Set the real `channelId` there.

## Publishing
1. **Authorize the release (human):** grant `release_authorization` bound to `{{VIDEO_SHA256}}`
   and transition to `YOUTUBE_UPLOAD` — see the `video-release-authorize` skill.
2. **Dry-run** (no network): `vid_cli.py distribute youtube <id> --language {{LANGUAGE}}`.
   Review the `request_preview`.
3. **Live upload:** with both flags set —
   ```
   VIDTRANS_PUBLISH_ENABLED=1 VIDTRANS_EXTERNAL_WRITES=enabled \
     vid_cli.py distribute youtube <id> --language {{LANGUAGE}} --live --advance
   ```
   The uploader performs a resumable `videos.insert`, then `captions.insert` for each SRT and
   (if configured) adds the video to a playlist. It records `video_id`/URL in
   `distribution/upload-manifest.json`.

## Notes
- A `videos.insert` costs ~1600 quota units; the daily default quota is 10,000. Plan accordingly.
- New API-uploaded videos are locked to **private** until your project is API-verified by
  YouTube; keep `privacy_default: private` and flip visibility manually after review, or complete
  verification.
- Chapters: the description already contains the `0:00`-anchored timecode block, which YouTube
  parses into chapter markers automatically.
