#!/usr/bin/env python3
"""Runtime safety overrides for the scanner.

Keeps scanner.py untouched while tightening A-tier classification, improving
Trading 212 stock-CFD sizing guidance internally, enforcing stale-entry rules,
blocking exhausted stock chases after violent moves, and keeping Telegram trade
alerts deliberately compact. Only A-tier trade alerts are sent to Telegram;
lower tiers continue to run and be tracked silently.
"""
import math
import time

import scanner


_US_STOCKS = {
    'NVDA','TSLA','PLTR','AMD','META','AMZN','AAPL','MSFT','GOOGL','AVGO','MU','ARM',
    'SMCI','COIN','MSTR','HOOD','SOFI','RDDT','NFLX','CRWD'
}
_fx_cache = {'value': 0.74, 'ts': 0.0}


def _usd_to_gbp():
    """Best-effort USD->GBP conversion for US-stock CFD sizing.

    Falls back conservatively to 0.74 if Yahoo is temporarily unavailable.
    """
    now = time.time()
    if now - _fx_cache['ts'] < 1800:
        return _fx_cache['value']
    try:
        d = scanner.yf.download('GBPUSD=X', period='1d', interval='1m', progress=False, auto_adjust=False)
        if d is not None and len(d):
            px = float(d['Close'].dropna().iloc[-1])
            if px > 0:
                _fx_cache['value'] = 1.0 / px
                _fx_cache['ts'] = now
    except Exception:
        pass
    return float(_fx_cache['value'])


def strict_gold_like_core(context):
    """A-tier core: full quality plus clean, aligned HTF trend regimes."""
    c = dict((context or {}).get('score_components') or {})
    reg15 = str((context or {}).get('regime_15m') or '')
    reg1h = str((context or {}).get('regime_1h') or '')
    aligned_trend = (
        reg15 in {'TREND_UP', 'TREND_DOWN'}
        and reg1h == reg15
    )
    return (
        aligned_trend
        and float(c.get('htf_regime', 0.0) or 0.0) >= 1.75
        and float(c.get('structure_location', 0.0) or 0.0) >= 2.0
        and float(c.get('trend_volatility', 0.0) or 0.0) >= 1.0
        and float(c.get('momentum_quality', 0.0) or 0.0) >= 1.0
    )


def strict_quality_tier(score, context):
    c = dict((context or {}).get('score_components') or {})
    trigger_1m = float(c.get('trigger_1m', 0.0) or 0.0)
    setup_5m = float(c.get('setup_5m', 0.0) or 0.0)
    if strict_gold_like_core(context):
        if (float(score) >= 6.75 and setup_5m >= 0.5) or (float(score) >= 7.0 and trigger_1m >= 1.0):
            return 'A-TIER'
    if float(score) >= 6.5:
        return 'B+'
    return 'B-TIER'


def _stock_cfd_unit_guide(sig):
    """Retained for internal/debug use even though compact Telegram alerts hide it."""
    sym = str(sig.symbol).upper().replace('^','')
    if sig.market_type != 'stock' or sym not in _US_STOCKS:
        return None
    move_usd = abs(float(sig.stop) - float(sig.price))
    if move_usd <= 0:
        return None
    usdgbp = _usd_to_gbp()
    loss_per_unit_gbp = move_usd * usdgbp
    risk_units = float(sig.risk_gbp) / loss_per_unit_gbp if loss_per_unit_gbp > 0 else 0.0
    notional_units = math.inf
    if sig.suggested_exposure_gbp:
        notional_per_unit_gbp = float(sig.price) * usdgbp
        if notional_per_unit_gbp > 0:
            notional_units = float(sig.suggested_exposure_gbp) / notional_per_unit_gbp
    units = min(risk_units, notional_units)
    if not math.isfinite(units) or units <= 0:
        return None
    return (
        f"max ~{units:.2f} units; ≈£{loss_per_unit_gbp:.2f}/unit to stop; USD→GBP {usdgbp:.3f}"
    )


def _compact_trade_alert(sig):
    """Only show the fields needed to act quickly; full analytics stay in logs/tracker."""
    dec = scanner.decimals_for_price(sig.price)
    tier = (sig.context or {}).get('quality_tier') or scanner.quality_tier(sig.score, sig.context or {})
    tier_icon = '🔥' if tier == 'A-TIER' else ('⭐' if tier == 'B+' else '⚡')
    side_icon = '🟢' if sig.side == 'LONG' else '🔴'

    # The scanner's current recommended first cash-out level is +0.75R.
    tp = scanner._r_target_price(sig.price, sig.stop, sig.side, scanner.HYBRID_BANK_R)

    symbol = str(sig.symbol).replace('^', '')
    label = str(sig.label or symbol)
    name = label if label.upper() == symbol.upper() else f"{label} ({symbol})"

    return (
        f"🚨 {name}\n"
        f"{tier_icon} {tier} • {side_icon} {sig.side}\n"
        f"📍 Entry: {sig.entry_low:.{dec}f} – {sig.entry_high:.{dec}f}\n"
        f"🎯 TP: {tp:.{dec}f}\n"
        f"🛑 SL: {sig.stop:.{dec}f}"
    )


