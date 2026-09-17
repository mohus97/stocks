"""Time and integrity rules for providers whose candle timestamps are bar opens."""
import numpy as np
import pandas as pd


def utc_now():
    return pd.Timestamp.now(tz='UTC')


def clean_frame(frame):
    if frame is None or frame.empty:
        return pd.DataFrame()
    d = frame.copy()
    d.index = pd.to_datetime(d.index, utc=True, errors='coerce')
    if d.index.isna().any() or d.index.has_duplicates:
        return pd.DataFrame()
    d = d.sort_index()
    columns = ['Open', 'High', 'Low', 'Close']
    if not set(columns).issubset(d.columns):
        return pd.DataFrame()
    d[columns] = d[columns].apply(pd.to_numeric, errors='coerce')
    values = d[columns].to_numpy()
    if not np.isfinite(values).all() or (values <= 0).any():
        return pd.DataFrame()
    if ((d.High < d[['Open', 'Close', 'Low']].max(axis=1)) |
            (d.Low > d[['Open', 'Close', 'High']].min(axis=1))).any():
        return pd.DataFrame()
    return d


def closed_bars(frame, minutes=5, now=None):
    d = clean_frame(frame)
    if d.empty:
        return d
    now = utc_now() if now is None else pd.Timestamp(now).tz_convert('UTC')
    # A small settlement delay avoids treating a provider's final update as final.
    return d.loc[d.index + pd.Timedelta(minutes=minutes, seconds=2) <= now]


def fresh_frame(frame, max_age_minutes, now=None):
    d = clean_frame(frame)
    if d.empty:
        return False
    now = utc_now() if now is None else pd.Timestamp(now).tz_convert('UTC')
    age = (now - d.index[-1]).total_seconds() / 60
    return 0 <= age <= max_age_minutes


def continuous_tail(frame, minutes, count=3):
    if frame is None or len(frame) < count:
        return False
    idx = pd.to_datetime(frame.index[-count:], utc=True)
    return bool((idx[1:] - idx[:-1] == pd.Timedelta(minutes=minutes)).all())


def resample_closed(frame, rule, now=None):
    d = closed_bars(frame, 5, now)
    if d.empty:
        return d
    if not (d.index == d.index.floor('5min')).all():
        return pd.DataFrame()  # Misaligned source bars straddle the target buckets.
    agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    if 'Volume' in d:
        agg['Volume'] = 'sum'
    # Both feeds label bars with their START. 10:00 belongs in [10:00, 10:15).
    grouped = d.resample(rule, label='left', closed='left', origin='start_day')
    out = grouped.agg(agg)
    expected = int(pd.Timedelta(rule) / pd.Timedelta(minutes=5))
    now = utc_now() if now is None else pd.Timestamp(now).tz_convert('UTC')
    complete = (grouped.Close.count() == expected) & (out.index + pd.Timedelta(rule) <= now)
    return out.loc[complete].dropna(subset=['Open', 'High', 'Low', 'Close'])
