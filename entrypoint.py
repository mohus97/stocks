#!/usr/bin/env python3
"""Runtime entrypoint with a narrow IG index-contract safety patch.

The scanner's index market mapper used minimum point cost as a stronger ranking
criterion than tradeability. That could select IG's Weekend UK 100 contract on a
weekday because it was cheaper, even when its status was EDITS_ONLY.

This patch keeps the existing mapper for every non-index market. For indices it
only considers live TRADEABLE, non-weekend contracts and otherwise fails closed.
"""

import scanner


_orig_ig_search_market = scanner.IGDemoExecutor.search_market


def _weekend_contract(market, details=None):
    instrument = (details or {}).get('instrument') or {}
    text = ' '.join(
        str(x or '')
        for x in (
            (market or {}).get('instrumentName'),
            (market or {}).get('epic'),
            instrument.get('name'),
            instrument.get('epic'),
        )
    ).upper()
    return 'WEEKEND' in text


def ig_search_market_tradeable_only(self, sig):
    """Select a live non-weekend index CFD, or fail closed if none is safe."""
    if str(sig.market_type).lower() != 'index':
        return _orig_ig_search_market(self, sig)

    key = str(sig.symbol).upper().replace('=X', '').replace('^', '').replace('/', '')
    terms = []
    if self.SEARCH_TERMS.get(key):
        terms.append(self.SEARCH_TERMS[key])
    if sig.label and sig.label not in terms:
        terms.append(sig.label)

    expected = self.EXPECTED_TYPES.get('index', {'INDICES'})
    candidates = []
    seen_epics = set()

    for term in terms[:2]:
        r, data = self.request('GET', '/markets', '1', params={'searchTerm': term})
        if r is None or not r.ok or not isinstance(data, dict):
            continue
        for market in data.get('markets') or []:
            epic = market.get('epic')
            if not epic or epic in seen_epics:
                continue
            seen_epics.add(epic)

            if str(market.get('instrumentType') or '').upper() not in expected:
                continue
            if str(market.get('marketStatus') or '').upper() != 'TRADEABLE':
                continue
            if _weekend_contract(market):
                continue

            score = 40 + 25
            if str(market.get('expiry') or '').upper() in {'DFB', '-'}:
                score += 15
            if int(market.get('delayTime') or 0) == 0:
                score += 10
            name = str(market.get('instrumentName') or '').lower()
            if term.lower() == name:
                score += 15
            elif term.lower() in name:
                score += 8
            if any(x in name for x in ('cash', 'spot', 'daily', 'dfb')):
                score += 4
            if 'mini' in name:
                score += 6
            candidates.append((market, score))

    if not candidates:
        print(f'[{scanner.datetime.now().strftime("%H:%M:%S")}] {sig.symbol}: no safe TRADEABLE non-weekend IG index contract')
        return None

    account_ccy = str(self.currency or 'GBP').upper()
    ranked = []
    for market, base_score in sorted(candidates, key=lambda x: x[1], reverse=True)[:10]:
        details = self.market_details(market.get('epic'))
        if not details or _weekend_contract(market, details):
            continue

        snapshot = details.get('snapshot') or {}
        status = str(snapshot.get('marketStatus') or market.get('marketStatus') or '').upper()
        if status != 'TRADEABLE':
            continue

        instrument = details.get('instrument') or {}
        rules = details.get('dealingRules') or {}
        pip_value = scanner._number_from_text(instrument.get('valueOfOnePip'))
        min_size = scanner._safe_float((rules.get('minDealSize') or {}).get('value'))
        if pip_value is None or pip_value <= 0 or min_size is None or min_size <= 0:
            continue

        currencies = [c for c in (instrument.get('currencies') or []) if isinstance(c, dict)]
        account_obj = next((c for c in currencies if str(c.get('code') or '').upper() == account_ccy), None)
        default_obj = next((c for c in currencies if bool(c.get('isDefault'))), None)
        deal_obj = account_obj or default_obj or (currencies[0] if currencies else {})
        deal_ccy = str(deal_obj.get('code') or '').upper()

        same_ccy_rank = 0 if deal_ccy == account_ccy else 1
        min_point_cost = float(min_size) * float(pip_value)
        ranked.append(((same_ccy_rank, min_point_cost, -base_score), market, details, deal_ccy, min_size, pip_value))

    if not ranked:
        print(f'[{scanner.datetime.now().strftime("%H:%M:%S")}] {sig.symbol}: IG index candidates found but none passed live-contract safety checks')
        return None

    ranked.sort(key=lambda x: x[0])
    _, chosen, details, deal_ccy, min_size, pip_value = ranked[0]
    chosen_name = (details.get('instrument') or {}).get('name') or chosen.get('instrumentName') or sig.label
    print(
        f'[{scanner.datetime.now().strftime("%H:%M:%S")}] {sig.symbol}: IG index contract selected '
        f'{chosen_name} | min size {min_size:g} | pip value {pip_value:g} | currency {deal_ccy or "?"} | live-only'
    )
    return chosen


scanner.IGDemoExecutor.search_market = ig_search_market_tradeable_only

# Importing launcher applies the rest of the existing runtime safety overrides.
import launcher  # noqa: E402


if __name__ == '__main__':
    launcher.scanner.main()
