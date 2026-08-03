# X / Twitter promotion guide

Automatable via `net/publish.py` (X API v2 `POST /2/tweets`, with optional media via the v1.1
upload endpoint). Dry-run by default.

## One-time setup
1. Create a project + app in the X Developer Portal with **Read and Write** permissions.
2. Generate: API key + secret (consumer keys) and an access token + secret for the posting
   account (OAuth 1.0a user context, required for write).
3. Put them in `.env` under the `X` prefix (matches `credentials_ref` in `promotion.config.json`):
   ```
   X_API_KEY=...
   X_API_SECRET=...
   X_ACCESS_TOKEN=...
   X_ACCESS_SECRET=...
   ```
4. Set `platforms.x.enabled: true` in `.claude/config/promotion.config.json` when ready.

## Posting
1. Draft & queue: `vid_cli.py distribute promote queue <id>` — writes a distinct X message from
   the template `{title}\n\n{summary}\n\nWatch: {url}\n{hashtags}`.
2. Human approval: grant `promotion_review` (see `video-promote-approve`).
3. Dry-run: `vid_cli.py distribute promote publish <id>` (returns the tweet body).
4. Live: `VIDTRANS_PUBLISH_ENABLED=1 VIDTRANS_EXTERNAL_WRITES=enabled vid_cli.py distribute promote publish <id> --live --advance`.

## Notes
- Keep within the free-tier write limits (`rate_limit_per_hour` in config is a self-imposed soft
  ceiling recorded in the manifest).
- Thread idea: post the chapter list as replies for long talks — draft these manually in the
  message text; the uploader posts a single tweet per platform.
