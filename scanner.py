#!/usr/bin/env python3
import os
import time
import json
import math
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import requests
import yfinance as yf
import yaml
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
UK = ZoneInfo('Europe/London')


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
    if 'Volume' not in df.columns:
        return pd.Series(np.nan, index=df.index)
    vol = pd.to_numeric(df['Volume'], errors='coerce').fillna(0)
    if vol.sum() <= 0:
        return pd.Series(np.nan, index=df.index)
    typical = (df['High'] + df['Low'] + df['Close']) / 3
    dates = pd.Series(df.index.date, index=df.index)
    pv = typical * vol
    return pv.groupby(dates).cumsum() / vol.groupby(dates).cumsum().replace(0, np.nan)


def normalize_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        if len(df.columns.levels[1]) == 1:
            df.columns = df.columns.get_level_values(0)
        else:
            df.columns = [c[0] for c in df.columns]
    return df


def fetch_yahoo(symbol):
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
            if df is not None and not df.empty:
                for col in ['Open', 'High', 'Low', 'Close']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                if 'Volume' not in df.columns:
                    df['Volume'] = 0.0
                df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])
                if len(df) >= 65:
                    return df
        except Exception as e:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: Yahoo fetch attempt {i} failed: {e}')
        if i < len(attempts):
            time.sleep(1.0)
    return None


def twelve_data_active(cfg):
    """Stay inside the free 800-credit/day allowance while covering the main day-trading window."""
    now = datetime.now(UK)
    weekday = now.weekday()  # Mon=0 ... Sun=6
    hour = now.hour + now.minute / 60.0
    sc = cfg.get('scanner', {})
    start = float(sc.get('twelvedata_active_start_hour_uk', 7))
    end = float(sc.get('twelvedata_active_end_hour_uk', 18))
    sunday_start = float(sc.get('twelvedata_sunday_start_hour_uk', 22))
    if weekday <= 4:  # Mon-Fri
        return start <= hour < end
    if weekday == 6:  # Sunday evening reopen
        return hour >= sunday_start
    return False


