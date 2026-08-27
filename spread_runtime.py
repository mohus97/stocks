#!/usr/bin/env python3
"""Runtime spread-friction guard for live Telegram signals.

Builds on entrypoint.py's existing safety patches.  The scanner still researches
all setups, but live Telegram A-tier alerts are only allowed through when the
estimated Trading 212 CFD spread is reasonably small relative to the planned
stop distance.  This avoids technically good but economically poor scalps where
spread consumes too much of the trade's R before price has moved.

Trading 212 CFD spreads are floating.  The explicit values below are recent
Trading 212 published average spreads (checked 2026-08-27).  Symbols without a
published value in this table use a conservative market-type proxy; those are
labelled as estimates in logs/alerts rather than presented as broker averages.
"""

import os

import entrypoint
import launcher
import scanner


SPREAD_GUARD_VERSION = 't212_spread_guard_v1_2026_08_27'
MAX_LIVE_SPREAD_R = float(os.getenv('T212_MAX_SPREAD_R', '0.25'))

# Price-unit spread estimates.  Explicit entries are recent Trading 212
# published average CFD spreads, not fixed guarantees.
_T212_AVG_SPREAD = {
    # Forex / metal
    'EURUSD': 0.00014,
    'GBPUSD': 0.00026,
    'USDJPY': 0.029,
    'XAUUSD': 0.92,
    # Indices
    'GSPC': 0.43,   # Trading 212 USA 500
    'FTSE': 1.40,   # Trading 212 UK 100
    'GDAXI': 3.40,  # Trading 212 Germany 40
    # Frequently scanned US stocks where a recent T212 average was verified
    'TSLA': 0.20,
    'AMD': 0.84,
    'META': 0.68,
    'AMZN': 0.25,
    'MSFT': 0.27,
    'AVGO': 0.35,
    'ARM': 1.01,
}


def _symbol_key(sig):
    return str(sig.symbol).upper().replace('^', '').replace('=X', '').replace('/', '')


def _spread_price_units(sig):
    """Return (spread, source) without making a network call in the alert path."""
    key = _symbol_key(sig)
    if key in _T212_AVG_SPREAD:
        return float(_T212_AVG_SPREAD[key]), 'T212 avg'

    price = abs(float(sig.price))
    market_type = str(sig.market_type or '').lower()

    # Conservative proxies for symbols not yet in the explicit table.  Their
    # purpose is to reject extremely tight-stop trades, not pretend to know the
    # exact live bid/ask.  Explicit T212 averages can replace these over time.
    if market_type == 'stock':
        return max(0.02, price * 0.0010), 'stock proxy'
    if market_type == 'index':
        return max(0.50, price * 0.00020), 'index proxy'
    if market_type == 'metal':
        return max(0.10, price * 0.00020), 'metal proxy'
    if market_type == 'forex':
        return max(0.00002, price * 0.00015), 'FX proxy'
    return None, 'unavailable'


def _spread_profile(sig):
    stop_distance = abs(float(sig.price) - float(sig.stop))
    spread, source = _spread_price_units(sig)
    if spread is None or stop_distance <= 0:
        return {
            'spread': spread,
            'source': source,
            'spread_r': None,
            'estimated_cost_gbp': None,
            'eligible': True,
            'grade': 'UNKNOWN',
        }

    spread_r = float(spread) / stop_distance
    risk_gbp = max(0.0, float(getattr(sig, 'risk_gbp', 0.0) or 0.0))
    cost_gbp = risk_gbp * spread_r
    eligible = spread_r <= MAX_LIVE_SPREAD_R
    if spread_r <= 0.10:
        grade = 'CHEAP'
    elif eligible:
        grade = 'OK'
    else:
        grade = 'EXPENSIVE'
    return {
        'spread': float(spread),
        'source': source,
        'spread_r': float(spread_r),
        'estimated_cost_gbp': float(cost_gbp),
        'eligible': bool(eligible),
        'grade': grade,
    }


def _apply_spread_context(sig, profile):
    ctx = getattr(sig, 'context', None)
    if ctx is None:
        sig.context = {}
        ctx = sig.context
    ctx['spread_guard_version'] = SPREAD_GUARD_VERSION
    ctx['spread_price_units'] = profile.get('spread')
    ctx['spread_source'] = profile.get('source')
    ctx['spread_r'] = profile.get('spread_r')
    ctx['spread_est_gbp'] = profile.get('estimated_cost_gbp')
    ctx['spread_live_eligible'] = profile.get('eligible', True)
    ctx['spread_grade'] = profile.get('grade')


