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


@dataclass
class Candidate:
    symbol: str
    label: str
    market_type: str
    provider: str
    data_symbol: str
    side: str
    score: float
    trigger_level: float
    atr_value: float
    anchor_close: float
    recent_low: float
    recent_high: float
    reason: str
    armed_at: float


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


def fetch_yahoo_1m(symbol):
    try:
        df = yf.download(
            symbol,
            period='1d',
            interval='1m',
            progress=False,
            auto_adjust=True,
            prepost=False,
            threads=False,
        )
        df = normalize_columns(df)
        if df is None or df.empty:
            return None
        for col in ['Open', 'High', 'Low', 'Close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        if 'Volume' not in df.columns:
            df['Volume'] = 0.0
        df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])
        return df if len(df) >= 5 else None
    except Exception as e:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: Yahoo 1m fetch failed: {e}')
        return None


def twelvedata_item_active(item, cfg):
    """Quota-aware Twelve Data schedule.

    Gold/other metals stay available through the weekday because they are one
    of the most useful overnight markets. FX pairs are scanned at full 5-minute
    frequency during the main UK day-trading window. Sunday evening is enabled
    for both after the weekly reopen. Saturday is disabled.
    """
    now = datetime.now(UK)
    weekday = now.weekday()  # Mon=0 ... Sun=6
    hour = now.hour + now.minute / 60.0
    sc = cfg.get('scanner', {})
    start = float(sc.get('twelvedata_active_start_hour_uk', 7))
    end = float(sc.get('twelvedata_active_end_hour_uk', 18))
    sunday_start = float(sc.get('twelvedata_sunday_start_hour_uk', 22))
    market_type = item.get('type', 'forex')

    if weekday == 5:  # Saturday
        return False
    if weekday == 6:  # Sunday weekly reopen
        return hour >= sunday_start
    # Monday-Friday: keep metals live; conserve free credits on FX overnight.
    if market_type == 'metal':
        return True
    return start <= hour < end


def twelve_data_active(cfg):
    """Backward-compatible global helper: true if any TD market window is active."""
    dummy_metal = {'type': 'metal'}
    dummy_fx = {'type': 'forex'}
    return twelvedata_item_active(dummy_metal, cfg) or twelvedata_item_active(dummy_fx, cfg)

def _twelvedata_frame(payload):
    """Convert one Twelve Data time_series payload to our OHLC DataFrame."""
    if not isinstance(payload, dict) or payload.get('status') == 'error' or 'values' not in payload:
        return None
    rows = payload.get('values') or []
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


def fetch_twelvedata_batch(items, cfg):
    """Reliably fetch Twelve Data instruments one-by-one with pacing.

    The free plan allows 8 credits/minute. The current watchlist uses at most
    four live TD instruments, and the core scan intentionally does not run the
    1-minute watcher in the same minute. Individual requests avoid brittle
    multi-symbol response parsing while staying safely under the rate limit.
    """
    if not items:
        return {}
    key = os.getenv('TWELVE_DATA_API_KEY', '').strip()
    if not key:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data: API key missing; skip FX/metals')
        return {i['symbol']: None for i in items}

    out = {}
    active_items = [i for i in items if twelvedata_item_active(i, cfg)]
    inactive_items = [i for i in items if not twelvedata_item_active(i, cfg)]
    for item in inactive_items:
        out[item['symbol']] = None
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {item["symbol"]}: Twelve Data scan paused for quota/session window')

    for n, item in enumerate(active_items):
        ds = item.get('data_symbol', item['symbol'])
        try:
            r = requests.get(
                'https://api.twelvedata.com/time_series',
                params={
                    'symbol': ds,
                    'interval': '5min',
                    'outputsize': 120,
                    'timezone': 'UTC',
                    'apikey': key,
                },
                timeout=25,
            )
            try:
                data = r.json()
            except Exception:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {ds}: Twelve Data invalid JSON HTTP {r.status_code}')
                out[item['symbol']] = None
                continue

            if not r.ok or (isinstance(data, dict) and data.get('status') == 'error'):
                msg = data.get('message', r.text[:220]) if isinstance(data, dict) else r.text[:220]
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {ds}: Twelve Data error HTTP {r.status_code}: {msg}')
                out[item['symbol']] = None
                continue

            rows = len(data.get('values') or []) if isinstance(data, dict) else 0
            df = _twelvedata_frame(data)
            if df is None:
                status = data.get('status', '?') if isinstance(data, dict) else '?'
                keys = ','.join(list(data.keys())[:6]) if isinstance(data, dict) else type(data).__name__
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {ds}: Twelve Data unusable response status={status}, rows={rows}, keys={keys}')
                out[item['symbol']] = None
            else:
                age = candle_age_minutes(df)
                age_txt = f'{age:.1f}m' if age is not None else '?'
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {ds}: Twelve Data OK — {len(df)} bars, latest age {age_txt}')
                out[item['symbol']] = df
        except Exception as e:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {ds}: Twelve Data fetch failed: {e}')
            out[item['symbol']] = None

        # Gentle pacing. Four core symbols = comfortably below 8 credits/min.
        if n < len(active_items) - 1:
            time.sleep(1.2)

    return out

