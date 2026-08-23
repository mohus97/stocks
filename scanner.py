#!/usr/bin/env python3
import os
import time
import json
import math
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import requests
import yfinance as yf
import yaml
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')


@dataclass
class Signal:
    symbol: str
    label: str
    market_type: str
    side: str
    price: float
    stop: float
    tp1: float
    tp2: float
    score: float
    reason: str
    risk_gbp: float
    suggested_exposure_gbp: float | None
    timestamp: str


def load_config():
    with open(BASE / 'config.yaml', 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    prev_close = df['Close'].shift(1)
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - prev_close).abs(),
        (df['Low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def session_vwap(df):
    # VWAP is only meaningful when volume is supplied. FX/index feeds may not have usable volume.
    vol = df['Volume'].fillna(0)
    if vol.sum() <= 0:
        return pd.Series(np.nan, index=df.index)
    typical = (df['High'] + df['Low'] + df['Close']) / 3
    dates = pd.Series(df.index.date, index=df.index)
    pv = typical * vol
    return pv.groupby(dates).cumsum() / vol.groupby(dates).cumsum().replace(0, np.nan)


def normalize_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance can return ticker level even for one symbol
        if len(df.columns.levels[1]) == 1:
            df.columns = df.columns.get_level_values(0)
        else:
            df.columns = [c[0] for c in df.columns]
    return df


def fetch(symbol):
    """Fetch 5-minute candles with a small fallback for feeds that initialise slowly."""
    attempts = [
        dict(period='5d', interval='5m'),
        dict(period='1d', interval='5m'),
    ]
    for i, params in enumerate(attempts, start=1):
        try:
            df = yf.download(
                symbol,
                period=params['period'],
                interval=params['interval'],
                progress=False,
                auto_adjust=True,
                prepost=False,
                threads=False,
            )
            df = normalize_columns(df)
            if df is not None:
                df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])
                if len(df) >= 60:
                    return df
        except Exception as e:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: fetch attempt {i} failed: {e}')
        if i < len(attempts):
            time.sleep(1.0)
    return None


def candle_age_minutes(df):
    if df is None or df.empty:
        return None
    ts = pd.Timestamp(df.index[-1])
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    else:
        ts = ts.tz_convert('UTC')
    now = pd.Timestamp.now(tz='UTC')
    return (now - ts).total_seconds() / 60.0


def data_is_fresh(df, item, cfg):
    """Avoid alerts from closed markets while allowing modest FX feed lag."""
    age_minutes = candle_age_minutes(df)
    if age_minutes is None:
        return False

    scanner_cfg = cfg.get('scanner', {})
    market_type = item.get('type', 'stock')
    if market_type == 'forex':
        # Yahoo's Sunday/opening FX feed can lag a few bars. Friday data is still
        # far too old to pass this check, so this does not turn stale weekends into alerts.
        max_age = float(scanner_cfg.get('forex_max_candle_age_minutes', 60))
    elif market_type == 'index':
        max_age = float(scanner_cfg.get('index_max_candle_age_minutes', 30))
    elif market_type == 'metal':
        # Gold/silver futures trade nearly around the clock on weekdays, but the
        # Yahoo feed can occasionally lag a few bars around session transitions.
        max_age = float(scanner_cfg.get('metal_max_candle_age_minutes', 45))
    else:
        max_age = float(scanner_cfg.get('max_candle_age_minutes', 20))

    return -2 <= age_minutes <= max_age