def fetch_twelvedata(symbol, cfg):
    key = os.getenv('TWELVE_DATA_API_KEY', '').strip()
    if not key:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: TWELVE_DATA_API_KEY missing; skip')
        return None
    if not twelve_data_active(cfg):
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: Twelve Data session paused to protect free API quota')
        return None
    try:
        r = requests.get(
            'https://api.twelvedata.com/time_series',
            params={
                'symbol': symbol,
                'interval': '5min',
                'outputsize': 120,
                'timezone': 'UTC',
                'apikey': key,
            },
            timeout=20,
        )
        data = r.json()
        if not r.ok or data.get('status') == 'error' or 'values' not in data:
            msg = data.get('message', r.text[:180]) if isinstance(data, dict) else r.text[:180]
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: Twelve Data error: {msg}')
            return None
        rows = data['values']
        if len(rows) < 65:
            return None
        df = pd.DataFrame(rows)
        rename = {'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'}
        df = df.rename(columns=rename)
        if 'Volume' not in df.columns:
            df['Volume'] = 0.0
        for col in ['Open','High','Low','Close','Volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        idx = pd.to_datetime(df['datetime'], utc=True, errors='coerce')
        df.index = idx
        df = df.drop(columns=['datetime'], errors='ignore')
        df = df.sort_index().dropna(subset=['Open','High','Low','Close'])
        return df if len(df) >= 65 else None
    except Exception as e:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: Twelve Data fetch failed: {e}')
        return None


def fetch_item(item, cfg):
    provider = item.get('provider', 'yahoo')
    data_symbol = item.get('data_symbol', item['symbol'])
    if provider == 'twelvedata':
        return fetch_twelvedata(data_symbol, cfg)
    return fetch_yahoo(data_symbol)


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
    age_minutes = candle_age_minutes(df)
    if age_minutes is None:
        return False
    sc = cfg.get('scanner', {})
    provider = item.get('provider', 'yahoo')
    market_type = item.get('type', 'stock')
    if provider == 'twelvedata':
        max_age = float(sc.get('twelvedata_max_candle_age_minutes', 15))
    elif market_type == 'index':
        max_age = float(sc.get('index_max_candle_age_minutes', 30))
    else:
        max_age = float(sc.get('max_candle_age_minutes', 20))
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

    has_volume = 'Volume' in d.columns and pd.to_numeric(d['Volume'], errors='coerce').fillna(0).sum() > 0
    if has_volume:
        d['VOLAVG20'] = d['Volume'].rolling(20).mean()
        d['RVOL'] = d['Volume'] / d['VOLAVG20'].replace(0, np.nan)
    else:
        d['RVOL'] = np.nan

    # The newest row can be the currently-forming bar. Score the last completed bar.
    row = d.iloc[-2]
    prev = d.iloc[-3]
    live = d.iloc[-1]
    if pd.isna(row['ATR']) or row['ATR'] <= 0 or pd.isna(row['RSI']):
        return None

    trigger_close = float(row['Close'])
    live_price = float(live['Close'])
    atr_v = float(row['ATR'])
    market_type = item.get('type', 'stock')
    min_score = float(cfg['scanner']['minimum_score'])

    # Avoid chasing if price has already moved too far after the confirmation candle.
    if abs(live_price - trigger_close) > 0.75 * atr_v:
        return None

    long_score = 0.0
    long_reasons = []
    if row['EMA9'] > row['EMA20'] > row['EMA50']:
        long_score += 2.0; long_reasons.append('EMA trend up')
    elif row['EMA9'] > row['EMA20']:
        long_score += 1.0; long_reasons.append('short trend up')
    if 55 <= row['RSI'] <= 72:
        long_score += 1.0; long_reasons.append(f"RSI {row['RSI']:.0f}")
    if trigger_close > row['HH20']:
        long_score += 2.0; long_reasons.append('20-bar breakout')
    elif trigger_close > prev['High']:
        long_score += 1.0; long_reasons.append('5m high break')
    if has_volume and not pd.isna(row['VWAP']) and trigger_close > row['VWAP']:
        long_score += 1.0; long_reasons.append('above VWAP')
    if has_volume and not pd.isna(row['RVOL']) and row['RVOL'] >= 1.5:
        long_score += 1.5; long_reasons.append(f"RVOL {row['RVOL']:.1f}x")
    if not has_volume and market_type in ('forex', 'metal', 'index'):
        if row['EMA20'] > row['EMA50'] and row['EMA20'] > d['EMA20'].iloc[-5]:
            long_score += 1.0; long_reasons.append('trend slope up')
    if trigger_close > prev['Close'] and (trigger_close - prev['Close']) > 0.20 * atr_v:
        long_score += 0.5; long_reasons.append('momentum')

    short_score = 0.0
    short_reasons = []
    if row['EMA9'] < row['EMA20'] < row['EMA50']:
        short_score += 2.0; short_reasons.append('EMA trend down')
    elif row['EMA9'] < row['EMA20']:
        short_score += 1.0; short_reasons.append('short trend down')
    if 28 <= row['RSI'] <= 45:
        short_score += 1.0; short_reasons.append(f"RSI {row['RSI']:.0f}")
    if trigger_close < row['LL20']:
        short_score += 2.0; short_reasons.append('20-bar breakdown')
    elif trigger_close < prev['Low']:
        short_score += 1.0; short_reasons.append('5m low break')
    if has_volume and not pd.isna(row['VWAP']) and trigger_close < row['VWAP']:
        short_score += 1.0; short_reasons.append('below VWAP')
    if has_volume and not pd.isna(row['RVOL']) and row['RVOL'] >= 1.5:
        short_score += 1.5; short_reasons.append(f"RVOL {row['RVOL']:.1f}x")
    if not has_volume and market_type in ('forex', 'metal', 'index'):
        if row['EMA20'] < row['EMA50'] and row['EMA20'] < d['EMA20'].iloc[-5]:
            short_score += 1.0; short_reasons.append('trend slope down')
    if trigger_close < prev['Close'] and (prev['Close'] - trigger_close) > 0.20 * atr_v:
        short_score += 0.5; short_reasons.append('momentum')

    # A signal now requires an actual completed 5-minute trigger, not indicators alone.
    long_trigger = trigger_close > prev['High'] and trigger_close > row['Open']
    short_trigger = trigger_close < prev['Low'] and trigger_close < row['Open']

    score = max(long_score, short_score)
    if score < min_score:
        return None
    if long_score > short_score and long_trigger:
        side = 'LONG'; reasons = long_reasons
    elif short_score > long_score and short_trigger:
        side = 'SHORT'; reasons = short_reasons
    else:
        return None

    recent_low = float(d['Low'].iloc[-10:-1].min())
    recent_high = float(d['High'].iloc[-10:-1].max())
    stop_mult = float(cfg['risk']['atr_stop_multiple'])
    price = live_price
    if side == 'LONG':
        stop = min(trigger_close - stop_mult * atr_v, recent_low - 0.10 * atr_v)
        risk_per_unit = price - stop
        if risk_per_unit <= 0:
            return None
        tp1 = price + float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = price + float(cfg['risk']['tp2_r_multiple']) * risk_per_unit
    else:
        stop = max(trigger_close + stop_mult * atr_v, recent_high + 0.10 * atr_v)
        risk_per_unit = stop - price
        if risk_per_unit <= 0:
            return None
        tp1 = price - float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = price - float(cfg['risk']['tp2_r_multiple']) * risk_per_unit

    account = float(cfg['risk']['account_cash_gbp'])
    risk_pct = float(cfg['risk']['risk_per_trade_pct']) / 100.0
    risk_gbp = account * risk_pct
    stop_pct = risk_per_unit / price
    max_exposure = account * float(cfg['risk']['max_cash_exposure_pct']) / 100.0
    suggested = min(max_exposure, risk_gbp / stop_pct) if stop_pct > 0 else None

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    return Signal(
        symbol=item['symbol'], label=item.get('name', item['symbol']), market_type=market_type,
        side=side, price=price, stop=float(stop), tp1=float(tp1), tp2=float(tp2),
        score=float(score), reason=', '.join(reasons[:5]), risk_gbp=float(risk_gbp),
        suggested_exposure_gbp=float(suggested) if suggested else None, timestamp=now,
    )


def decimals_for_price(x):
    if x >= 1000: return 1
    if x >= 100: return 2
    if x >= 1: return 4
    return 5


def format_signal(s: Signal):
    dec = decimals_for_price(s.price)
    emoji = '🟢' if s.side == 'LONG' else '🔴'
    is_cfd = s.market_type in ('forex', 'index', 'metal') or s.side == 'SHORT'
    sizing_line = (
        f"📐 CFD sizing: choose units so the stop costs no more than ~£{s.risk_gbp:.2f}"
        if is_cfd else
        f"📦 Cash exposure guide: up to ~£{s.suggested_exposure_gbp:,.0f}" if s.suggested_exposure_gbp else
        f"📦 Size to max loss ~£{s.risk_gbp:.2f}"
    )
    note = ''
    if is_cfd:
        note += '\n⚠️ Check the live Trading 212 bid/ask before entering; spread/leverage can change real risk.'
    return (
        f"⚡ 5-MIN SETUP — {s.label}\n"
        f"{emoji} {s.side} — CONFIRMED near {s.price:.{dec}f}\n"
        f"🛑 Stop: {s.stop:.{dec}f}\n"
        f"🎯 TP1: {s.tp1:.{dec}f}\n"
        f"🎯 TP2: {s.tp2:.{dec}f}\n"
        f"⭐ Score: {s.score:.1f}/8\n"
        f"💷 Max planned loss: ~£{s.risk_gbp:.2f}\n"
        f"{sizing_line}\n"
        f"Reason: {s.reason}{note}\n"
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
            df = fetch_item(item, cfg)
            if df is None:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: insufficient/unavailable data')
                continue
            if not data_is_fresh(df, item, cfg):
                age = candle_age_minutes(df)
                age_txt = f'{age:.1f}m old' if age is not None else 'unknown age'
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: market/data stale ({age_txt}); skip')
                continue
            sig = score_signal(df, item, cfg)
            if not sig:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: no confirmed setup')
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
            delivered = True
            if cfg['notifications'].get('telegram', True):
                delivered = telegram_notify(text)

            # Only suppress future duplicates if the alert was actually delivered.
            if delivered:
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
    next_t = (math.floor(now / step) + 1) * step + 8
    return max(5, next_t - now)


def main():
    cfg = load_config()
    td_key = bool(os.getenv('TWELVE_DATA_API_KEY', '').strip())
    print('Market Scanner started.')
    print(f"Watchlist: {len(cfg['watchlist'])} instruments | min score {cfg['scanner']['minimum_score']} | interval 5m")
    print(f"Twelve Data FX/metals: {'READY' if td_key else 'MISSING API KEY'}")
    print('Press Ctrl+C to stop.\n')

    if cfg.get('notifications', {}).get('telegram', True):
        account = float(cfg.get('risk', {}).get('account_cash_gbp', 1000))
        risk_pct = float(cfg.get('risk', {}).get('risk_per_trade_pct', 1.0))
        risk_gbp = account * risk_pct / 100.0
        td_line = '✅ FX/metals live feed ready' if td_key else '⚠️ FX/metals disabled until TWELVE_DATA_API_KEY is added'
        startup_message = (
            '✅ Market Scanner Online\n'
            '🔄 Core scan every 5 minutes\n'
            f'📊 Watchlist: {len(cfg["watchlist"])} instruments\n'
            f'⭐ Minimum score: {cfg["scanner"]["minimum_score"]}/8 + confirmed 5m trigger\n'
            f'💷 Account basis: £{account:,.2f}\n'
            f'🛑 Planned risk/trade: ~£{risk_gbp:,.2f}\n'
            f'{td_line}'
        )
        if telegram_notify(startup_message):
            print('Telegram startup test: OK')
        else:
            print('Telegram startup test: FAILED — check Railway variables.')

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
