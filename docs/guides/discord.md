# Discord promotion guide

Automatable via an **incoming webhook** (a single POST; no bot/OAuth needed).

## One-time setup
1. In your Discord server: **Channel settings → Integrations → Webhooks → New Webhook**.
2. Copy the webhook URL.
3. Put it in `.env` under the `DISCORD` prefix:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
   ```
4. Set `platforms.discord.enabled: true` in `promotion.config.json`.

## Posting
1. `vid_cli.py distribute promote queue <id>` (template: `**{title}**\n{summary}\n{url}`).
2. Grant `promotion_review` (human).
3. Dry-run: `vid_cli.py distribute promote publish <id>`.
4. Live: `VIDTRANS_PUBLISH_ENABLED=1 VIDTRANS_EXTERNAL_WRITES=enabled vid_cli.py distribute promote publish <id> --live`.

## Notes
- The webhook posts to the one channel it was created for. Discord auto-embeds the video link.
- Anyone with the webhook URL can post to that channel — treat it as a secret.
