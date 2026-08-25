#!/usr/bin/env python3
"""Runtime safety overrides for the scanner.

Keeps scanner.py untouched while tightening A-tier classification, improving
Trading 212 stock-CFD sizing guidance internally, enforcing stale-entry rules,
blocking exhausted stock chases after violent moves, adding a full-session
extension/catalyst guard for stocks, and keeping Telegram trade alerts deliberately
compact. Only A-tier trade alerts are sent to Telegram; lower tiers continue to
run and be tracked silently.
"""
import math
import os
import time

import scanner


_US_STOCKS = {
    'NVDA','TSLA','PLTR','AMD','META','AMZN','AAPL','MSFT','GOOGL','AVGO','MU','ARM',
    'SMCI','COIN','MSTR','HOOD','SOFI','RDDT','NFLX','CRWD'
}
_fx_cache = {'value': 0.74, 'ts': 0.0}
_ig_startup_status = {}


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


def _stock_session_extension(snap, item):
    """Block same-direction stock entries after a highly extended session move.

    This is deliberately price-led rather than dependent on a news API. A large
    session move, gap, or abnormal volume is treated as a possible catalyst day.
    The trade is blocked near the session extreme and can become eligible again
    only after a meaningful pullback; the scanner's normal confirmation rules
    must then qualify a fresh continuation setup.
    """
    if not snap or str((item or {}).get('type', '')).lower() != 'stock':
        return {'LONG': None, 'SHORT': None}

    d = snap.get('d')
    atr_v = float(snap.get('atr') or 0.0)
    if d is None or len(d) < 12 or not math.isfinite(atr_v) or atr_v <= 0:
        return {'LONG': None, 'SHORT': None}

    completed = d.iloc[:-1].copy()
    if completed.empty:
        return {'LONG': None, 'SHORT': None}

    try:
        idx_utc = scanner.pd.to_datetime(completed.index, utc=True, errors='coerce')
        valid = ~idx_utc.isna()
        completed = completed.loc[valid].copy()
        idx_utc = idx_utc[valid]
        if completed.empty:
            return {'LONG': None, 'SHORT': None}
        ny_dates = idx_utc.tz_convert('America/New_York').date
        current_date = ny_dates[-1]
        current_mask = ny_dates == current_date
        session = completed.loc[current_mask].copy()
        prior = completed.loc[~current_mask].copy()
    except Exception:
        # Yahoo's normal stock feed is timezone-aware, but fail open if an
        # unexpected index shape appears rather than breaking the whole scanner.
        return {'LONG': None, 'SHORT': None}

    if len(session) < 4:
        return {'LONG': None, 'SHORT': None}

    session_open = float(session.iloc[0]['Open'])
    close = float(session.iloc[-1]['Close'])
    session_high = float(session['High'].astype(float).max())
    session_low = float(session['Low'].astype(float).min())
    if session_open <= 0 or close <= 0:
        return {'LONG': None, 'SHORT': None}

    move_pct = 100.0 * (close - session_open) / session_open
    move_atr = (close - session_open) / atr_v
    long_pullback_atr = max(0.0, (session_high - close) / atr_v)
    short_pullback_atr = max(0.0, (close - session_low) / atr_v)

    prev_close = None
    gap_pct = 0.0
    if not prior.empty:
        try:
            prev_close = float(prior.iloc[-1]['Close'])
            if prev_close > 0:
                gap_pct = 100.0 * (session_open - prev_close) / prev_close
        except Exception:
            prev_close = None

    volume_ratio = 1.0
    if 'Volume' in completed.columns:
        try:
            recent_vol = session['Volume'].astype(float).tail(3)
            baseline_src = prior['Volume'].astype(float).tail(40) if not prior.empty else session['Volume'].astype(float).iloc[:-3]
            baseline = float(baseline_src[baseline_src > 0].median()) if len(baseline_src) else 0.0
            recent = float(recent_vol[recent_vol > 0].mean()) if len(recent_vol) else 0.0
            if baseline > 0 and recent > 0:
                volume_ratio = recent / baseline
        except Exception:
            volume_ratio = 1.0

    # A plain 3% move needs substantial ATR extension. A smaller move can still
    # be treated as catalyst-like when accompanied by a large gap or abnormal
    # volume. These defaults are intentionally conservative for live alerts.
    catalyst_like = abs(gap_pct) >= 1.50 or volume_ratio >= 2.00
    extended_up = (
        (move_pct >= 3.00 and move_atr >= 2.50)
        or (move_pct >= 2.00 and move_atr >= 2.00 and catalyst_like)
    )
    extended_down = (
        (move_pct <= -3.00 and move_atr <= -2.50)
        or (move_pct <= -2.00 and move_atr <= -2.00 and catalyst_like)
    )

    # Do not chase the session extreme. A pullback of at least 0.65 ATR resets
    # this guard; normal structure/momentum/trigger confirmation is still needed.
    long_reset = long_pullback_atr >= 0.65
    short_reset = short_pullback_atr >= 0.65

    out = {'LONG': None, 'SHORT': None}
    catalyst_bits = []
    if abs(gap_pct) >= 1.50:
        catalyst_bits.append(f"gap {gap_pct:+.1f}%")
    if volume_ratio >= 2.00:
        catalyst_bits.append(f"volume {volume_ratio:.1f}x")
    catalyst_text = f"; catalyst flags: {', '.join(catalyst_bits)}" if catalyst_bits else ''

    if extended_up and not long_reset:
        out['LONG'] = (
            f"session already extended +{move_pct:.1f}% / +{move_atr:.1f} ATR — wait for pullback "
            f"({long_pullback_atr:.2f} ATR so far{catalyst_text})"
        )
    if extended_down and not short_reset:
        out['SHORT'] = (
            f"session already extended {move_pct:.1f}% / {move_atr:.1f} ATR — wait for pullback "
            f"({short_pullback_atr:.2f} ATR so far{catalyst_text})"
        )

    snap.setdefault('context', {})['session_extension'] = {
        'session_move_pct': round(move_pct, 3),
        'session_move_atr': round(move_atr, 3),
        'gap_pct': round(gap_pct, 3),
        'volume_ratio': round(volume_ratio, 3),
        'long_pullback_atr': round(long_pullback_atr, 3),
        'short_pullback_atr': round(short_pullback_atr, 3),
        'catalyst_like': bool(catalyst_like),
    }
    return out