def _spread_aware_alert(sig):
    """Keep TP1/TP2 display and add execution-friction information."""
    profile = _spread_profile(sig)
    _apply_spread_context(sig, profile)

    dec = scanner.decimals_for_price(sig.price)
    tier = (sig.context or {}).get('quality_tier') or scanner.quality_tier(sig.score, sig.context or {})
    tier_icon = '🔥' if tier == 'A-TIER' else ('⭐' if tier == 'B+' else '⚡')
    side_icon = '🟢' if sig.side == 'LONG' else '🔴'
    tp1 = scanner._r_target_price(sig.price, sig.stop, sig.side, scanner.HYBRID_BANK_R)
    tp2 = scanner._r_target_price(sig.price, sig.stop, sig.side, scanner.HYBRID_RUNNER_R)

    symbol = str(sig.symbol).replace('^', '')
    label = str(sig.label or symbol)
    name = label if label.upper() == symbol.upper() else f'{label} ({symbol})'

    spread_r = profile.get('spread_r')
    cost = profile.get('estimated_cost_gbp')
    source = profile.get('source')
    grade = profile.get('grade')
    if spread_r is None:
        spread_line = '💸 Spread: estimate unavailable — check T212 live bid/ask'
        block_line = ''
    else:
        icon = '🟢' if grade == 'CHEAP' else ('🟡' if grade == 'OK' else '🔴')
        spread_line = f'{icon} Spread est: ~£{cost:.2f} ({spread_r:.2f}R, {source})'
        block_line = '' if profile['eligible'] else f'\n🚫 SPREAD BLOCK: {spread_r:.2f}R > {MAX_LIVE_SPREAD_R:.2f}R live limit'

    return (
        f'🚨 {name}\n'
        f'{tier_icon} {tier} • {side_icon} {sig.side}\n'
        f'📍 Entry: {sig.entry_low:.{dec}f} – {sig.entry_high:.{dec}f}\n'
        f'🎯 TP1: {tp1:.{dec}f} (+0.75R)\n'
        f'🏁 TP2: {tp2:.{dec}f} (+1.50R)\n'
        f'🛑 SL: {sig.stop:.{dec}f}\n'
        f'{spread_line}{block_line}'
    )


# launcher.format_signal_safe()/format_1m_signal_safe() resolve this global at
# call time, so replacing it preserves every existing signal-quality filter.
launcher._compact_trade_alert = _spread_aware_alert


# Add a final live-delivery gate after the existing A-tier-only Telegram gate.
_prev_telegram_notify = scanner.telegram_notify


def _telegram_with_spread_gate(text):
    message = str(text or '')
    if message.lstrip().startswith('🚨 ') and '🚫 SPREAD BLOCK:' in message:
        first_line = message.splitlines()[0] if message else 'trade alert'
        print(f'Telegram suppressed (spread friction): {first_line}')
        # True intentionally preserves the research/tracker path.  The clean
        # current-live A-tier cohort is handled separately below.
        return True
    return _prev_telegram_notify(text)


scanner.telegram_notify = _telegram_with_spread_gate


# Keep the clean CURRENT A-tier cohort representative of alerts that were actually
# eligible for live delivery.  High-friction A-tiers remain in legacy research.
_current_a_tier_register = scanner.register_tracked_signal


def _register_spread_aware(sig, item=None):
    profile = _spread_profile(sig)
    _apply_spread_context(sig, profile)
    tier = entrypoint._signal_tier(sig)

    if tier == 'A-TIER' and not profile.get('eligible', True):
        tid = entrypoint._orig_register_tracked_signal(sig, item)
        print(
            f'[{scanner.datetime.now().strftime("%H:%M:%S")}] tracker: '
            f'A-tier excluded from CURRENT cohort by spread guard '
            f'({_symbol_key(sig)} {profile.get("spread_r", 0.0):.2f}R)'
        )
    else:
        tid = _current_a_tier_register(sig, item)

    # Persist spread metadata into whichever research row actually owns this id.
    try:
        data = scanner.load_tracker()
        changed = False
        for rec in data.get('signals', []):
            if rec.get('id') != tid:
                continue
            if rec.get('spread_guard_version'):
                break
            rec['spread_guard_version'] = SPREAD_GUARD_VERSION
            rec['spread_price_units'] = profile.get('spread')
            rec['spread_source'] = profile.get('source')
            rec['spread_r'] = profile.get('spread_r')
            rec['spread_est_gbp'] = profile.get('estimated_cost_gbp')
            rec['spread_live_eligible'] = profile.get('eligible', True)
            rec['spread_grade'] = profile.get('grade')
            changed = True
            break
        if changed:
            scanner.save_tracker(data)
    except Exception as exc:
        print(f'spread metadata tracker update skipped: {type(exc).__name__}')
    return tid


scanner.register_tracked_signal = _register_spread_aware


if __name__ == '__main__':
    print(
        f'T212 spread guard: ENABLED — live A-tier max {MAX_LIVE_SPREAD_R:.2f}R '
        f'friction ({SPREAD_GUARD_VERSION})'
    )
    launcher.scanner.main()
