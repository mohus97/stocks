#!/usr/bin/env python3
"""Fast intraday live-alert layer.

This sits on top of spread_runtime.py and keeps the existing A-tier path intact.
It exposes a carefully filtered subset of B+ setups as separate FAST alerts for
short-horizon 15-60 minute manual Trading 212 trades.

Important design choices:
- Frequency is increased by surfacing qualifying B+ setups; core scanner quality
  thresholds are NOT lowered below 6.5.
- FAST alerts require immediate 1m/5m confirmation and tighter spread friction.
- FAST exits are deliberately quicker: +0.50R first target, +1.00R runner.
- Index FAST alerts are disabled until the Yahoo cash-index -> T212 CFD price-basis
  mismatch is solved. Raw index levels must not be presented as executable T212
  prices.
- IG demo behaviour is unchanged. This layer only changes live Telegram delivery
  and tracker metadata.
"""

import os

import launcher
import scanner
import spread_runtime


FAST_MODE_VERSION = 'fast_intraday_v1_2026_08_30'
FAST_COHORT = 'fast_intraday_current_2026_08_30'
FAST_MAX_SPREAD_R = float(os.getenv('T212_FAST_MAX_SPREAD_R', '0.15'))
FAST_MIN_SCORE = float(os.getenv('FAST_MIN_SCORE', '6.5'))
FAST_TP1_R = float(os.getenv('FAST_TP1_R', '0.50'))
FAST_TP2_R = float(os.getenv('FAST_TP2_R', '1.00'))


def _num(value, default=0.0):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return float(default)


def _tier(sig):
    ctx = dict(getattr(sig, 'context', {}) or {})
    return ctx.get('quality_tier') or scanner.quality_tier(sig.score, ctx)


def _fast_candidate(sig):
    """Return (eligible, reason, profile) for a short-horizon B+ live alert."""
    ctx = dict(getattr(sig, 'context', {}) or {})
    components = dict(ctx.get('score_components') or {})
    tier = _tier(sig)
    market_type = str(getattr(sig, 'market_type', '') or '').lower()

    profile = spread_runtime._spread_profile(sig)
    spread_runtime._apply_spread_context(sig, profile)

    if tier != 'B+':
        return False, f'tier {tier}', profile
    if _num(getattr(sig, 'score', 0.0)) < FAST_MIN_SCORE:
        return False, 'score below fast minimum', profile

    # Known unresolved production defect: Yahoo cash-index prices do not map
    # one-for-one to the T212 CFD quote. Do not emit executable FAST index levels.
    if market_type == 'index':
        return False, 'index basis conversion unavailable', profile

    trigger_1m = _num(components.get('trigger_1m'))
    setup_5m = _num(components.get('setup_5m'))
    momentum = _num(components.get('momentum_quality'))
    structure = _num(components.get('structure_location'))
    trend_vol = _num(components.get('trend_volatility'))

    # FAST means there must be evidence that price is moving NOW. A strong 1m
    # trigger is ideal; otherwise require a strong 5m setup plus decent momentum.
    immediate = trigger_1m >= 1.0 or (setup_5m >= 1.0 and momentum >= 1.0)
    if not immediate:
        return False, 'no immediate 1m/5m confirmation', profile

    # Avoid weak-location momentum punts. These are intentionally looser than
    # A-tier, but still require enough structure/trend quality to avoid noise.
    if structure < 1.0 or trend_vol < 0.5 or momentum < 0.5:
        return False, 'structure/trend/momentum too weak', profile

    spread_r = profile.get('spread_r')
    if spread_r is None:
        return False, 'spread unavailable', profile
    if float(spread_r) > FAST_MAX_SPREAD_R:
        return False, f'fast spread {float(spread_r):.2f}R > {FAST_MAX_SPREAD_R:.2f}R', profile

    return True, 'eligible', profile