def _stock_impulse_exhaustion(snap, item):
    """Detect when a stock has already moved too far to chase safely.

    A violent 5m impulse often produces a snapback even when the higher-timeframe
    trend is correct. We block entries near the extreme until price has made a
    meaningful retrace. After that retrace, the normal breakout/retest logic may
    qualify a fresh continuation entry.
    """
    if not snap or str((item or {}).get('type', '')).lower() != 'stock':
        return {'LONG': None, 'SHORT': None}

    d = snap.get('d')
    atr_v = float(snap.get('atr') or 0.0)
    if d is None or len(d) < 8 or not math.isfinite(atr_v) or atr_v <= 0:
        return {'LONG': None, 'SHORT': None}

    # Completed 5m bars only; ignore the still-forming live candle.
    w = d.iloc[:-1].tail(6).copy()
    if len(w) < 5:
        return {'LONG': None, 'SHORT': None}

    close = float(w.iloc[-1]['Close'])
    ema20 = float(w.iloc[-1]['EMA20'])
    rsi = float(w.iloc[-1]['RSI'])
    first_open = float(w.iloc[0]['Open'])
    lows = [float(x) for x in w['Low']]
    highs = [float(x) for x in w['High']]
    opens = [float(x) for x in w['Open']]
    closes = [float(x) for x in w['Close']]

    low = min(lows)
    high = max(highs)
    low_i = lows.index(low)
    high_i = highs.index(high)
    red_bars = sum(1 for o, c in zip(opens, closes) if c < o)
    green_bars = sum(1 for o, c in zip(opens, closes) if c > o)

    drop_atr = max(0.0, (first_open - low) / atr_v)
    rally_atr = max(0.0, (high - first_open) / atr_v)
    below_ema_atr = max(0.0, (ema20 - close) / atr_v)
    above_ema_atr = max(0.0, (close - ema20) / atr_v)
    from_low_atr = max(0.0, (close - low) / atr_v)
    from_high_atr = max(0.0, (high - close) / atr_v)

    # A retrace only counts if it happened AFTER the impulse extreme. This stops
    # a vertical dump/pump being chased, while allowing a pullback + fresh rejection.
    post_low_high = max(highs[low_i + 1:]) if low_i + 1 < len(highs) else low
    post_high_low = min(lows[high_i + 1:]) if high_i + 1 < len(lows) else high
    short_retrace_atr = max(0.0, (post_low_high - low) / atr_v)
    long_retrace_atr = max(0.0, (high - post_high_low) / atr_v)
    short_reset = short_retrace_atr >= 0.45
    long_reset = long_retrace_atr >= 0.45

    # Two complementary definitions catch both multi-bar waterfalls and a stock
    # that is simply very stretched away from its mean after the move.
    heavy_down = (
        (drop_atr >= 1.50 and red_bars >= 3 and from_low_atr <= 0.45)
        or (below_ema_atr >= 1.20 and rsi <= 34 and from_low_atr <= 0.50)
    )
    heavy_up = (
        (rally_atr >= 1.50 and green_bars >= 3 and from_high_atr <= 0.45)
        or (above_ema_atr >= 1.20 and rsi >= 66 and from_high_atr <= 0.50)
    )

    out = {'LONG': None, 'SHORT': None}
    if heavy_down and not short_reset:
        out['SHORT'] = (
            f"extended downside impulse — wait for retrace "
            f"(drop {drop_atr:.2f} ATR, retrace {short_retrace_atr:.2f} ATR)"
        )
    if heavy_up and not long_reset:
        out['LONG'] = (
            f"extended upside impulse — wait for retrace "
            f"(rally {rally_atr:.2f} ATR, retrace {long_retrace_atr:.2f} ATR)"
        )

    # Keep metrics in context so the tracker can later tell us whether thresholds
    # are too strict or too loose without cluttering Telegram alerts.
    snap.setdefault('context', {})['impulse_exhaustion'] = {
        'drop_atr': round(drop_atr, 3),
        'rally_atr': round(rally_atr, 3),
        'below_ema20_atr': round(below_ema_atr, 3),
        'above_ema20_atr': round(above_ema_atr, 3),
        'short_retrace_atr': round(short_retrace_atr, 3),
        'long_retrace_atr': round(long_retrace_atr, 3),
        'rsi': round(rsi, 2),
    }
    return out


_orig_decision_snapshot = scanner._decision_snapshot
_orig_telegram_notify = scanner.telegram_notify


def decision_snapshot_safe(df, item, cfg):
    """Wrap Decision Engine v2 with a stock-specific anti-chase veto."""
    snap = _orig_decision_snapshot(df, item, cfg)
    if not snap:
        return snap
    blocks = _stock_impulse_exhaustion(snap, item)
    for side, reason in blocks.items():
        if not reason:
            continue
        result = (snap.get('results') or {}).get(side)
        if not result:
            continue
        result['veto'] = True
        reasons = result.setdefault('veto_reasons', [])
        if reason not in reasons:
            reasons.append(reason)
    return snap


def telegram_notify_a_tier_only(text):
    """Suppress B/B+ trade alerts while preserving tracking and non-trade messages.

    Returning True for a suppressed trade is intentional: scanner.py treats a
    successful delivery as permission to register the signal in the research
    tracker. This keeps the lower-tier sample running silently in the background.
    """
    message = str(text or '')
    if message.lstrip().startswith('🚨 ') and 'A-TIER' not in message:
        first_line = message.splitlines()[0] if message else 'trade alert'
        print(f"Telegram suppressed (non-A-tier): {first_line}")
        return True
    return _orig_telegram_notify(text)


def format_1m_signal_safe(sig):
    return _compact_trade_alert(sig)


def format_signal_safe(sig):
    return _compact_trade_alert(sig)


# Monkey-patch functions looked up dynamically by scanner.py.
scanner._gold_like_core = strict_gold_like_core
scanner.quality_tier = strict_quality_tier
scanner._decision_snapshot = decision_snapshot_safe
scanner.telegram_notify = telegram_notify_a_tier_only
scanner.format_1m_signal = format_1m_signal_safe
scanner.format_signal = format_signal_safe


if __name__ == '__main__':
    scanner.main()