def score_signal(df, item, cfg):
    d = df.copy()
    d['EMA9'] = ema(d['Close'], 9)
    d['EMA20'] = ema(d['Close'], 20)
    d['EMA50'] = ema(d['Close'], 50)
    d['RSI'] = rsi(d['Close'], 14)
    d['ATR'] = atr(d, 14)
    d['VWAP'] = session_vwap(d)
    d['HH20'] = d['High'].shift(1).rolling(20).max()
    d['LL20'] = d['Low'].shift(1).rolling(20).min()

    if 'Volume' in d.columns and d['Volume'].fillna(0).sum() > 0:
        d['VOLAVG20'] = d['Volume'].rolling(20).mean()
        d['RVOL'] = d['Volume'] / d['VOLAVG20'].replace(0, np.nan)
    else:
        d['RVOL'] = np.nan

    row = d.iloc[-1]
    prev = d.iloc[-2]
    if pd.isna(row['ATR']) or row['ATR'] <= 0 or pd.isna(row['RSI']):
        return None

    price = float(row['Close'])
    atr_v = float(row['ATR'])
    market_type = item.get('type', 'stock')
    min_score = float(cfg['scanner']['minimum_score'])

    long_score = 0.0
    long_reasons = []
    if row['EMA9'] > row['EMA20'] > row['EMA50']:
        long_score += 2.0; long_reasons.append('EMA trend up')
    elif row['EMA9'] > row['EMA20']:
        long_score += 1.0; long_reasons.append('short trend up')
    if 55 <= row['RSI'] <= 72:
        long_score += 1.0; long_reasons.append(f"RSI {row['RSI']:.0f}")
    if price > row['HH20']:
        long_score += 2.0; long_reasons.append('20-bar breakout')
    elif price > prev['High']:
        long_score += 0.5
    if not pd.isna(row['VWAP']) and price > row['VWAP']:
        long_score += 1.0; long_reasons.append('above VWAP')
    if not pd.isna(row['RVOL']) and row['RVOL'] >= 1.5:
        long_score += 1.5; long_reasons.append(f"RVOL {row['RVOL']:.1f}x")
    elif market_type in ('forex', 'index', 'metal'):
        long_score += 0.5  # volume can be unavailable/patchy on these Yahoo feeds
    if price > prev['Close'] and (price - prev['Close']) > 0.20 * atr_v:
        long_score += 0.5

    short_score = 0.0
    short_reasons = []
    if row['EMA9'] < row['EMA20'] < row['EMA50']:
        short_score += 2.0; short_reasons.append('EMA trend down')
    elif row['EMA9'] < row['EMA20']:
        short_score += 1.0; short_reasons.append('short trend down')
    if 28 <= row['RSI'] <= 45:
        short_score += 1.0; short_reasons.append(f"RSI {row['RSI']:.0f}")
    if price < row['LL20']:
        short_score += 2.0; short_reasons.append('20-bar breakdown')
    elif price < prev['Low']:
        short_score += 0.5
    if not pd.isna(row['VWAP']) and price < row['VWAP']:
        short_score += 1.0; short_reasons.append('below VWAP')
    if not pd.isna(row['RVOL']) and row['RVOL'] >= 1.5:
        short_score += 1.5; short_reasons.append(f"RVOL {row['RVOL']:.1f}x")
    elif market_type in ('forex', 'index', 'metal'):
        short_score += 0.5
    if price < prev['Close'] and (prev['Close'] - price) > 0.20 * atr_v:
        short_score += 0.5

    side = None
    score = max(long_score, short_score)
    if score < min_score:
        return None
    if long_score > short_score:
        side = 'LONG'; reasons = long_reasons
    elif short_score > long_score:
        side = 'SHORT'; reasons = short_reasons
    else:
        return None

    # ATR + recent swing protective stop. Wider of the two avoids ultra-tight stops.
    recent_low = float(d['Low'].tail(8).min())
    recent_high = float(d['High'].tail(8).max())
    stop_mult = float(cfg['risk']['atr_stop_multiple'])
    if side == 'LONG':
        stop = min(price - stop_mult * atr_v, recent_low - 0.10 * atr_v)
        risk_per_unit = price - stop
        tp1 = price + float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = price + float(cfg['risk']['tp2_r_multiple']) * risk_per_unit
    else:
        stop = max(price + stop_mult * atr_v, recent_high + 0.10 * atr_v)
        risk_per_unit = stop - price
        tp1 = price - float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = price - float(cfg['risk']['tp2_r_multiple']) * risk_per_unit

    if risk_per_unit <= 0:
        return None

    account = float(cfg['risk']['account_cash_gbp'])
    risk_pct = float(cfg['risk']['risk_per_trade_pct']) / 100.0
    risk_gbp = account * risk_pct
    stop_pct = risk_per_unit / price

    # This is cash-equivalent exposure, not CFD contract sizing.
    max_exposure = account * float(cfg['risk']['max_cash_exposure_pct']) / 100.0
    suggested = min(max_exposure, risk_gbp / stop_pct) if stop_pct > 0 else None

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    return Signal(
        symbol=item['symbol'],
        label=item.get('name', item['symbol']),
        market_type=market_type,
        side=side,
        price=price,
        stop=float(stop),
        tp1=float(tp1),
        tp2=float(tp2),
        score=float(score),
        reason=', '.join(reasons[:4]),
        risk_gbp=float(risk_gbp),
        suggested_exposure_gbp=float(suggested) if suggested else None,
        timestamp=now,
    )


def decimals_for_price(x):
    if x >= 1000: return 1
    if x >= 100: return 2
    if x >= 1: return 4
    return 5