def _fast_aware_alert(sig):
    eligible, reason, profile = _fast_candidate(sig)
    if not eligible:
        # Preserve the existing A-tier/spread-aware format and normal suppression
        # behaviour for every signal that is not a FAST candidate.
        return spread_runtime._spread_aware_alert(sig)

    ctx = getattr(sig, 'context', None)
    if ctx is None:
        sig.context = {}
        ctx = sig.context
    ctx['fast_mode_version'] = FAST_MODE_VERSION
    ctx['fast_strategy_cohort'] = FAST_COHORT
    ctx['fast_live_eligible'] = True
    ctx['fast_hold_window_minutes'] = [15, 60]
    ctx['fast_tp1_r'] = FAST_TP1_R
    ctx['fast_tp2_r'] = FAST_TP2_R

    dec = scanner.decimals_for_price(sig.price)
    side_icon = '🟢' if sig.side == 'LONG' else '🔴'
    tp1 = scanner._r_target_price(sig.price, sig.stop, sig.side, FAST_TP1_R)
    tp2 = scanner._r_target_price(sig.price, sig.stop, sig.side, FAST_TP2_R)

    symbol = str(sig.symbol).replace('^', '')
    label = str(sig.label or symbol)
    name = label if label.upper() == symbol.upper() else f'{label} ({symbol})'

    spread_r = float(profile.get('spread_r'))
    cost = float(profile.get('estimated_cost_gbp') or 0.0)
    source = profile.get('source')
    spread_icon = '🟢' if spread_r <= 0.10 else '🟡'

    return (
        f'🚨 {name}\n'
        f'⚡ FAST • B+ • {side_icon} {sig.side}\n'
        f'⏱ Target hold: 15–60m\n'
        f'📍 Entry: {sig.entry_low:.{dec}f} – {sig.entry_high:.{dec}f}\n'
        f'🎯 TP1: {tp1:.{dec}f} (+{FAST_TP1_R:.2f}R)\n'
        f'🏁 TP2: {tp2:.{dec}f} (+{FAST_TP2_R:.2f}R)\n'
        f'🛑 SL: {sig.stop:.{dec}f}\n'
        f'{spread_icon} Spread est: ~£{cost:.2f} ({spread_r:.2f}R, {source})'
    )


# scanner.format_signal()/format_1m_signal() call launcher._compact_trade_alert at
# runtime, so this cleanly layers FAST formatting over the existing safety stack.
launcher._compact_trade_alert = _fast_aware_alert


# spread_runtime's notifier ultimately routes through launcher's A-tier-only gate.
# Bypass that gate ONLY for explicitly classified FAST messages. A-tier and all
# other messages continue through the existing spread/A-tier pipeline unchanged.
_prev_notify = scanner.telegram_notify


def _telegram_with_fast_lane(text):
    message = str(text or '')
    if message.lstrip().startswith('🚨 ') and '⚡ FAST • B+' in message:
        # FAST formatting only occurs after the stricter 0.15R friction check.
        first_line = message.splitlines()[0] if message else 'FAST trade alert'
        print(f'Telegram FAST live: {first_line} ({FAST_MODE_VERSION})')
        return launcher._orig_telegram_notify(text)
    return _prev_notify(text)


scanner.telegram_notify = _telegram_with_fast_lane


# Label FAST rows in the existing tracker. The tracker already records 0.50R and
# 1.00R shadow exits, so we can evaluate this strategy without inventing a new
# outcome engine or contaminating the clean A-tier cohort.
_prev_register = scanner.register_tracked_signal


def _register_fast_metadata(sig, item=None):
    eligible, reason, profile = _fast_candidate(sig)
    tid = _prev_register(sig, item)
    if not eligible:
        return tid

    try:
        data = scanner.load_tracker()
        changed = False
        for rec in data.get('signals', []):
            if rec.get('id') != tid:
                continue
            rec['fast_mode_version'] = FAST_MODE_VERSION
            rec['fast_strategy_cohort'] = FAST_COHORT
            rec['fast_live_alert'] = True
            rec['fast_hold_window_minutes'] = [15, 60]
            rec['fast_tp1_r'] = FAST_TP1_R
            rec['fast_tp2_r'] = FAST_TP2_R
            rec['fast_max_spread_r'] = FAST_MAX_SPREAD_R
            rec['fast_spread_r'] = profile.get('spread_r')
            changed = True
            break
        if changed:
            scanner.save_tracker(data)
            print(
                f'[{scanner.datetime.now().strftime("%H:%M:%S")}] tracker: '
                f'FAST cohort tagged {sig.symbol} {sig.side} #{tid}'
            )
    except Exception as exc:
        print(f'FAST tracker metadata update skipped: {type(exc).__name__}')
    return tid


scanner.register_tracked_signal = _register_fast_metadata


if __name__ == '__main__':
    print(
        f'FAST intraday alerts: ENABLED — B+ 15-60m lane | '
        f'min score {FAST_MIN_SCORE:.1f} | max spread {FAST_MAX_SPREAD_R:.2f}R | '
        f'targets {FAST_TP1_R:.2f}R/{FAST_TP2_R:.2f}R | indices disabled '
        f'({FAST_MODE_VERSION})'
    )
    launcher.scanner.main()