def fetch_twelvedata_1m(candidate, cfg):
    key = os.getenv('TWELVE_DATA_API_KEY', '').strip()
    item = {'type': candidate.market_type}
    if not key or not twelvedata_item_active(item, cfg):
        return None
    try:
        r = requests.get(
            'https://api.twelvedata.com/time_series',
            params={
                'symbol': candidate.data_symbol,
                'interval': '1min',
                'outputsize': 30,
                'timezone': 'UTC',
                'apikey': key,
            },
            timeout=20,
        )
        try:
            data = r.json()
        except Exception:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {candidate.data_symbol}: Twelve Data 1m invalid response')
            return None
        if not r.ok or (isinstance(data, dict) and data.get('status') == 'error'):
            msg = data.get('message', r.text[:180]) if isinstance(data, dict) else r.text[:180]
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {candidate.data_symbol}: Twelve Data 1m error: {msg}')
            return None
        return _twelvedata_frame_min_rows(data, 5)
    except Exception as e:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {candidate.data_symbol}: Twelve Data 1m fetch failed: {e}')
        return None


def _twelvedata_frame_min_rows(payload, min_rows=5):
    if not isinstance(payload, dict) or payload.get('status') == 'error' or 'values' not in payload:
        return None
    rows = payload.get('values') or []
    if len(rows) < min_rows:
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
    return df if len(df) >= min_rows else None


def fetch_twelvedata(symbol, cfg):
    # Backward-compatible single-symbol helper; normal scans use the batch path.
    item = {'symbol': symbol.replace('/', ''), 'data_symbol': symbol, 'provider': 'twelvedata'}
    return fetch_twelvedata_batch([item], cfg).get(item['symbol'])

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