def format_signal(s: Signal):
    dec = decimals_for_price(s.price)
    emoji = '🟢' if s.side == 'LONG' else '🔴'
    exposure = f"£{s.suggested_exposure_gbp:,.0f} cash-equivalent" if s.suggested_exposure_gbp else 'size to risk budget'
    cfd_note = ''
    if s.market_type in ('forex', 'index', 'metal') or s.side == 'SHORT':
        cfd_note = '\n⚠️ CFD: set units so stop-out ≈ risk budget; leverage can magnify losses.'
    return (
        f"⚡ 5-MIN SETUP — {s.label} ({s.symbol})\n"
        f"{emoji} {s.side} — ENTER/CONFIRM near {s.price:.{dec}f}\n"
        f"🛑 Stop: {s.stop:.{dec}f}\n"
        f"🎯 TP1: {s.tp1:.{dec}f}\n"
        f"🎯 TP2: {s.tp2:.{dec}f}\n"
        f"⭐ Score: {s.score:.1f}/8\n"
        f"💷 Risk budget: ~£{s.risk_gbp:.0f}\n"
        f"📦 Exposure guide: {exposure}\n"
        f"Reason: {s.reason}"
        f"{cfd_note}\n"
        f"🕒 {s.timestamp}"
    )



def telegram_notify(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        print('Telegram: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
        return False
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': message},
            timeout=15,
        )
        if not r.ok:
            # Safe to log status/body; the bot token is never printed.
            print(f'Telegram send failed: HTTP {r.status_code} — {r.text[:250]}')
            return False
        return True
    except Exception as e:
        print(f'Telegram send failed: {e}')
        return False


def state_path(): return BASE / '.scanner_state.json'


def load_state():
    p = state_path()
    if not p.exists(): return {}
    try: return json.loads(p.read_text())
    except Exception: return {}


def save_state(s):
    state_path().write_text(json.dumps(s, indent=2))


def signature(sig: Signal):
    # Bucket price a little so repeated bars don't spam identical signals.
    bucket = round(sig.price, decimals_for_price(sig.price))
    raw = f'{sig.symbol}|{sig.side}|{bucket}|{round(sig.score,1)}'
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def scan_once(cfg):
    state = load_state()
    cooldown_min = int(cfg['scanner']['cooldown_minutes'])
    now_ts = time.time()
    alerts = []

    for item in cfg['watchlist']:
        symbol = item['symbol']
        try:
            df = fetch(symbol)
            if df is None:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: insufficient data')
                continue
            if not data_is_fresh(df, item, cfg):
                age = candle_age_minutes(df)
                age_txt = f'{age:.1f}m old' if age is not None else 'unknown age'
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: market/data stale ({age_txt}); skip')
                continue
            sig = score_signal(df, item, cfg)
            if not sig:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: no setup')
                continue

            sig_id = signature(sig)
            key = f'{symbol}:{sig.side}'
            last = state.get(key, {})
            too_soon = (now_ts - last.get('time', 0)) < cooldown_min * 60
            same = last.get('signature') == sig_id
            if too_soon and same:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: setup held; cooldown')
                continue

            text = format_signal(sig)
            print('\n' + text + '\n')
            if cfg['notifications'].get('telegram', True):
                telegram_notify(text)

            state[key] = {'time': now_ts, 'signature': sig_id}
            alerts.append(sig)
            time.sleep(0.3)
        except Exception as e:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: ERROR {e}')

    save_state(state)
    return alerts


def seconds_to_next_5m():
    now = time.time()
    step = 300
    next_t = (math.floor(now / step) + 1) * step + 3  # 3 sec after candle boundary
    return max(5, next_t - now)


def main():
    cfg = load_config()
    print('Market Scanner started.')
    print(f"Watchlist: {len(cfg['watchlist'])} instruments | min score {cfg['scanner']['minimum_score']} | interval 5m")
    print('Press Ctrl+C to stop.\n')

    if cfg.get('notifications', {}).get('telegram', True):
        account = float(cfg.get('risk', {}).get('account_cash_gbp', 1000))
        risk_pct = float(cfg.get('risk', {}).get('risk_per_trade_pct', 1.0))
        risk_gbp = account * risk_pct / 100.0
        startup_message = (
            '✅ Market Scanner Online\n'
            '🔄 Scanning every 5 minutes\n'
            f'📊 Watchlist: {len(cfg["watchlist"])} instruments\n'
            f'💷 Account basis: £{account:,.0f}\n'
            f'🛑 Planned risk/trade: ~£{risk_gbp:,.0f}\n'
            '📱 Telegram alerts active'
        )
        if telegram_notify(startup_message):
            print('Telegram startup test: OK')
        else:
            print('Telegram startup test: FAILED — check Railway variables.')

    # Immediate scan on startup, then align to 5-minute bars.
    scan_once(cfg)
    while True:
        try:
            wait = seconds_to_next_5m()
            print(f'Next scan in {int(wait)}s...')
            time.sleep(wait)
            scan_once(cfg)
        except KeyboardInterrupt:
            print('\nStopped.')
            break


if __name__ == '__main__':
    main()
