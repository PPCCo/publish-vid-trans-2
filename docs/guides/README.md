# Distribution & promotion guides

How-to guides for publishing a finished, **rights-cleared** video edition and promoting it.
The framework itself stops at `READY_FOR_REVIEW` and never publishes on its own; everything here
is a separate, human-authorized step (Phase 6). Nothing publishes unless **both**
`VIDTRANS_PUBLISH_ENABLED=1` and `VIDTRANS_EXTERNAL_WRITES=enabled` are set, credentials are
present, and the human release/promotion gates have been granted.

## Automated platforms (API-capable via the sanctioned uploader)
These have live code paths in `net/publish.py` (dry-run by default). Each also gets a
**per-project** guide rendered into `projects/<id>/distribution/guides/` with the video's real
title/description when you run `distribute package`.

| Platform | Guide | Credentials (env prefix) |
|---|---|---|
| YouTube  | [youtube-upload.md](youtube-upload.md) | `YT_<CHANNEL>_CLIENT_ID/_CLIENT_SECRET/_REFRESH_TOKEN` |
| X / Twitter | [x-twitter.md](x-twitter.md) | `X_API_KEY/_API_SECRET/_ACCESS_TOKEN/_ACCESS_SECRET` |
| Telegram | [telegram.md](telegram.md) | `TELEGRAM_BOT_TOKEN/_CHANNEL` |
| Discord  | [discord.md](discord.md) | `DISCORD_WEBHOOK_URL` |

## Manual platforms (checklist + how-to only)
No API automation; `distribute promote queue` emits a checklist item pointing here.

- [reddit.md](reddit.md) · [instagram.md](instagram.md) · [tiktok.md](tiktok.md) ·
  [facebook.md](facebook.md) · [linkedin.md](linkedin.md) · [rumble.md](rumble.md) ·
  [odysee.md](odysee.md) · [peertube.md](peertube.md) · [podcast-rss.md](podcast-rss.md)

## The two flags (defense in depth)
- `VIDTRANS_PUBLISH_ENABLED=1` — module-level switch inside `net/publish.py`.
- `VIDTRANS_EXTERNAL_WRITES=enabled` — hook-level switch that lets bash API verbs past
  `pre_tool_policy.py`.

Set credentials in a local `.env` (see [`.env.example`](../../.env.example)); never commit secrets.