def candidate_from_5m(df, item, cfg):
    """Arm a 1-minute watcher only when a 5-minute setup is genuinely close.

    The core strategy remains 5-minute. This function looks for a directional
    setup within roughly 1 score point of the normal threshold and near the
    last completed 5-minute high/low. The 1-minute watcher then waits for a
    closed 1-minute breakout/breakdown before alerting.
    """
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

    row = d.iloc[-2]
    prev = d.iloc[-3]
    live = d.iloc[-1]
    if pd.isna(row['ATR']) or row['ATR'] <= 0 or pd.isna(row['RSI']):
        return None

    close = float(row['Close'])
    live_price = float(live['Close'])
    atr_v = float(row['ATR'])
    market_type = item.get('type', 'stock')
    min_score = float(cfg['scanner']['minimum_score'])
    gap = float(cfg.get('scanner', {}).get('entry_watch_near_score_gap', 1.0))
    near_threshold = max(0.0, min_score - gap)

    long_score = 0.0
    long_reasons = []
    if row['EMA9'] > row['EMA20'] > row['EMA50']:
        long_score += 2.0; long_reasons.append('EMA trend up')
    elif row['EMA9'] > row['EMA20']:
        long_score += 1.0; long_reasons.append('short trend up')
    if 55 <= row['RSI'] <= 72:
        long_score += 1.0; long_reasons.append(f"RSI {row['RSI']:.0f}")
    if close > row['HH20']:
        long_score += 2.0; long_reasons.append('20-bar breakout')
    elif close > prev['High']:
        long_score += 1.0; long_reasons.append('5m high break')
    if has_volume and not pd.isna(row['VWAP']) and close > row['VWAP']:
        long_score += 1.0; long_reasons.append('above VWAP')
    if has_volume and not pd.isna(row['RVOL']) and row['RVOL'] >= 1.5:
        long_score += 1.5; long_reasons.append(f"RVOL {row['RVOL']:.1f}x")
    if not has_volume and market_type in ('forex', 'metal', 'index'):
        if row['EMA20'] > row['EMA50'] and row['EMA20'] > d['EMA20'].iloc[-5]:
            long_score += 1.0; long_reasons.append('trend slope up')
    if close > prev['Close'] and (close - prev['Close']) > 0.20 * atr_v:
        long_score += 0.5; long_reasons.append('momentum')

    short_score = 0.0
    short_reasons = []
    if row['EMA9'] < row['EMA20'] < row['EMA50']:
        short_score += 2.0; short_reasons.append('EMA trend down')
    elif row['EMA9'] < row['EMA20']:
        short_score += 1.0; short_reasons.append('short trend down')
    if 28 <= row['RSI'] <= 45:
        short_score += 1.0; short_reasons.append(f"RSI {row['RSI']:.0f}")
    if close < row['LL20']:
        short_score += 2.0; short_reasons.append('20-bar breakdown')
    elif close < prev['Low']:
        short_score += 1.0; short_reasons.append('5m low break')
    if has_volume and not pd.isna(row['VWAP']) and close < row['VWAP']:
        short_score += 1.0; short_reasons.append('below VWAP')
    if has_volume and not pd.isna(row['RVOL']) and row['RVOL'] >= 1.5:
        short_score += 1.5; short_reasons.append(f"RVOL {row['RVOL']:.1f}x")
    if not has_volume and market_type in ('forex', 'metal', 'index'):
        if row['EMA20'] < row['EMA50'] and row['EMA20'] < d['EMA20'].iloc[-5]:
            short_score += 1.0; short_reasons.append('trend slope down')
    if close < prev['Close'] and (prev['Close'] - close) > 0.20 * atr_v:
        short_score += 0.5; short_reasons.append('momentum')

    if long_score == short_score:
        return None
    if long_score > short_score:
        side = 'LONG'; score = long_score; reasons = long_reasons
        trigger = float(row['High'])
        distance = trigger - live_price
    else:
        side = 'SHORT'; score = short_score; reasons = short_reasons
        trigger = float(row['Low'])
        distance = live_price - trigger

    # Only arm setups close enough to matter and close enough in score that a
    # breakout confirmation can realistically push them over the threshold.
    if score < near_threshold:
        return None
    if distance > 0.60 * atr_v:
        return None
    if distance < -0.35 * atr_v:  # already ran too far through the trigger
        return None

    return Candidate(
        symbol=item['symbol'],
        label=item.get('name', item['symbol']),
        market_type=market_type,
        provider=item.get('provider', 'yahoo'),
        data_symbol=item.get('data_symbol', item['symbol']),
        side=side,
        score=float(score),
        trigger_level=float(trigger),
        atr_value=float(atr_v),
        anchor_close=float(close),
        recent_low=float(d['Low'].iloc[-10:-1].min()),
        recent_high=float(d['High'].iloc[-10:-1].max()),
        reason=', '.join(reasons[:5]),
        armed_at=time.time(),
    )


