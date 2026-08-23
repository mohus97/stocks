#!/usr/bin/env python3
import os
import requests
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
if not token:
    raise SystemExit('Add TELEGRAM_BOT_TOKEN to .env first.')

r = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=15)
r.raise_for_status()
data = r.json()
results = data.get('result', [])
if not results:
    raise SystemExit('No messages found. Open your bot in Telegram, press Start, send it "hello", then run this again.')

seen = []
for update in results:
    msg = update.get('message') or update.get('channel_post') or {}
    chat = msg.get('chat', {})
    cid = chat.get('id')
    if cid is not None and cid not in seen:
        seen.append(cid)
        print(f"CHAT ID: {cid} | name: {chat.get('title') or chat.get('username') or chat.get('first_name') or 'unknown'}")
