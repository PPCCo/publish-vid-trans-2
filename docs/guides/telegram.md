# Telegram promotion guide

Automatable via the Telegram Bot API (`sendMessage`). Plain HTTPS; no SDK.

## One-time setup
1. Create a bot with [@BotFather](https://t.me/BotFather); note the **bot token**.
2. Add the bot to your channel/group as an **administrator** (so it can post).
3. Identify the target chat: a public channel `@username`, or a numeric chat id.
4. Put them in `.env` under the `TELEGRAM` prefix:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-...
   TELEGRAM_CHANNEL=@your_channel   # or -1001234567890
   ```
5. Set `platforms.telegram.enabled: true` in `promotion.config.json`.

## Posting
1. `vid_cli.py distribute promote queue <id>` (template: `{title}\n\n{summary}\n\n{url}`).
2. Grant `promotion_review` (human).
3. Dry-run: `vid_cli.py distribute promote publish <id>`.
4. Live: `VIDTRANS_PUBLISH_ENABLED=1 VIDTRANS_EXTERNAL_WRITES=enabled vid_cli.py distribute promote publish <id> --live`.

## Notes
- The bot posts to exactly one configured chat; for multiple channels, run per channel with
  different `TELEGRAM_CHANNEL` values.
