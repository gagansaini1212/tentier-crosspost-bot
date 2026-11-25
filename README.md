# Telegram Multi-Platform Cross-Poster Bot

Send an image + caption (or text) to your Telegram bot in DM, toggle target platforms with inline buttons, and publish everywhere at once.

## Supported Platforms
- **Telegram Channel**
- **Discord** (Webhook)
- **X / Twitter** (Tweepy)
- **Bluesky** (`atproto`)
- **Mastodon**
- **LinkedIn**
- **Facebook Page**
- **Instagram Business** (via Meta Graph API + Cloudinary unsigned preset)

## Setup & Run
1. Create a bot via `@BotFather`.
2. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`, `ADMIN_USER_IDS`, and credentials for whichever platforms you want to enable.
3. Run:
   ```bash
   .venv/bin/python bot.py
   ```