def select_candidates(candidates, cfg):
    if not candidates:
        return {}
    sc = cfg.get('scanner', {})
    max_yahoo = int(sc.get('entry_watch_max_yahoo_candidates', 5))
    max_td = int(sc.get('entry_watch_max_twelvedata_candidates', 1))
    yahoo = sorted([c for c in candidates if c.provider != 'twelvedata'], key=lambda c: c.score, reverse=True)[:max_yahoo]
    td = sorted([c for c in candidates if c.provider == 'twelvedata'], key=lambda c: c.score, reverse=True)[:max_td]
    chosen = yahoo + td
    return {c.symbol: c for c in chosen}


def td_watcher_daily_budget(cfg):
    """Reserve the free 800-credit/day plan for core scans first.

    Metals are budgeted at 5-minute frequency for 24h on weekdays. FX pairs
    are budgeted at 5-minute frequency only during the configured UK window.
    The remainder is available to the near-setup 1-minute watcher.
    """
    sc = cfg.get('scanner', {})
    start = float(sc.get('twelvedata_active_start_hour_uk', 7))
    end = float(sc.get('twelvedata_active_end_hour_uk', 18))
    active_hours = max(0.0, end - start)
    td_items = [i for i in cfg.get('watchlist', []) if i.get('provider') == 'twelvedata']
    metals = sum(1 for i in td_items if i.get('type') == 'metal')
    fx = sum(1 for i in td_items if i.get('type') == 'forex')
    estimated_core = int(metals * 12 * 24 + fx * 12 * active_hours)
    reserve = int(sc.get('entry_watch_td_credit_reserve', 50))
    return max(0, 800 - estimated_core - reserve)

def consume_td_watch_credit(state, cfg):
    today = datetime.now(UK).date().isoformat()
    if state.get('_td_watch_date') != today:
        state['_td_watch_date'] = today
        state['_td_watch_credits'] = 0
    used = int(state.get('_td_watch_credits', 0))
    budget = td_watcher_daily_budget(cfg)
    if used >= budget:
        return False, used, budget
    state['_td_watch_credits'] = used + 1
    return True, used + 1, budget


def build_signal_from_candidate(c, entry_price, cfg):
    stop_mult = float(cfg['risk']['atr_stop_multiple'])
    if c.side == 'LONG':
        stop = min(c.anchor_close - stop_mult * c.atr_value, c.recent_low - 0.10 * c.atr_value)
        risk_per_unit = entry_price - stop
        if risk_per_unit <= 0:
            return None
        tp1 = entry_price + float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = entry_price + float(cfg['risk']['tp2_r_multiple']) * risk_per_unit
    else:
        stop = max(c.anchor_close + stop_mult * c.atr_value, c.recent_high + 0.10 * c.atr_value)
        risk_per_unit = stop - entry_price
        if risk_per_unit <= 0:
            return None
        tp1 = entry_price - float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = entry_price - float(cfg['risk']['tp2_r_multiple']) * risk_per_unit

    account = float(cfg['risk']['account_cash_gbp'])
    risk_gbp = account * float(cfg['risk']['risk_per_trade_pct']) / 100.0
    stop_pct = risk_per_unit / entry_price
    max_exposure = account * float(cfg['risk']['max_cash_exposure_pct']) / 100.0
    suggested = min(max_exposure, risk_gbp / stop_pct) if stop_pct > 0 else None
    return Signal(
        symbol=c.symbol,
        label=c.label,
        market_type=c.market_type,
        side=c.side,
        price=float(entry_price),
        stop=float(stop),
        tp1=float(tp1),
        tp2=float(tp2),
        score=float(min(8.0, c.score + 1.0)),
        reason=(c.reason + ', 1m trigger confirmed').strip(', '),
        risk_gbp=float(risk_gbp),
        suggested_exposure_gbp=float(suggested) if suggested else None,
        timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
    )


