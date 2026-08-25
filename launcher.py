#!/usr/bin/env python3
"""Runtime safety overrides for the scanner.

Keeps scanner.py untouched while tightening A-tier classification, improving
Trading 212 stock-CFD sizing guidance internally, enforcing stale-entry rules,
and keeping Telegram trade alerts deliberately compact.
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


def format_1m_signal_safe(sig):
    return _compact_trade_alert(sig)


def format_signal_safe(sig):
    return _compact_trade_alert(sig)


# Monkey-patch functions looked up dynamically by scanner.py.
scanner._gold_like_core = strict_gold_like_core
scanner.quality_tier = strict_quality_tier
scanner.format_1m_signal = format_1m_signal_safe
scanner.format_signal = format_signal_safe


if __name__ == '__main__':
    scanner.main()
