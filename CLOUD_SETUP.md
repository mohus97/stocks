# 5-minute Telegram Market Scanner — Cloud Setup

This version is designed to stay online when your Mac is off. It runs continuously in a cloud worker and scans the configured watchlist every 5 minutes.

## Current settings
- Account reference: £1,000
- Planned risk per setup: ~1% / £10
- Alerts: Telegram only
- Interval: every 5 minutes
- Duplicate cooldown: 30 minutes
- Stale-market guard: no alerts when the latest 5-minute candle is older than 20 minutes

## Before deploying
You need two Telegram values:

1. `TELEGRAM_BOT_TOKEN`
2. `TELEGRAM_CHAT_ID`

Never commit the token to GitHub. Put both values into Railway's Variables/Secrets section.

### Get your Telegram bot token
Create a bot with Telegram's official `@BotFather`, then copy the bot token it gives you.

### Get your chat ID
Send your new bot a message such as `hello`.
On a computer with Python installed, you can run:

```bash
export TELEGRAM_BOT_TOKEN='YOUR_TOKEN'
python3 telegram_setup.py
```

It prints the most recent chat ID. Copy that number.

## Deploy to Railway

### Option A — GitHub (easiest ongoing maintenance)
1. Create a private GitHub repository.
2. Upload all files from this folder. Do NOT upload a `.env` file containing your token.
3. In Railway, create a new project and choose **Deploy from GitHub Repo**.
4. Select the repository.
5. In the Railway service, open **Variables** and add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID
6. Deploy/redeploy.
7. Open Railway logs. You should see `Market Scanner started.` and 5-minute scans.

The included `Dockerfile` and `railway.json` are already configured; no custom start command is needed.

### Option B — Railway CLI
If you already use Railway CLI, create a project from this folder, add the two environment variables above, and deploy. The Dockerfile is the startup definition.

## Test Telegram before leaving it running
The scanner only alerts when a setup passes the threshold, so silence can be normal. To verify your Telegram details, run `telegram_setup.py` locally and confirm it finds your chat ID.

## Change aggressiveness
Edit `config.yaml`:
- `minimum_score`: lower = more alerts / noisier; higher = fewer / stricter.
- `risk_per_trade_pct`: 1.00 means the alert's risk budget is ~£10 on £1,000.
- `cooldown_minutes`: suppresses repeated identical signals.

I recommend keeping the minimum score at 5.0 initially and reviewing the signals before lowering it.

## Important limitations
- The scanner uses Yahoo Finance/yfinance 5-minute data, not Trading 212's execution feed. Prices can be delayed, missing, or differ from the broker spread.
- It does not place trades. Verify price, spread, market status, and stop distance in Trading 212 before entering.
- The exposure shown for CFDs is only a cash-equivalent guide. CFD leverage and contract sizing require checking the broker ticket so the loss at the stop matches the stated risk budget.
- A signal is not a guaranteed profitable trade. High-volatility markets can gap past stops.

## Stop it
Pause or delete the Railway service. Telegram will stop receiving scanner alerts.