_orig_decision_snapshot = scanner._decision_snapshot
_orig_telegram_notify = scanner.telegram_notify
_orig_ig_ensure_login = scanner.IGDemoExecutor.ensure_login


def decision_snapshot_safe(df, item, cfg):
    """Wrap Decision Engine v2 with stock anti-chase and session-extension vetoes."""
    snap = _orig_decision_snapshot(df, item, cfg)
    if not snap:
        return snap

    blocks = _stock_impulse_exhaustion(snap, item)
    session_blocks = _stock_session_extension(snap, item)
    for side in ('LONG', 'SHORT'):
        reasons_to_add = [blocks.get(side), session_blocks.get(side)]
        for reason in reasons_to_add:
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


def ig_demo_single_login_status():
    """Avoid IG's old double-login startup flow.

    scanner.main() historically authenticated once for a connectivity test, logged
    that session out, then immediately authenticated a second time for execution.
    IG can reject that second rapid login with an invalid-client-security-token 401.
    Return a shared provisional status object so main performs only the executor
    login; the ensure_login wrapper below then updates this same object with the
    real result before startup status is printed or sent to Telegram.
    """
    api_key = os.getenv('IG_DEMO_API_KEY', '').strip()
    identifier = os.getenv('IG_DEMO_IDENTIFIER', '').strip()
    password = os.getenv('IG_DEMO_PASSWORD', '').strip()
    auto_flag = os.getenv('IG_AUTO_TRADE', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
    _ig_startup_status.clear()
    if not (api_key and identifier and password):
        _ig_startup_status.update({
            'ok': False,
            'configured': False,
            'auto_requested': auto_flag,
            'message': 'IG demo credentials missing in Railway variables.',
        })
    else:
        # Provisional True intentionally makes scanner.main() call executor.ensure_login().
        _ig_startup_status.update({
            'ok': True,
            'configured': True,
            'auto_requested': auto_flag,
            'message': 'IG demo executor authentication pending.',
        })
    return _ig_startup_status


def ig_ensure_login_with_status(self):
    """Authenticate once and keep startup/runtime status aligned with the executor."""
    ok = bool(_orig_ig_ensure_login(self))
    if self.configured:
        _ig_startup_status.update({
            'ok': ok,
            'configured': True,
            'auto_requested': bool(self.auto),
            'environment': 'DEMO' if ok else None,
            'account_id': self.account_id,
            'account_type': self.account_type,
            'currency': self.currency,
            'dealing_enabled': bool(self.dealing_enabled),
            'tokens_ok': bool(self.cst and self.xst),
            'message': 'IG demo executor connected.' if ok else 'IG demo executor authentication failed.',
        })
    return ok


def format_1m_signal_safe(sig):
    return _compact_trade_alert(sig)


def format_signal_safe(sig):
    return _compact_trade_alert(sig)


# Monkey-patch functions looked up dynamically by scanner.py.
scanner._gold_like_core = strict_gold_like_core
scanner.quality_tier = strict_quality_tier
scanner._decision_snapshot = decision_snapshot_safe
scanner.telegram_notify = telegram_notify_a_tier_only
scanner.ig_demo_connection_test = ig_demo_single_login_status
scanner.IGDemoExecutor.ensure_login = ig_ensure_login_with_status
scanner.format_1m_signal = format_1m_signal_safe
scanner.format_signal = format_signal_safe


if __name__ == '__main__':
    scanner.main()