def format_1m_signal(s: Signal):
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
        note = '\n⚠️ Check the live Trading 212 bid/ask before entering; spread/leverage can change real risk.'
    return (
        f"🚨 1-MIN ENTRY — {s.label}\n"
        f"{emoji} {s.side} — TRIGGER CONFIRMED near {s.price:.{dec}f}\n"
        f"🛑 Stop: {s.stop:.{dec}f}\n"
        f"🎯 TP1: {s.tp1:.{dec}f}\n"
        f"🎯 TP2: {s.tp2:.{dec}f}\n"
        f"⭐ 5m setup + 1m trigger: {s.score:.1f}/8\n"
        f"💷 Max planned loss: ~£{s.risk_gbp:.2f}\n"
        f"{sizing_line}\n"
        f"Reason: {s.reason}{note}\n"
        f"🕒 {s.timestamp}"
    )


def watch_1m_entries(cfg, armed):
    if not armed:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] 1m watcher: no armed setups')
        return armed

    state = load_state()
    cooldown_min = int(cfg['scanner']['cooldown_minutes'])
    ttl_min = float(cfg.get('scanner', {}).get('entry_watch_ttl_minutes', 6))
    now_ts = time.time()
    remaining = dict(armed)

    for symbol, c in list(armed.items()):
        if now_ts - c.armed_at > ttl_min * 60:
            remaining.pop(symbol, None)
            continue

        try:
            if c.provider == 'twelvedata':
                allowed, used, budget = consume_td_watch_credit(state, cfg)
                if not allowed:
                    print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m watcher paused — Twelve Data daily watcher budget {budget} used')
                    continue
                df = fetch_twelvedata_1m(c, cfg)
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m watcher TD credit {used}/{budget}')
            else:
                df = fetch_yahoo_1m(c.data_symbol)
            if df is None or len(df) < 3:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m watcher data unavailable')
                continue

            # Use the last completed 1-minute candle and require an actual cross.
            bar = df.iloc[-2]
            prev = df.iloc[-3]
            live = df.iloc[-1]
            close = float(bar['Close'])
            prev_close = float(prev['Close'])
            live_price = float(live['Close'])

            if c.side == 'LONG':
                crossed = prev_close <= c.trigger_level and close > c.trigger_level and close > float(bar['Open'])
                too_far = live_price - c.trigger_level > 0.35 * c.atr_value
            else:
                crossed = prev_close >= c.trigger_level and close < c.trigger_level and close < float(bar['Open'])
                too_far = c.trigger_level - live_price > 0.35 * c.atr_value

            if not crossed:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m watcher armed {c.side} @ {c.trigger_level:.5f}; waiting')
                continue
            if too_far:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m trigger crossed but entry already stretched; skip')
                remaining.pop(symbol, None)
                continue

            sig = build_signal_from_candidate(c, live_price, cfg)
            if not sig:
                remaining.pop(symbol, None)
                continue

            sig_id = signature(sig)
            key = f'{symbol}:{sig.side}'
            last = state.get(key, {})
            if (now_ts - last.get('time', 0)) < cooldown_min * 60 and last.get('signature') == sig_id:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m trigger held; cooldown')
                remaining.pop(symbol, None)
                continue

            text = format_1m_signal(sig)
            print('\n' + text + '\n')
            delivered = telegram_notify(text) if cfg.get('notifications', {}).get('telegram', True) else True
            if delivered:
                state[key] = {'time': now_ts, 'signature': sig_id}
                remaining.pop(symbol, None)
        except Exception as e:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m watcher ERROR {e}')

    save_state(state)
    return remaining


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


def scan_once(cfg, include_twelvedata=True):
    state = load_state()
    cooldown_min = int(cfg['scanner']['cooldown_minutes'])
    now_ts = time.time()
    alerts = []
    candidates = []

    td_items = [i for i in cfg['watchlist'] if i.get('provider') == 'twelvedata']
    td_data = {}
    if include_twelvedata:
        td_data = fetch_twelvedata_batch(td_items, cfg)
    elif td_items:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data: startup handover — waiting until next 5-minute scan')

    for item in cfg['watchlist']:
        symbol = item['symbol']
        try:
            if item.get('provider') == 'twelvedata':
                if not include_twelvedata:
                    continue
                if not twelvedata_item_active(item, cfg):
                    continue
                df = td_data.get(symbol)
            else:
                df = fetch_item(item, cfg)
            if df is None:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: live data unavailable — see provider detail above')
                continue
            if not data_is_fresh(df, item, cfg):
                age = candle_age_minutes(df)
                age_txt = f'{age:.1f}m old' if age is not None else 'unknown age'
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: market/data stale ({age_txt}); skip')
                continue
            sig = score_signal(df, item, cfg)
            if not sig:
                c = candidate_from_5m(df, item, cfg)
                if c:
                    candidates.append(c)
                    print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: near setup — 1m watcher armed {c.side} @ {c.trigger_level:.5f} (score {c.score:.1f})')
                else:
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
    return alerts, select_candidates(candidates, cfg)


def seconds_to_next_minute():
    now = time.time()
    next_t = (math.floor(now / 60) + 1) * 60 + 12
    return max(3, next_t - now)


def seconds_to_next_5m():
    now = time.time()
    step = 300
    next_t = (math.floor(now / step) + 1) * step + 8
    return max(5, next_t - now)


def main():
    cfg = load_config()
    td_key = bool(os.getenv('TWELVE_DATA_API_KEY', '').strip())
    print('Market Scanner started.')
    print(f"Watchlist: {len(cfg['watchlist'])} instruments | min score {cfg['scanner']['minimum_score']} | core interval 5m")
    print('1-minute entry watcher: ENABLED for near-qualified 5m setups')
    print(f"Twelve Data FX/metals: {'READY' if td_key else 'MISSING API KEY'}")
    print('Press Ctrl+C to stop.\n')

    if cfg.get('notifications', {}).get('telegram', True):
        account = float(cfg.get('risk', {}).get('account_cash_gbp', 1000))
        risk_pct = float(cfg.get('risk', {}).get('risk_per_trade_pct', 1.0))
        risk_gbp = account * risk_pct / 100.0
        td_line = '✅ FX/metals live feed ready' if td_key else '⚠️ FX/metals disabled until TWELVE_DATA_API_KEY is added'
        startup_message = (
            '✅ Market Scanner Online\n'
            '🔄 Core strategy: confirmed 5-minute setups\n'
            '👀 1-minute entry watcher: ACTIVE when a setup is close\n'
            f'📊 Watchlist: {len(cfg["watchlist"])} instruments\n'
            f'⭐ Minimum score: {cfg["scanner"]["minimum_score"]}/8\n'
            f'💷 Account basis: £{account:,.2f}\n'
            f'🛑 Planned risk/trade: ~£{risk_gbp:,.2f}\n'
            f'{td_line}'
        )
        if telegram_notify(startup_message):
            print('Telegram startup test: OK')
        else:
            print('Telegram startup test: FAILED — check Railway variables.')

    # Railway may briefly run old/new containers together during redeploy. Do
    # not touch Twelve Data on this immediate startup pass.
    _, armed = scan_once(cfg, include_twelvedata=False)

    while True:
        try:
            wait = seconds_to_next_minute()
            print(f'Next scheduler tick in {int(wait)}s...')
            time.sleep(wait)
            now = datetime.now(UK)

            # Run the expensive/full scan once per 5-minute boundary. On these
            # minutes we intentionally skip the 1m watcher so the free Twelve
            # Data per-minute credit limit has plenty of headroom.
            if now.minute % 5 == 0:
                _, armed = scan_once(cfg, include_twelvedata=True)
                if armed:
                    labels = ', '.join(f'{c.symbol}:{c.side}' for c in armed.values())
                    print(f'1m watcher armed: {labels}')
                else:
                    print('1m watcher: nothing armed after core scan')
            else:
                armed = watch_1m_entries(cfg, armed)
        except KeyboardInterrupt:
            print('\nStopped.')
            break


if __name__ == '__main__':
    main()
