#!/usr/bin/env python3
import os
import time
import json
import math
import hashlib
import re
from dataclasses import dataclass, field
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
    entry_low: float
    entry_high: float
    score: float
    reason: str
    risk_gbp: float
    suggested_exposure_gbp: float | None
    timestamp: str
    context: dict = field(default_factory=dict)


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
    context: dict = field(default_factory=dict)




IG_DEMO_BASE = 'https://demo-api.ig.com/gateway/deal'


def ig_demo_connection_test():
    """Authenticate against IG's DEMO gateway only and return safe account status.

    This function intentionally does not place, amend, or close any orders.
    It is the first-stage connectivity check before demo execution is enabled.
    """
    api_key = os.getenv('IG_DEMO_API_KEY', '').strip()
    identifier = os.getenv('IG_DEMO_IDENTIFIER', '').strip()
    password = os.getenv('IG_DEMO_PASSWORD', '').strip()
    auto_flag = os.getenv('IG_AUTO_TRADE', 'false').strip().lower() in {'1','true','yes','on'}

    if not (api_key and identifier and password):
        return {
            'ok': False,
            'configured': False,
            'auto_requested': auto_flag,
            'message': 'IG demo credentials missing in Railway variables.'
        }

    headers = {
        'X-IG-API-KEY': api_key,
        'Content-Type': 'application/json',
        'Accept': 'application/json; charset=UTF-8',
        'Version': '2',
        'User-Agent': 'MoMarketScanner/1.0',
    }
    payload = {
        'identifier': identifier,
        'password': password,
        'encryptedPassword': False,
    }
    try:
        r = requests.post(
            f'{IG_DEMO_BASE}/session',
            headers=headers,
            json=payload,
            timeout=25,
        )
        try:
            data = r.json()
        except Exception:
            data = {}

        if not r.ok:
            err = data.get('errorCode') if isinstance(data, dict) else None
            return {
                'ok': False,
                'configured': True,
                'auto_requested': auto_flag,
                'message': f'IG demo login failed (HTTP {r.status_code}{": " + err if err else ""}).'
            }

        cst = r.headers.get('CST')
        sec = r.headers.get('X-SECURITY-TOKEN')
        current_id = data.get('currentAccountId') or data.get('accountId')
        environment = str(data.get('reroutingEnvironment') or 'DEMO').upper()
        dealing_enabled = bool(data.get('dealingEnabled', False))
        account_type = None
        currency = data.get('currencyIsoCode')
        account_name = None
        accounts = data.get('accounts') or []
        for acct in accounts:
            if acct.get('accountId') == current_id:
                account_type = acct.get('accountType')
                currency = acct.get('currencyIsoCode') or currency
                account_name = acct.get('accountName')
                break
        if account_type is None:
            account_type = (data.get('accountInfo') or {}).get('accountType')

        # Safety: the URL is already hard-coded to the demo gateway. If IG ever
        # reports an unexpected reroute, treat it as a failed safety check.
        safe_demo = environment == 'DEMO'
        tokens_ok = bool(cst and sec)
        cfd_ok = str(account_type or '').upper() == 'CFD'
        ok = bool(safe_demo and tokens_ok and dealing_enabled and cfd_ok)

        # Log out immediately: this stage only proves credentials/account access.
        if tokens_ok:
            logout_headers = dict(headers)
            logout_headers.update({'CST': cst, 'X-SECURITY-TOKEN': sec, 'Version': '1'})
            try:
                requests.delete(f'{IG_DEMO_BASE}/session', headers=logout_headers, timeout=10)
            except Exception:
                pass

        return {
            'ok': ok,
            'configured': True,
            'auto_requested': auto_flag,
            'environment': environment,
            'account_id': current_id,
            'account_name': account_name,
            'account_type': account_type,
            'currency': currency,
            'dealing_enabled': dealing_enabled,
            'tokens_ok': tokens_ok,
            'message': 'IG demo connection verified.' if ok else 'IG responded, but one or more demo safety checks failed.'
        }
    except Exception as e:
        return {
            'ok': False,
            'configured': True,
            'auto_requested': auto_flag,
            'message': f'IG demo connection error: {type(e).__name__}'
        }


def format_ig_demo_status(status):
    if not status.get('configured'):
        return '⚪ IG demo: not configured'
    if not status.get('ok'):
        return f'⚠️ IG demo: NOT READY — {status.get("message", "connection failed")}'
    aid = status.get('account_id') or 'unknown'
    # Only reveal a short account suffix in Telegram/logs.
    safe_id = ('…' + str(aid)[-3:]) if len(str(aid)) > 3 else str(aid)
    auto_requested = status.get('auto_requested', False)
    auto_line = '🤖 IG auto execution switch: ON (DEMO safety gates apply)' if auto_requested else '🔒 IG auto execution switch: OFF'
    return (
        '🤖 IG DEMO CONNECTED\n'
        f'✅ Environment: DEMO\n'
        f'✅ Account: {status.get("account_type") or "?"} {status.get("currency") or ""} ({safe_id})\n'
        f'✅ Dealing enabled: {"YES" if status.get("dealing_enabled") else "NO"}\n'
        f'{auto_line}'
    )

IG_EXECUTOR = None


def _env_true(name, default='false'):
    return os.getenv(name, default).strip().lower() in {'1', 'true', 'yes', 'on'}


def _safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _number_from_text(value):
    if value is None:
        return None
    m = re.search(r'[-+]?\d+(?:\.\d+)?', str(value).replace(',', ''))
    return float(m.group(0)) if m else None


def ig_execution_path():
    return storage_dir() / 'ig_demo_execution.json'


def load_ig_execution_state():
    p = ig_execution_path()
    if not p.exists():
        return {'trades': []}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {'trades': []}
        data.setdefault('trades', [])
        return data
    except Exception:
        return {'trades': []}


def save_ig_execution_state(data):
    _atomic_json_write(ig_execution_path(), data)


class IGDemoExecutor:
    """IG CFD DEMO executor. This build has no code path to api.ig.com."""

    SEARCH_TERMS = {
        'XAUUSD': 'Spot Gold',
        'GBPUSD': 'GBP/USD',
        'EURUSD': 'EUR/USD',
        'USDJPY': 'USD/JPY',
        'EURGBP': 'EUR/GBP',
        'GBPJPY': 'GBP/JPY',
        'NDX': 'US Tech 100',
        'GSPC': 'US 500',
        'DJI': 'Wall Street',
        'FTSE': 'FTSE 100',
        'GDAXI': 'Germany 40',
    }
    EXPECTED_TYPES = {
        'metal': {'COMMODITIES'},
        'forex': {'CURRENCIES'},
        'index': {'INDICES'},
        'stock': {'SHARES'},
    }

    def __init__(self, cfg):
        self.cfg = cfg
        self.api_key = os.getenv('IG_DEMO_API_KEY', '').strip()
        self.identifier = os.getenv('IG_DEMO_IDENTIFIER', '').strip()
        self.password = os.getenv('IG_DEMO_PASSWORD', '').strip()
        self.auto = _env_true('IG_AUTO_TRADE', 'false')
        allowed = os.getenv('IG_AUTO_MARKET_TYPES', 'forex,index,metal')
        self.allowed_types = {x.strip().lower() for x in allowed.split(',') if x.strip()}
        self.max_open = max(1, int(os.getenv('IG_MAX_OPEN_POSITIONS', '3')))
        self.max_trades_day = max(1, int(os.getenv('IG_MAX_DEMO_TRADES_PER_DAY', '8')))
        self.max_stops_day = max(1, int(os.getenv('IG_MAX_DEMO_STOP_OUTS_PER_DAY', '3')))
        self.cst = None
        self.xst = None
        self.account_id = None
        self.account_type = None
        self.currency = None
        self.dealing_enabled = False
        self.logged_in = False
        self.market_cache = {}

    @property
    def configured(self):
        return bool(self.api_key and self.identifier and self.password)

    def base_headers(self, version='1'):
        headers = {
            'X-IG-API-KEY': self.api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json; charset=UTF-8',
            'Version': str(version),
            'User-Agent': 'MarketScanner-DemoExecutor/1.0',
        }
        if self.cst:
            headers['CST'] = self.cst
        if self.xst:
            headers['X-SECURITY-TOKEN'] = self.xst
        return headers

    def login(self):
        if not self.configured:
            return False, 'credentials missing'
        try:
            r = requests.post(
                f'{IG_DEMO_BASE}/session',
                headers=self.base_headers('2'),
                json={'identifier': self.identifier, 'password': self.password, 'encryptedPassword': False},
                timeout=25,
            )
            try:
                data = r.json()
            except Exception:
                data = {}
            if not r.ok:
                err = data.get('errorCode') if isinstance(data, dict) else None
                return False, f'HTTP {r.status_code}{": " + err if err else ""}'
            self.cst = r.headers.get('CST')
            self.xst = r.headers.get('X-SECURITY-TOKEN')
            self.account_id = data.get('currentAccountId') or data.get('accountId')
            environment = str(data.get('reroutingEnvironment') or 'DEMO').upper()
            self.dealing_enabled = bool(data.get('dealingEnabled', False))
            self.currency = data.get('currencyIsoCode')
            self.account_type = None
            for acct in data.get('accounts') or []:
                if acct.get('accountId') == self.account_id:
                    self.account_type = acct.get('accountType')
                    self.currency = acct.get('currencyIsoCode') or self.currency
                    break
            if self.account_type is None:
                self.account_type = (data.get('accountInfo') or {}).get('accountType')
            safe = (
                IG_DEMO_BASE == 'https://demo-api.ig.com/gateway/deal'
                and environment == 'DEMO'
                and bool(self.cst and self.xst)
                and str(self.account_type or '').upper() == 'CFD'
                and self.dealing_enabled
            )
            self.logged_in = bool(safe)
            return (True, 'connected') if safe else (False, 'demo safety check failed')
        except Exception as e:
            return False, f'{type(e).__name__}: {e}'

    def ensure_login(self):
        if self.logged_in and self.cst and self.xst:
            return True
        ok, msg = self.login()
        if not ok:
            print(f'IG demo executor login failed: {msg}')
        return ok

    def request(self, method, path, version='1', *, params=None, payload=None, retry_auth=True):
        if not self.ensure_login():
            return None, None
        try:
            r = requests.request(
                method,
                f'{IG_DEMO_BASE}{path}',
                headers=self.base_headers(version),
                params=params,
                json=payload,
                timeout=25,
            )
            if r.status_code == 401 and retry_auth:
                self.logged_in = False
                self.cst = self.xst = None
                if self.ensure_login():
                    return self.request(method, path, version, params=params, payload=payload, retry_auth=False)
            try:
                data = r.json()
            except Exception:
                data = {}
            return r, data
        except Exception as e:
            print(f'IG demo request error {method} {path}: {e}')
            return None, None

    def open_positions(self):
        r, data = self.request('GET', '/positions', '2')
        if r is None or not r.ok or not isinstance(data, dict):
            return None
        return data.get('positions') or []

    @staticmethod
    def _deal_ids(positions):
        ids = set()
        for row in positions or []:
            pos = row.get('position') or {}
            deal_id = pos.get('dealId') or row.get('dealId')
            if deal_id:
                ids.add(str(deal_id))
        return ids

    def search_market(self, sig):
        key = str(sig.symbol).upper().replace('=X', '').replace('^', '').replace('/', '')
        terms = []
        if self.SEARCH_TERMS.get(key):
            terms.append(self.SEARCH_TERMS[key])
        if sig.label and sig.label not in terms:
            terms.append(sig.label)
        expected = self.EXPECTED_TYPES.get(sig.market_type, set())

        # Gather all plausible matches first.  The old code returned the first
        # high-scoring name match, which frequently selected IG's standard index
        # contract (for example FTSE £10/point) even when a smaller Mini/GBP
        # contract was available.  That made a ~£20 risk budget impossible.
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
                inst_type = str(market.get('instrumentType') or '').upper()
                if expected and inst_type not in expected:
                    continue
                score = 40 if inst_type in expected else 0
                if str(market.get('marketStatus') or '').upper() == 'TRADEABLE':
                    score += 25
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
                # A name hint is useful, but actual contract metadata below is
                # the deciding factor for indices.
                if 'mini' in name:
                    score += 6
                candidates.append((market, score))
                seen_epics.add(epic)

        if not candidates:
            return None

        # Non-index markets keep the original relevance/tradeability ranking.
        if sig.market_type != 'index':
            return max(candidates, key=lambda x: x[1])[0]

        # For indices, inspect the best plausible contracts and prefer the one
        # with the smallest practical point cost in the account currency.  This
        # makes FTSE Mini / GBP £1-per-point style contracts win over standard
        # £10/€25 contracts when both are available.
        account_ccy = str(self.currency or 'GBP').upper()
        ranked = []
        for market, base_score in sorted(candidates, key=lambda x: x[1], reverse=True)[:10]:
            details = self.market_details(market.get('epic'))
            if not details:
                continue
            instrument = details.get('instrument') or {}
            rules = details.get('dealingRules') or {}
            pip_value = _number_from_text(instrument.get('valueOfOnePip'))
            min_size = _safe_float((rules.get('minDealSize') or {}).get('value'))
            if pip_value is None or pip_value <= 0 or min_size is None or min_size <= 0:
                continue

            currencies = [c for c in (instrument.get('currencies') or []) if isinstance(c, dict)]
            account_obj = next((c for c in currencies if str(c.get('code') or '').upper() == account_ccy), None)
            default_obj = next((c for c in currencies if bool(c.get('isDefault'))), None)
            deal_obj = account_obj or default_obj or (currencies[0] if currencies else {})
            deal_ccy = str(deal_obj.get('code') or '').upper()

            # Primary preference: a contract that can be dealt directly in the
            # account currency. Secondary preference: lower minimum point cost.
            same_ccy_rank = 0 if deal_ccy == account_ccy else 1
            min_point_cost = float(min_size) * float(pip_value)
            ranked.append(((same_ccy_rank, min_point_cost, -base_score), market, details, deal_ccy, min_size, pip_value))

        if ranked:
            ranked.sort(key=lambda x: x[0])
            _, chosen, details, deal_ccy, min_size, pip_value = ranked[0]
            chosen_name = (details.get('instrument') or {}).get('name') or chosen.get('instrumentName') or sig.label
            print(
                f'[{datetime.now().strftime("%H:%M:%S")}] {sig.symbol}: IG index contract selected '
                f'{chosen_name} | min size {min_size:g} | pip value {pip_value:g} | currency {deal_ccy or "?"}'
            )
            return chosen

        # Metadata can occasionally be incomplete; fail back to the safest
        # original relevance ranking rather than inventing a contract.
        return max(candidates, key=lambda x: x[1])[0]

    def market_details(self, epic):
        cached = self.market_cache.get(epic)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        r, data = self.request('GET', f'/markets/{epic}', '3')
        if r is None or not r.ok or not isinstance(data, dict):
            return None
        self.market_cache[epic] = (time.time(), data)
        return data

    @staticmethod
    def _rule_points(rule, reference_price, scaling_factor=1.0):
        """Convert an IG dealing-rule distance into the same price units as bid/offer.

        IG returns POINTS in instrument point/pip units, while scanner stops are
        expressed as raw price differences (for example 0.0004 on GBP/USD).
        snapshot.scalingFactor is the multiplier from raw price distance to IG
        point/pip units, so POINTS must be divided by that factor here.
        """
        if not isinstance(rule, dict):
            return 0.0
        value = _safe_float(rule.get('value'), 0.0) or 0.0
        unit = str(rule.get('unit') or '').upper()
        if unit == 'PERCENTAGE':
            return abs(reference_price) * value / 100.0
        if unit == 'POINTS':
            sf = _safe_float(scaling_factor, 1.0) or 1.0
            if sf <= 0:
                return 0.0
            return value / sf
        return value

    @staticmethod
    def _size_floor(raw_size, min_size):
        if raw_size <= 0:
            return 0.0
        step = min_size if min_size and min_size > 0 else 0.01
        size = math.floor((raw_size + 1e-12) / step) * step
        s = f'{step:.8f}'.rstrip('0')
        decimals = min(6, len(s.split('.')[1])) if '.' in s else 0
        return round(size, decimals)

    def _today_counts(self):
        today = datetime.now(UK).date()
        opened = stops = 0
        for rec in load_ig_execution_state().get('trades', []):
            try:
                ts = pd.Timestamp(rec.get('created_at'))
                if ts.tzinfo is None:
                    ts = ts.tz_localize('UTC')
                if ts.tz_convert(UK).date() != today:
                    continue
            except Exception:
                continue
            if rec.get('status') in {'OPEN', 'TP1', 'STOP', 'CLOSED_OTHER'}:
                opened += 1
            if rec.get('status') == 'STOP':
                stops += 1
        return opened, stops

    def stats_line(self):
        trades = load_ig_execution_state().get('trades', [])
        wins = sum(1 for x in trades if x.get('status') == 'TP1')
        losses = sum(1 for x in trades if x.get('status') == 'STOP')
        open_n = sum(1 for x in trades if x.get('status') == 'OPEN')
        total_r = wins * float(self.cfg['risk'].get('tp1_r_multiple', 1.5)) - losses
        n = wins + losses
        wr = f'{100*wins/n:.1f}%' if n else 'n/a'
        return f'🤖 IG demo: {wins}W / {losses}L | Win rate {wr} | {total_r:+.1f}R | Open {open_n}'

    def _already_attempted(self, sig_id):
        return any(x.get('signal_id') == sig_id for x in load_ig_execution_state().get('trades', []))

    def _record(self, rec):
        data = load_ig_execution_state()
        data.setdefault('trades', []).append(rec)
        save_ig_execution_state(data)

    def execute_signal(self, sig, source='5m'):
        if not self.auto:
            return False, 'auto execution OFF'
        if sig.market_type not in self.allowed_types:
            return False, f'{sig.market_type} alert-only in phase 1'
        if not self.ensure_login():
            telegram_notify('⚠️ IG DEMO AUTO: login unavailable — no order placed.')
            return False, 'login unavailable'

        sig_id = signature(sig)
        if self._already_attempted(sig_id):
            return False, 'already attempted'

        opened_today, stops_today = self._today_counts()
        if opened_today >= self.max_trades_day:
            telegram_notify(f'🛑 IG DEMO AUTO: daily trade cap reached ({opened_today}/{self.max_trades_day}).')
            return False, 'daily trade cap'
        if stops_today >= self.max_stops_day:
            telegram_notify(f'🛑 IG DEMO AUTO: daily stop-out cap reached ({stops_today}/{self.max_stops_day}).')
            return False, 'daily stop cap'

        positions = self.open_positions()
        if positions is None:
            telegram_notify('⚠️ IG DEMO AUTO: open-position check failed; skipped for safety.')
            return False, 'position check failed'
        if len(positions) >= self.max_open:
            telegram_notify(f'⏸ IG DEMO AUTO: {len(positions)} position already open; {sig.label} skipped.')
            return False, 'max open positions'

        market = self.search_market(sig)
        if not market:
            telegram_notify(f'⚠️ IG DEMO AUTO: could not safely map {sig.label}; no order placed.')
            self._record({'signal_id': sig_id, 'symbol': sig.symbol, 'label': sig.label, 'status': 'REJECTED',
                          'reason': 'market mapping failed', 'created_at': datetime.now(UK).isoformat(), 'source': source})
            return False, 'market mapping failed'

        epic = market.get('epic')

        # Demo data-collection mode may hold several positions at once, but do
        # not stack repeated signals on the exact same IG market. This lets us
        # collect concurrent DAX/FTSE/Gold/FX samples without accidentally
        # pyramiding DAX-on-DAX (or any other duplicate epic).
        for row in positions:
            open_market = row.get('market') or {}
            open_position = row.get('position') or {}
            open_epic = open_market.get('epic') or open_position.get('epic')
            if open_epic and str(open_epic) == str(epic):
                telegram_notify(
                    f'⏸ IG DEMO AUTO: {sig.label} already has an open position; duplicate signal skipped.'
                )
                return False, 'same instrument already open'

        details = self.market_details(epic)
        if not details:
            return False, 'market details unavailable'
        instrument = details.get('instrument') or {}
        snapshot = details.get('snapshot') or {}
        rules = details.get('dealingRules') or {}
        status = str(snapshot.get('marketStatus') or market.get('marketStatus') or '').upper()
        if status != 'TRADEABLE':
            return False, f'market {status or "not tradeable"}'
        if str(rules.get('marketOrderPreference') or '').upper() == 'NOT_AVAILABLE':
            return False, 'market orders unavailable'
        if not bool(instrument.get('stopsLimitsAllowed', True)):
            return False, 'stops/limits unavailable'

        bid = _safe_float(snapshot.get('bid'))
        offer = _safe_float(snapshot.get('offer'))
        if bid is None or offer is None:
            return False, 'IG quote unavailable'
        ig_entry = offer if sig.side == 'LONG' else bid
        original_risk_distance = abs(float(sig.price) - float(sig.stop))
        spread = abs(offer - bid)
        spread_r_fraction = spread / max(original_risk_distance, 1e-12)
        max_spread_r_fraction = float(self.cfg.get('scanner', {}).get('max_spread_r_fraction', 0.20))
        if spread_r_fraction > max_spread_r_fraction:
            telegram_notify(
                f'⏸ IG DEMO AUTO — {sig.label}\nSpread consumes ~{spread_r_fraction*100:.0f}% of planned 1R; ' 
                f'above {max_spread_r_fraction*100:.0f}% limit. No order placed.'
            )
            return False, 'spread too large versus stop'
        zone_buffer = max(0.05 * original_risk_distance, spread * 2.0)
        zone_low = float(sig.entry_low) - zone_buffer
        zone_high = float(sig.entry_high) + zone_buffer
        inside_entry_zone = zone_low <= ig_entry <= zone_high

        # Cash-index feeds (Yahoo) and IG's corresponding CFD can carry a small
        # basis offset even at the same moment.  For indices only, permit a
        # modest OUTSIDE-zone quote when it is favourable to the intended side:
        # lower for a LONG, higher for a SHORT.  Never use this exception to
        # chase a worse price.  Forex/metals keep the original strict zone rule.
        index_basis_accepted = False
        index_basis_tolerance = 0.0
        if not inside_entry_zone and sig.market_type == 'index':
            index_basis_tolerance = min(
                max(0.25 * original_risk_distance, 3.0 * spread),
                0.0015 * abs(float(sig.price)),
            )
            if sig.side == 'LONG' and ig_entry < zone_low:
                favourable_gap = zone_low - ig_entry
                index_basis_accepted = favourable_gap <= index_basis_tolerance
            elif sig.side == 'SHORT' and ig_entry > zone_high:
                favourable_gap = ig_entry - zone_high
                index_basis_accepted = favourable_gap <= index_basis_tolerance

            if index_basis_accepted:
                basis_diff = ig_entry - float(sig.price)
                print(
                    f'[{datetime.now().strftime("%H:%M:%S")}] {sig.symbol}: '
                    f'IG index quote accepted via favourable basis tolerance | '
                    f'basis {basis_diff:+.2f} | tolerance {index_basis_tolerance:.2f}'
                )

        if not inside_entry_zone and not index_basis_accepted:
            dec = decimals_for_price(ig_entry)
            if sig.market_type == 'index':
                telegram_notify(
                    f'⏸ IG DEMO AUTO — {sig.label}\nIG price {ig_entry:.{dec}f} is outside adjusted index entry tolerance. '
                    'No order placed; cash-index/CFD mismatch is too large or the quote moved in the chase direction.'
                )
                return False, 'outside adjusted index entry tolerance'
            telegram_notify(
                f'⏸ IG DEMO AUTO — {sig.label}\nIG price {ig_entry:.{dec}f} is outside the scanner entry zone. '
                'No order placed; waiting avoids chasing/feed mismatch.'
            )
            return False, 'outside entry zone'

        # IG quotes dealing-rule POINTS in pip/point units, not raw price units.
        # Convert those rules with scalingFactor before comparing them with the
        # scanner's stop/target distances. For GBP/USD, for example, 4 POINTS
        # becomes 0.0004 when scalingFactor is 10000.
        scaling = _safe_float(snapshot.get('scalingFactor'), _safe_float(market.get('scalingFactor')))
        if scaling is None or scaling <= 0:
            telegram_notify(f'⚠️ IG DEMO AUTO: scaling metadata unavailable for {sig.label}; skipped rather than guess stop distance.')
            return False, 'scaling metadata unavailable'

        stop_distance = original_risk_distance
        limit_distance = abs(float(sig.tp1) - float(sig.price))
        min_dist = self._rule_points(rules.get('minNormalStopOrLimitDistance'), ig_entry, scaling)
        if min_dist > 0:
            stop_distance = max(stop_distance, min_dist * 1.05)
            limit_distance = max(limit_distance, min_dist * 1.05)

        # IG CFDs are often dealt in the instrument's quote/deal currency rather
        # than the account base currency.  E.g. EUR/USD normally deals in USD.
        # IG's market-details response tells us which currencyCode is valid and
        # supplies baseExchangeRate for translating that currency back to the
        # active account's base currency (GBP here).  This lets us size the stop
        # in GBP without inventing a GBP deal currency that the market does not
        # support.
        currencies = [c for c in (instrument.get('currencies') or []) if isinstance(c, dict)]
        if not currencies:
            telegram_notify(f'⚠️ IG DEMO AUTO: no deal-currency metadata for {sig.label}; skipped rather than guess sizing.')
            return False, 'no deal currency metadata'

        account_ccy = str(self.currency or 'GBP').upper()
        # Prefer the account currency whenever IG explicitly allows it.  This is
        # especially important for index markets where IG may expose a GBP deal
        # currency alongside local-currency contracts.
        deal_ccy_obj = (
            next((c for c in currencies if str(c.get('code') or '').upper() == account_ccy), None)
            or next((c for c in currencies if bool(c.get('isDefault'))), None)
            or currencies[0]
        )
        deal_currency = str(deal_ccy_obj.get('code') or '').upper()
        if not deal_currency:
            telegram_notify(f'⚠️ IG DEMO AUTO: IG did not provide a valid deal currency for {sig.label}; skipped.')
            return False, 'invalid deal currency'

        if deal_currency == account_ccy:
            to_account_rate = 1.0
        else:
            # baseExchangeRate is IG's market-provided conversion into the
            # account base currency.  Do not fall back to a guessed reciprocal
            # or a stale external FX quote; if IG omits it we simply skip.
            to_account_rate = _safe_float(deal_ccy_obj.get('baseExchangeRate'))
            if to_account_rate is None or to_account_rate <= 0:
                telegram_notify(
                    f'⚠️ IG DEMO AUTO: no safe {deal_currency}→{account_ccy} conversion for {sig.label}; '
                    'skipped rather than guess risk.'
                )
                return False, 'conversion unavailable'

        pip_value = _number_from_text(instrument.get('valueOfOnePip'))
        if pip_value is None or pip_value <= 0:
            telegram_notify(f'⚠️ IG DEMO AUTO: risk metadata unavailable for {sig.label}; skipped rather than guess size.')
            return False, 'risk metadata unavailable'

        risk_per_size_deal = stop_distance * scaling * pip_value
        risk_per_size_gbp = risk_per_size_deal * to_account_rate
        target_risk = float(sig.risk_gbp)
        if risk_per_size_gbp <= 0:
            return False, 'invalid risk per size'
        raw_size = target_risk / risk_per_size_gbp
        min_size = _safe_float((rules.get('minDealSize') or {}).get('value'), 0.0) or 0.0
        size = self._size_floor(raw_size, min_size if min_size > 0 else 0.01)
        if size < min_size:
            size = min_size
        estimated_risk = size * risk_per_size_gbp
        if size <= 0:
            return False, 'size <= 0'
        if estimated_risk > target_risk * 1.25:
            stop_points = stop_distance * scaling
            telegram_notify(
                f'⚠️ IG DEMO AUTO: minimum practical {sig.label} size estimates ~£{estimated_risk:.2f} stop risk, '
                f'above ~£{target_risk:.2f}; skipped.\n'
                f'ℹ️ IG sizing check: stop ≈ {stop_points:.1f} points | min size {min_size:g} | pip value {pip_value:g}.'
            )
            return False, 'min size exceeds risk'

        expiry = str(instrument.get('expiry') or market.get('expiry') or 'DFB')
        direction = 'BUY' if sig.side == 'LONG' else 'SELL'
        deal_ref = ('MS' + hashlib.sha1(f'{sig_id}{time.time()}'.encode()).hexdigest()[:18]).upper()
        precision = max(1, decimals_for_price(ig_entry))

        # IMPORTANT: IG's /positions/otc stopDistance and limitDistance use the
        # instrument's dealing point/pip units, not raw quote-price differences.
        # Our scanner stores distances in raw price units, so convert them back
        # with scalingFactor before submitting the order. Example: GBP/USD raw
        # distance 0.0008 with scalingFactor 10000 => 8.0 IG points/pips.
        stop_distance_points = stop_distance * scaling
        limit_distance_points = limit_distance * scaling
        if stop_distance_points <= 0 or limit_distance_points <= 0:
            telegram_notify(f'⚠️ IG DEMO AUTO: invalid attached-order distance for {sig.label}; skipped.')
            return False, 'invalid attached-order distance'

        payload = {
            'currencyCode': deal_currency,
            'dealReference': deal_ref,
            'direction': direction,
            'epic': epic,
            'expiry': expiry,
            'forceOpen': True,
            'guaranteedStop': False,
            'orderType': 'MARKET',
            'size': size,
            'stopDistance': round(stop_distance_points, 4),
            'limitDistance': round(limit_distance_points, 4),
            'timeInForce': 'FILL_OR_KILL',
            'trailingStop': False,
        }
        r, data = self.request('POST', '/positions/otc', '2', payload=payload)
        if r is None or not r.ok or not isinstance(data, dict) or not data.get('dealReference'):
            err = data.get('errorCode') if isinstance(data, dict) else None
            telegram_notify(f'❌ IG DEMO ORDER REJECTED — {sig.label}\n{err or ("HTTP " + str(r.status_code) if r is not None else "request failed")}')
            self._record({'signal_id': sig_id, 'symbol': sig.symbol, 'label': sig.label, 'status': 'REJECTED',
                          'reason': err or 'order request failed', 'created_at': datetime.now(UK).isoformat(),
                          'source': source, 'epic': epic, 'size': size})
            return False, 'order rejected'

        deal_reference = data.get('dealReference')
        confirm = None
        for _ in range(8):
            time.sleep(0.8)
            cr, cd = self.request('GET', f'/confirms/{deal_reference}', '1')
            if cr is not None and cr.ok and isinstance(cd, dict):
                confirm = cd
                if cd.get('dealStatus') in {'ACCEPTED', 'REJECTED'}:
                    break
        if not confirm or confirm.get('dealStatus') != 'ACCEPTED':
            reason = (confirm or {}).get('reason') or (confirm or {}).get('dealStatus') or 'confirmation unavailable'
            telegram_notify(
                f'❌ IG DEMO ORDER NOT ACCEPTED — {sig.label}\n'
                f'Reason: {reason}\n'
                f'ℹ️ Sent stop distance: {payload.get("stopDistance")} IG points | '
                f'limit distance: {payload.get("limitDistance")} IG points | scaling {scaling:g}'
            )
            self._record({'signal_id': sig_id, 'symbol': sig.symbol, 'label': sig.label, 'status': 'REJECTED',
                          'reason': str(reason), 'created_at': datetime.now(UK).isoformat(), 'source': source,
                          'epic': epic, 'size': size, 'deal_reference': deal_reference})
            return False, 'confirmation rejected'

        deal_id = confirm.get('dealId')
        fill_level = _safe_float(confirm.get('level'), ig_entry)
        stop_level = _safe_float(confirm.get('stopLevel'))
        limit_level = _safe_float(confirm.get('limitLevel'))
        rec = {
            'signal_id': sig_id, 'symbol': sig.symbol, 'label': sig.label, 'market_type': sig.market_type,
            'side': sig.side, 'status': 'OPEN', 'source': source, 'created_at': datetime.now(UK).isoformat(),
            'deal_reference': deal_reference, 'deal_id': deal_id, 'epic': epic,
            'ig_market_name': market.get('instrumentName'), 'size': size, 'fill_level': fill_level,
            'stop_level': stop_level, 'limit_level': limit_level, 'estimated_risk_gbp': estimated_risk,
            'risk_budget_gbp': target_risk,
            'deal_currency': deal_currency,
            'deal_to_account_rate': to_account_rate,
            'spread_r_fraction': spread_r_fraction,
            'decision_context': dict(getattr(sig, 'context', {}) or {}),
        }
        self._record(rec)
        dec = decimals_for_price(fill_level)
        lines = [
            f'🤖🧪 IG DEMO TRADE OPENED — {sig.label}',
            f'{"🟢 LONG" if sig.side == "LONG" else "🔴 SHORT"} | {source} signal',
            f'IG fill: {fill_level:.{dec}f}',
            f'Size: {size:g} | Deal currency: {deal_currency}',
            f'💷 Estimated stop risk: ~£{estimated_risk:.2f} (budget ~£{target_risk:.2f})',
            '🔒 DEMO only — whole position exits at TP1 in phase 1.',
        ]
        if stop_level is not None:
            lines.insert(3, f'🛑 Stop: {stop_level:.{dec}f}')
        if limit_level is not None:
            lines.insert(4, f'🎯 TP1: {limit_level:.{dec}f}')
        telegram_notify('\n'.join(lines))
        return True, 'opened'

    def _closure_outcome(self, deal_id):
        r, data = self.request('GET', '/history/activity', '3', params={'dealId': deal_id, 'detailed': 'true', 'pageSize': 50})
        if r is None or not r.ok or not isinstance(data, dict):
            return None, None
        action_types, close_level = [], None
        for activity in data.get('activities') or []:
            details = activity.get('details') or {}
            if close_level is None:
                close_level = _safe_float(details.get('level'))
            for action in details.get('actions') or []:
                t = str(action.get('actionType') or '').upper()
                if t:
                    action_types.append(t)
            desc = (str(activity.get('description') or '') + ' ' + str(details)).lower()
            if 'stop' in desc and 'filled' in desc:
                action_types.append('STOP_ORDER_FILLED')
            if 'limit' in desc and 'filled' in desc:
                action_types.append('LIMIT_ORDER_FILLED')
        if 'STOP_ORDER_FILLED' in action_types:
            return 'STOP', close_level
        if 'LIMIT_ORDER_FILLED' in action_types:
            return 'TP1', close_level
        return 'CLOSED_OTHER', close_level

    def monitor(self):
        if not self.configured:
            return
        data = load_ig_execution_state()
        open_recs = [x for x in data.get('trades', []) if x.get('status') == 'OPEN' and x.get('deal_id')]
        if not open_recs:
            return
        positions = self.open_positions()
        if positions is None:
            return
        remote_ids = self._deal_ids(positions)
        changed = False
        for rec in open_recs:
            if str(rec.get('deal_id')) in remote_ids:
                continue
            outcome, close_level = self._closure_outcome(str(rec.get('deal_id')))
            if outcome is None:
                continue
            rec['status'] = outcome
            rec['closed_at'] = datetime.now(UK).isoformat()
            rec['close_level'] = close_level
            rec['result_r'] = float(self.cfg['risk'].get('tp1_r_multiple', 1.5)) if outcome == 'TP1' else -1.0 if outcome == 'STOP' else 0.0
            changed = True
            icon = '✅' if outcome == 'TP1' else '🔴' if outcome == 'STOP' else 'ℹ️'
            detail = 'TP1 filled' if outcome == 'TP1' else 'stop filled' if outcome == 'STOP' else 'position closed by another reason'
            # Save before stats so the summary includes this result.
            save_ig_execution_state(data)
            telegram_notify(
                f'{icon} IG DEMO RESULT — {rec.get("label", rec.get("symbol"))}\n'
                f'{detail}\nResult: {rec["result_r"]:+.1f}R\n{self.stats_line()}'
            )
        if changed:
            save_ig_execution_state(data)


def maybe_execute_ig_demo(sig, source='5m'):
    global IG_EXECUTOR
    if IG_EXECUTOR is None:
        return
    try:
        opened, reason = IG_EXECUTOR.execute_signal(sig, source=source)
        print(f'IG demo auto {sig.symbol}: {"OPENED" if opened else "SKIP"} — {reason}')
    except Exception as e:
        print(f'IG demo auto {sig.symbol}: ERROR {e}')
        telegram_notify(f'⚠️ IG DEMO AUTO internal error on {sig.label}: {type(e).__name__}. Demo order not placed.')


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
    """Fetch all Twelve Data symbols in ONE batch request.

    /time_series costs 1 credit per symbol. With our 5-symbol FX/metals list this
    consumes 5 credits per scan, below the Basic plan's 8-credit/minute cap.
    """
    if not items:
        return {}
    key = os.getenv('TWELVE_DATA_API_KEY', '').strip()
    if not key:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data: API key missing; skip FX/metals')
        return {i['symbol']: None for i in items}
    if not twelve_data_active(cfg):
        print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data: session paused to protect free API quota')
        return {i['symbol']: None for i in items}

    data_symbols = [i.get('data_symbol', i['symbol']) for i in items]
    requested = ','.join(data_symbols)
    try:
        r = requests.get(
            'https://api.twelvedata.com/time_series',
            params={
                'symbol': requested,
                'interval': '5min',
                'outputsize': 120,
                'timezone': 'UTC',
                'apikey': key,
            },
            timeout=30,
        )
        try:
            data = r.json()
        except Exception:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data batch: invalid response HTTP {r.status_code}')
            return {i['symbol']: None for i in items}

        used = r.headers.get('api-credits-used')
        left = r.headers.get('api-credits-left')
        if used is not None or left is not None:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data batch: credits used={used or "?"}, left={left or "?"}')

        if not r.ok or (isinstance(data, dict) and data.get('status') == 'error'):
            msg = data.get('message', r.text[:220]) if isinstance(data, dict) else r.text[:220]
            print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data batch error: {msg}')
            return {i['symbol']: None for i in items}

        out = {}
        # Multi-symbol batch responses are keyed by symbol. Be tolerant of
        # exact/upper-case key variations. If the provider unexpectedly returns
        # a single payload, use it only when a single symbol was requested.
        for item, ds in zip(items, data_symbols):
            payload = None
            if isinstance(data, dict):
                payload = data.get(ds)
                if payload is None:
                    payload = data.get(ds.upper())
                if payload is None:
                    for k, v in data.items():
                        if isinstance(k, str) and k.upper() == ds.upper():
                            payload = v
                            break
                if payload is None and len(items) == 1 and 'values' in data:
                    payload = data

            if isinstance(payload, dict) and payload.get('status') == 'error':
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {ds}: Twelve Data error: {payload.get("message", "unknown error")}')
                out[item['symbol']] = None
            else:
                out[item['symbol']] = _twelvedata_frame(payload)
        return out
    except Exception as e:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] Twelve Data batch fetch failed: {e}')
        return {i['symbol']: None for i in items}


def fetch_twelvedata_1m(candidate, cfg):
    key = os.getenv('TWELVE_DATA_API_KEY', '').strip()
    if not key or not twelve_data_active(cfg):
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


def make_entry_zone(entry_price, stop_price, cfg):
    """Create a narrow entry range from the trade's initial risk distance.

    Default half-width is 0.10R, so an 18-point stop produces an entry zone
    roughly +/-1.8 points around the trigger. This keeps alerts practical
    without encouraging users to chase a move that has already run.
    """
    risk_per_unit = abs(float(entry_price) - float(stop_price))
    frac = float(cfg.get('scanner', {}).get('entry_zone_r_fraction', 0.10))
    half = max(0.0, risk_per_unit * frac)
    return float(entry_price - half), float(entry_price + half)


def _resample_ohlc(df, rule):
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    if 'Volume' in d.columns:
        agg['Volume'] = 'sum'
    try:
        out = d.resample(rule, label='right', closed='right').agg(agg)
    except Exception:
        return pd.DataFrame()
    return out.dropna(subset=['Open', 'High', 'Low', 'Close'])


def _regime_from_frame(frame, timeframe):
    """Classify price regime with deliberately low-correlation evidence.

    Uses only price/ATR/EMA slope so it works for Yahoo and Twelve Data without
    extra API calls or dependencies. 1h can be built from the 5m cache even on
    the smaller Twelve Data history, so its thresholds adapt to sample length.
    """
    if frame is None or len(frame) < 6:
        return {'name': 'UNKNOWN', 'strength': 0.0, 'slope_atr': 0.0, 'sep_atr': 0.0}
    d = frame.copy()
    n = len(d)
    if timeframe == '1h' and n < 16:
        fast_n, mid_n, slow_n = 3, 6, min(9, max(7, n - 1))
    else:
        fast_n, mid_n, slow_n = 5, 13, 21
    d['FAST'] = ema(d['Close'], fast_n)
    d['MID'] = ema(d['Close'], mid_n)
    d['SLOW'] = ema(d['Close'], slow_n)
    atr_n = min(14, max(3, n // 3))
    d['ATR_R'] = atr(d, atr_n)
    row = d.iloc[-1]
    atr_v = float(row['ATR_R']) if pd.notna(row['ATR_R']) and row['ATR_R'] > 0 else float((d['High'] - d['Low']).tail(6).mean())
    if not np.isfinite(atr_v) or atr_v <= 0:
        return {'name': 'UNKNOWN', 'strength': 0.0, 'slope_atr': 0.0, 'sep_atr': 0.0}
    lookback = min(3, n - 1)
    mid_prev = float(d['MID'].iloc[-1-lookback])
    mid_now = float(row['MID'])
    slope_atr = (mid_now - mid_prev) / atr_v / max(1, lookback)
    sep_atr = (float(row['FAST']) - float(row['MID'])) / atr_v
    close_rel = (float(row['Close']) - mid_now) / atr_v
    bull = float(row['FAST']) > float(row['MID']) and mid_now >= float(row['SLOW']) and slope_atr > 0.025 and close_rel > -0.10
    bear = float(row['FAST']) < float(row['MID']) and mid_now <= float(row['SLOW']) and slope_atr < -0.025 and close_rel < 0.10
    strength = min(1.0, 0.45 * min(1.0, abs(slope_atr) / 0.12) + 0.35 * min(1.0, abs(sep_atr) / 0.45) + 0.20 * min(1.0, abs(close_rel) / 0.75))
    if bull:
        name = 'TREND_UP'
    elif bear:
        name = 'TREND_DOWN'
    else:
        name = 'RANGE_CHOP'
        strength = min(strength, 0.55)
    return {'name': name, 'strength': float(strength), 'slope_atr': float(slope_atr), 'sep_atr': float(sep_atr)}


def _session_bucket(ts=None):
    try:
        t = pd.Timestamp(ts) if ts is not None else pd.Timestamp.now(tz='UTC')
        if t.tzinfo is None:
            t = t.tz_localize('UTC')
        local = t.tz_convert(UK)
        mins = local.hour * 60 + local.minute
    except Exception:
        mins = datetime.now(UK).hour * 60 + datetime.now(UK).minute
    if 7*60 <= mins < 9*60:
        return 'EUROPE_OPEN'
    if 9*60 <= mins < 11*60+30:
        return 'EUROPE_MORNING'
    if 11*60+30 <= mins < 13*60+30:
        return 'LUNCH'
    if 13*60+30 <= mins < 16*60+30:
        return 'NY_OVERLAP'
    return 'OTHER'


def _swing_levels(d, lookback=36):
    x = d.tail(lookback).copy()
    if len(x) < 7:
        return [], []
    highs = x['High'].astype(float).values
    lows = x['Low'].astype(float).values
    sh, sl = [], []
    for i in range(2, len(x)-2):
        if highs[i] >= max(highs[i-2:i]) and highs[i] > max(highs[i+1:i+3]):
            sh.append(float(highs[i]))
        if lows[i] <= min(lows[i-2:i]) and lows[i] < min(lows[i+1:i+3]):
            sl.append(float(lows[i]))
    return sh, sl


def _atr_percentile(d, current_atr):
    vals = atr(d, 14).dropna().tail(60).astype(float)
    if len(vals) < 10 or not np.isfinite(current_atr):
        return 50.0
    return float((vals <= current_atr).mean() * 100.0)


def _decision_snapshot(df, item, cfg):
    """Return independent Decision Engine v2 components for LONG and SHORT."""
    if df is None or len(df) < 65:
        return None
    d = df.copy()
    d['EMA9'] = ema(d['Close'], 9)
    d['EMA20'] = ema(d['Close'], 20)
    d['EMA50'] = ema(d['Close'], 50)
    d['RSI'] = rsi(d['Close'], 14)
    d['ATR'] = atr(d, 14)
    row = d.iloc[-2]
    prev = d.iloc[-3]
    live = d.iloc[-1]
    if pd.isna(row['ATR']) or row['ATR'] <= 0 or pd.isna(row['RSI']):
        return None

    completed = d.iloc[:-1].copy()
    tf15 = _resample_ohlc(completed, '15min')
    tf1h = _resample_ohlc(completed, '1h')
    reg15 = _regime_from_frame(tf15, '15m')
    reg1h = _regime_from_frame(tf1h, '1h')

    close = float(row['Close']); op = float(row['Open']); high = float(row['High']); low = float(row['Low'])
    prev_high = float(prev['High']); prev_low = float(prev['Low']); prev_close = float(prev['Close'])
    live_price = float(live['Close']); atr_v = float(row['ATR'])
    candle_range = max(high - low, 1e-12)
    body = abs(close - op)
    body_ratio = body / candle_range
    close_pos = (close - low) / candle_range
    atr_pct = _atr_percentile(completed, atr_v)

    hist = completed.iloc[:-1].tail(40)
    sh, sl = _swing_levels(hist, 36)
    resistance = [x for x in sh if x > close]
    support = [x for x in sl if x < close]
    nearest_res = min(resistance) if resistance else None
    nearest_sup = max(support) if support else None
    res_dist_atr = ((nearest_res - close) / atr_v) if nearest_res is not None else 99.0
    sup_dist_atr = ((close - nearest_sup) / atr_v) if nearest_sup is not None else 99.0
    prior_high20 = float(hist['High'].tail(20).max()) if not hist.empty else prev_high
    prior_low20 = float(hist['Low'].tail(20).min()) if not hist.empty else prev_low
    midpoint = (prior_high20 + prior_low20) / 2.0

    ema_sep = abs(float(row['EMA20']) - float(row['EMA50'])) / atr_v
    ema_slope = (float(row['EMA20']) - float(d['EMA20'].iloc[-6])) / atr_v / 4.0
    trend_strength = min(1.0, 0.55 * min(1.0, ema_sep / 0.55) + 0.45 * min(1.0, abs(ema_slope) / 0.12))
    vol_ok = 12.0 <= atr_pct <= 97.5
    vol_extreme = atr_pct > 99.0

    results = {}
    for side in ('LONG', 'SHORT'):
        long = side == 'LONG'
        opposite15 = reg15['name'] == ('TREND_DOWN' if long else 'TREND_UP') and reg15['strength'] >= 0.50
        strong_opposite1h = reg1h['name'] == ('TREND_DOWN' if long else 'TREND_UP') and reg1h['strength'] >= 0.78
        veto = bool(opposite15 or strong_opposite1h or vol_extreme)
        veto_reasons = []
        if opposite15: veto_reasons.append('15m regime opposite')
        if strong_opposite1h: veto_reasons.append('1h strongly opposite')
        if vol_extreme: veto_reasons.append('extreme volatility')

        same15 = reg15['name'] == ('TREND_UP' if long else 'TREND_DOWN')
        same1h = reg1h['name'] == ('TREND_UP' if long else 'TREND_DOWN')
        htf = 0.0
        if same15:
            htf += 1.25 + (0.25 if reg15['strength'] >= 0.70 else 0.0)
        elif reg15['name'] == 'RANGE_CHOP':
            htf += 0.5
        if same1h:
            htf += 0.5
        elif reg1h['name'] == 'RANGE_CHOP' or reg1h['name'] == 'UNKNOWN':
            htf += 0.25
        htf = min(2.0, htf)

        if long:
            directional_structure = close > midpoint and float(row['EMA20']) >= float(row['EMA50'])
            clean_break = close > prior_high20
            room = res_dist_atr
            into_level = (nearest_res is not None and res_dist_atr < 0.35 and not clean_break)
        else:
            directional_structure = close < midpoint and float(row['EMA20']) <= float(row['EMA50'])
            clean_break = close < prior_low20
            room = sup_dist_atr
            into_level = (nearest_sup is not None and sup_dist_atr < 0.35 and not clean_break)
        structure = (1.0 if directional_structure else 0.0) + (1.0 if clean_break or room >= 0.75 else 0.0)
        if into_level:
            structure = min(structure, 0.5)
            veto_reasons.append('breakout into nearby structure')
            if room < 0.20:
                veto = True

        tv = 0.0
        aligned_slope = ema_slope > 0.025 if long else ema_slope < -0.025
        if vol_ok and aligned_slope and trend_strength >= 0.30:
            tv = 1.0
        elif vol_ok and (aligned_slope or trend_strength >= 0.25):
            tv = 0.5

        if long:
            breakout = close > prev_high
            directional_candle = close > op and close_pos >= 0.62
        else:
            breakout = close < prev_low
            directional_candle = close < op and close_pos <= 0.38
        setup5 = 1.0 if breakout and directional_candle and body_ratio >= 0.45 else (0.5 if directional_candle and body_ratio >= 0.30 else 0.0)

        rsi_v = float(row['RSI'])
        if long:
            rsi_ok = 52 <= rsi_v <= 69
            price_mom = close > prev_close and close > float(row['EMA9'])
            exhausted = rsi_v > 76 or (close - float(row['EMA20'])) > 1.25 * atr_v
        else:
            rsi_ok = 31 <= rsi_v <= 48
            price_mom = close < prev_close and close < float(row['EMA9'])
            exhausted = rsi_v < 24 or (float(row['EMA20']) - close) > 1.25 * atr_v
        momentum = 1.0 if rsi_ok and price_mom and not exhausted else (0.5 if (rsi_ok or price_mom) and not exhausted else 0.0)
        if exhausted:
            veto_reasons.append('momentum exhausted')

        score = htf + structure + tv + setup5 + momentum  # max 7 before 1m execution
        components = {
            'htf_regime': round(htf, 2),
            'structure_location': round(structure, 2),
            'trend_volatility': round(tv, 2),
            'setup_5m': round(setup5, 2),
            'momentum_quality': round(momentum, 2),
            'trigger_1m': 0.0,
        }
        results[side] = {
            'score': float(score), 'veto': veto, 'veto_reasons': veto_reasons,
            'components': components, 'breakout': bool(breakout), 'directional_candle': bool(directional_candle),
        }

    context = {
        'engine': 'v2',
        'session': _session_bucket(row.name),
        'regime_15m': reg15['name'], 'regime_15m_strength': round(reg15['strength'], 3),
        'regime_1h': reg1h['name'], 'regime_1h_strength': round(reg1h['strength'], 3),
        'atr_value': atr_v, 'atr_percentile': round(atr_pct, 1),
        'trend_strength': round(trend_strength, 3),
        'nearest_resistance_atr': round(float(res_dist_atr), 3),
        'nearest_support_atr': round(float(sup_dist_atr), 3),
        'candle_body_ratio': round(float(body_ratio), 3),
        'live_price': live_price,
    }
    return {'d': d, 'row': row, 'prev': prev, 'live': live, 'atr': atr_v, 'close': close, 'live_price': live_price,
            'results': results, 'context': context}


def _v2_reason(side_result, context):
    c = side_result['components']
    return (
        f"HTF {context['regime_15m']}/{context['regime_1h']} {c['htf_regime']:.1f}/2, "
        f"structure {c['structure_location']:.1f}/2, trend/vol {c['trend_volatility']:.1f}/1, "
        f"5m {c['setup_5m']:.1f}/1, momentum {c['momentum_quality']:.1f}/1"
    )


def score_signal(df, item, cfg):
    snap = _decision_snapshot(df, item, cfg)
    if not snap:
        return None
    row, prev = snap['row'], snap['prev']
    live_price, atr_v = snap['live_price'], snap['atr']
    min_score = float(cfg['scanner']['minimum_score'])
    direct_min_score = min_score  # Aggressive v2: direct CFD entries allowed from 6.0/8
    candidates = []
    for side in ('LONG', 'SHORT'):
        r = snap['results'][side]
        if not r['veto'] and r['breakout'] and r['directional_candle'] and r['score'] >= direct_min_score:
            candidates.append((r['score'], side, r))
    if not candidates:
        return None
    _, side, side_result = max(candidates, key=lambda x: x[0])

    # Decision Engine v2 deliberately chases less than v1.
    trigger_close = float(row['Close'])
    if abs(live_price - trigger_close) > 0.60 * atr_v:  # Aggressive v2: looser direct anti-chase tolerance
        return None
    d = snap['d']
    recent_low = float(d['Low'].iloc[-10:-1].min())
    recent_high = float(d['High'].iloc[-10:-1].max())
    stop_mult = float(cfg['risk']['atr_stop_multiple'])
    price = live_price
    if side == 'LONG':
        stop = min(trigger_close - stop_mult * atr_v, recent_low - 0.10 * atr_v)
        risk_per_unit = price - stop
        if risk_per_unit <= 0: return None
        tp1 = price + float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = price + float(cfg['risk']['tp2_r_multiple']) * risk_per_unit
    else:
        stop = max(trigger_close + stop_mult * atr_v, recent_high + 0.10 * atr_v)
        risk_per_unit = stop - price
        if risk_per_unit <= 0: return None
        tp1 = price - float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = price - float(cfg['risk']['tp2_r_multiple']) * risk_per_unit

    account = float(cfg['risk']['account_cash_gbp'])
    risk_gbp = account * float(cfg['risk']['risk_per_trade_pct']) / 100.0
    stop_pct = risk_per_unit / price
    max_exposure = account * float(cfg['risk']['max_cash_exposure_pct']) / 100.0
    suggested = min(max_exposure, risk_gbp / stop_pct) if stop_pct > 0 else None
    entry_low, entry_high = make_entry_zone(price, stop, cfg)
    context = dict(snap['context'])
    context['score_components'] = dict(side_result['components'])
    context['veto_reasons'] = list(side_result['veto_reasons'])
    return Signal(
        symbol=item['symbol'], label=item.get('name', item['symbol']), market_type=item.get('type', 'stock'),
        side=side, price=float(price), stop=float(stop), tp1=float(tp1), tp2=float(tp2),
        entry_low=entry_low, entry_high=entry_high, score=float(side_result['score']),
        reason=_v2_reason(side_result, context), risk_gbp=float(risk_gbp),
        suggested_exposure_gbp=float(suggested) if suggested else None,
        timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'), context=context,
    )


def candidate_from_5m(df, item, cfg):
    snap = _decision_snapshot(df, item, cfg)
    if not snap:
        return None
    min_score = float(cfg['scanner']['minimum_score'])
    gap = float(cfg.get('scanner', {}).get('entry_watch_near_score_gap', 1.0))
    near_threshold = max(0.0, min_score - gap)
    live_price, atr_v = snap['live_price'], snap['atr']
    row = snap['row']

    choices = []
    for side in ('LONG', 'SHORT'):
        r = snap['results'][side]
        if r['veto'] or r['score'] < near_threshold:
            continue
        trigger = float(row['High']) if side == 'LONG' else float(row['Low'])
        distance = (trigger - live_price) if side == 'LONG' else (live_price - trigger)
        if distance > 0.50 * atr_v or distance < -0.29 * atr_v:  # Aggressive v2: allow slightly more extension before arming
            continue
        choices.append((r['score'], side, trigger, r))
    if not choices:
        return None
    score, side, trigger, side_result = max(choices, key=lambda x: x[0])
    d = snap['d']
    context = dict(snap['context'])
    context['score_components'] = dict(side_result['components'])
    context['veto_reasons'] = list(side_result['veto_reasons'])
    return Candidate(
        symbol=item['symbol'], label=item.get('name', item['symbol']), market_type=item.get('type', 'stock'),
        provider=item.get('provider', 'yahoo'), data_symbol=item.get('data_symbol', item['symbol']), side=side,
        score=float(score), trigger_level=float(trigger), atr_value=float(atr_v), anchor_close=float(snap['close']),
        recent_low=float(d['Low'].iloc[-10:-1].min()), recent_high=float(d['High'].iloc[-10:-1].max()),
        reason=_v2_reason(side_result, context), armed_at=time.time(), context=context,
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
    """Reserve enough of the free 800-credit/day plan for the 5m core scan."""
    td_count = sum(1 for i in cfg.get('watchlist', []) if i.get('provider') == 'twelvedata')
    sc = cfg.get('scanner', {})
    start = float(sc.get('twelvedata_active_start_hour_uk', 7))
    end = float(sc.get('twelvedata_active_end_hour_uk', 18))
    active_hours = max(0.0, end - start)
    estimated_core = int(td_count * 12 * active_hours)
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


def build_signal_from_candidate(c, entry_price, cfg, trigger_meta=None):
    stop_mult = float(cfg['risk']['atr_stop_multiple'])
    if c.side == 'LONG':
        stop = min(c.anchor_close - stop_mult * c.atr_value, c.recent_low - 0.10 * c.atr_value)
        risk_per_unit = entry_price - stop
        if risk_per_unit <= 0: return None
        tp1 = entry_price + float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = entry_price + float(cfg['risk']['tp2_r_multiple']) * risk_per_unit
    else:
        stop = max(c.anchor_close + stop_mult * c.atr_value, c.recent_high + 0.10 * c.atr_value)
        risk_per_unit = stop - entry_price
        if risk_per_unit <= 0: return None
        tp1 = entry_price - float(cfg['risk']['tp1_r_multiple']) * risk_per_unit
        tp2 = entry_price - float(cfg['risk']['tp2_r_multiple']) * risk_per_unit

    account = float(cfg['risk']['account_cash_gbp'])
    risk_gbp = account * float(cfg['risk']['risk_per_trade_pct']) / 100.0
    stop_pct = risk_per_unit / entry_price
    max_exposure = account * float(cfg['risk']['max_cash_exposure_pct']) / 100.0
    suggested = min(max_exposure, risk_gbp / stop_pct) if stop_pct > 0 else None
    entry_low, entry_high = make_entry_zone(entry_price, stop, cfg)
    context = dict(c.context or {})
    comps = dict(context.get('score_components') or {})
    comps['trigger_1m'] = 1.0
    context['score_components'] = comps
    if trigger_meta:
        context.update(trigger_meta)
    mode = context.get('trigger_mode', 'confirmed')
    return Signal(
        symbol=c.symbol, label=c.label, market_type=c.market_type, side=c.side, price=float(entry_price),
        stop=float(stop), tp1=float(tp1), tp2=float(tp2), entry_low=entry_low, entry_high=entry_high,
        score=float(min(8.0, c.score + 1.0)),
        reason=(c.reason + f', 1m {mode} confirmed').strip(', '), risk_gbp=float(risk_gbp),
        suggested_exposure_gbp=float(suggested) if suggested else None,
        timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'), context=context,
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
        f"✅ Entry zone: {s.entry_low:.{dec}f} – {s.entry_high:.{dec}f}\n"
        f"🚫 Outside the zone: WAIT for a retest/new alert\n"
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
            remaining.pop(symbol, None); continue
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
            if df is None or len(df) < 4:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m watcher data unavailable'); continue

            bar = df.iloc[-2]; prev = df.iloc[-3]; live = df.iloc[-1]
            close=float(bar['Close']); op=float(bar['Open']); high=float(bar['High']); low=float(bar['Low'])
            prev_close=float(prev['Close']); prev_high=float(prev['High']); prev_low=float(prev['Low'])
            live_price=float(live['Close']); rng=max(high-low, 1e-12); body=abs(close-op); body_ratio=body/rng
            close_pos=(close-low)/rng
            if c.side == 'LONG':
                breakout_now = prev_close <= c.trigger_level and close > c.trigger_level
                retest_hold = prev_close > c.trigger_level and low <= c.trigger_level + 0.05*c.atr_value and close > c.trigger_level
                quality = close > op and body_ratio >= (0.45 if breakout_now else 0.32) and close_pos >= 0.68 and (high-max(op,close))/rng <= 0.28
                too_far = live_price - c.trigger_level > 0.33 * c.atr_value
            else:
                breakout_now = prev_close >= c.trigger_level and close < c.trigger_level
                retest_hold = prev_close < c.trigger_level and high >= c.trigger_level - 0.05*c.atr_value and close < c.trigger_level
                quality = close < op and body_ratio >= (0.45 if breakout_now else 0.32) and close_pos <= 0.32 and (min(op,close)-low)/rng <= 0.28
                too_far = c.trigger_level - live_price > 0.33 * c.atr_value

            triggered = breakout_now or retest_hold
            if not triggered:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m watcher armed {c.side} @ {c.trigger_level:.5f}; waiting'); continue
            if not quality:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m trigger touched but candle quality weak; keep waiting')
                continue
            if too_far:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m trigger confirmed but entry stretched; skip')
                remaining.pop(symbol, None); continue

            mode = 'retest_hold' if retest_hold and not breakout_now else 'breakout'
            trigger_meta = {'trigger_mode': mode, 'trigger_body_ratio': round(float(body_ratio),3), 'trigger_close_position': round(float(close_pos),3)}
            sig = build_signal_from_candidate(c, live_price, cfg, trigger_meta=trigger_meta)
            if not sig:
                remaining.pop(symbol, None); continue

            sig_id = signature(sig); key=f'{symbol}:{sig.side}'; last=state.get(key,{})
            if (now_ts-last.get('time',0)) < cooldown_min*60 and last.get('signature') == sig_id:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: 1m trigger held; cooldown')
                remaining.pop(symbol,None); continue

            text=format_1m_signal(sig); print('\n'+text+'\n')
            delivered=telegram_notify(text) if cfg.get('notifications',{}).get('telegram',True) else True
            if delivered:
                state[key]={'time':now_ts,'signature':sig_id}
                item=next((i for i in cfg.get('watchlist',[]) if i.get('symbol')==sig.symbol),None)
                register_tracked_signal(sig,item)
                maybe_execute_ig_demo(sig, source='1m')
                remaining.pop(symbol,None)
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
        f"✅ Entry zone: {s.entry_low:.{dec}f} – {s.entry_high:.{dec}f}\n"
        f"🚫 Outside the zone: WAIT for a retest/new alert\n"
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


def storage_dir():
    """Use a Railway volume mounted at /data when present; otherwise fall back locally.

    A Railway volume is recommended if you want scanner history to survive
    redeploys/restarts. The scanner still works without one.
    """
    env_dir = os.getenv('SCANNER_DATA_DIR', '').strip()
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
    railway_mount = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '').strip()
    if railway_mount:
        p = Path(railway_mount)
        p.mkdir(parents=True, exist_ok=True)
        return p
    railway_volume = Path('/data')
    if railway_volume.exists() and os.access(railway_volume, os.W_OK):
        return railway_volume
    return BASE


def state_path():
    return storage_dir() / '.scanner_state.json'


def tracker_path():
    return storage_dir() / 'scanner_performance.json'


def load_state():
    p = state_path()
    if not p.exists(): return {}
    try: return json.loads(p.read_text())
    except Exception: return {}


def _atomic_json_write(path, payload):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    tmp.replace(path)


def save_state(s):
    _atomic_json_write(state_path(), s)


def load_tracker():
    p = tracker_path()
    if not p.exists():
        return {'signals': []}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('signals'), list):
            return {'signals': []}
        return data
    except Exception:
        return {'signals': []}


def save_tracker(data):
    _atomic_json_write(tracker_path(), data)


def track_id(sig: Signal):
    raw = f'{sig.symbol}|{sig.side}|{sig.price:.10f}|{sig.stop:.10f}|{sig.tp1:.10f}|{sig.tp2:.10f}|{sig.timestamp}'
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def register_tracked_signal(sig: Signal, item=None):
    data = load_tracker()
    tid = track_id(sig)
    if any(r.get('id') == tid for r in data['signals']):
        return tid

    # One theoretical thesis per symbol+direction at a time. This removes the
    # repeated DAX/USDJPY bookkeeping distortion without changing real IG logic.
    active_same = [r for r in data['signals'] if r.get('symbol') == sig.symbol and r.get('side') == sig.side and r.get('status') in {'OPEN','TP1_OPEN'}]
    if active_same:
        existing = active_same[-1]
        print(f'[{datetime.now().strftime("%H:%M:%S")}] tracker: duplicate thesis suppressed {sig.symbol} {sig.side}; active #{existing.get("id","?")}')
        return existing.get('id')

    provider = (item or {}).get('provider', 'yahoo')
    data_symbol = (item or {}).get('data_symbol', sig.symbol)
    ctx = dict(getattr(sig, 'context', {}) or {})
    rec = {
        'id': tid, 'symbol': sig.symbol, 'label': sig.label, 'market_type': sig.market_type,
        'provider': provider, 'data_symbol': data_symbol, 'side': sig.side,
        'entry': sig.price, 'entry_low': sig.entry_low, 'entry_high': sig.entry_high,
        'stop': sig.stop, 'tp1': sig.tp1, 'tp2': sig.tp2, 'score': sig.score,
        'risk_gbp': sig.risk_gbp, 'created_at': sig.timestamp, 'status': 'OPEN',
        'last_checked_bar': None, 'tp1_hit_at': None, 'closed_at': None, 'result_r': 0.0,
        'max_favorable_r': 0.0, 'max_adverse_r': 0.0,
        'reentry_armed_at': None, 'reentry_alerted_at': None,
        'decision_engine': ctx.get('engine','v1'), 'session': ctx.get('session'),
        'regime_15m': ctx.get('regime_15m'), 'regime_1h': ctx.get('regime_1h'),
        'atr_value': ctx.get('atr_value'), 'atr_percentile': ctx.get('atr_percentile'),
        'trend_strength': ctx.get('trend_strength'), 'nearest_resistance_atr': ctx.get('nearest_resistance_atr'),
        'nearest_support_atr': ctx.get('nearest_support_atr'), 'score_components': ctx.get('score_components'),
        'trigger_mode': ctx.get('trigger_mode'), 'trigger_body_ratio': ctx.get('trigger_body_ratio'),
    }
    data['signals'].append(rec); save_tracker(data)
    print(f'[{datetime.now().strftime("%H:%M:%S")}] tracker: registered {sig.symbol} {sig.side} #{tid}')
    return tid

def scanner_stats(data=None):
    data = data or load_tracker()
    rows = data.get('signals', [])
    win_statuses = {'TP1_OPEN', 'TP1_ONLY', 'TP2_HIT'}
    wins = sum(1 for r in rows if r.get('status') in win_statuses)
    losses = sum(1 for r in rows if r.get('status') == 'STOPPED')
    expired = sum(1 for r in rows if r.get('status') == 'EXPIRED')
    ambiguous = sum(1 for r in rows if r.get('status') == 'AMBIGUOUS')
    open_count = sum(1 for r in rows if r.get('status') in {'OPEN', 'TP1_OPEN'})
    resolved = wins + losses
    win_rate = (wins / resolved * 100.0) if resolved else 0.0
    total_r = sum(float(r.get('result_r', 0.0) or 0.0) for r in rows)
    return {
        'wins': wins, 'losses': losses, 'expired': expired, 'ambiguous': ambiguous,
        'open': open_count, 'resolved': resolved, 'win_rate': win_rate, 'total_r': total_r,
        'total_signals': len(rows),
    }


def tracker_summary_line(data=None):
    s = scanner_stats(data)
    wr = f'{s["win_rate"]:.0f}%' if s['resolved'] else 'n/a'
    return (
        f'🤖 Scanner record: {s["wins"]}W / {s["losses"]}L | '
        f'Win rate {wr} | Total {s["total_r"]:+.1f}R | Open {s["open"]}'
    )


def _notify_tracker_result(title, rec, detail, data):
    dec = decimals_for_price(float(rec['entry']))
    mfe = float(rec.get('max_favorable_r', 0.0) or 0.0)
    mae = float(rec.get('max_adverse_r', 0.0) or 0.0)
    text = (
        f'{title} — {rec["label"]}\n'
        f'{rec["side"]} from {float(rec["entry"]):.{dec}f} | score {float(rec.get("score", 0)):.1f}/8\n'
        f'{detail}\n'
        f'📏 MFE +{mfe:.2f}R | MAE -{mae:.2f}R\n'
        f'{tracker_summary_line(data)}'
    )
    print('\n' + text + '\n')
    telegram_notify(text)


def _bar_hits(rec, bar):
    high = float(bar['High'])
    low = float(bar['Low'])
    side = rec['side']
    if side == 'LONG':
        return low <= float(rec['stop']), high >= float(rec['tp1']), high >= float(rec['tp2'])
    return high >= float(rec['stop']), low <= float(rec['tp1']), low <= float(rec['tp2'])


def _maybe_notify_reentry(rec, bar, metrics, cfg, ts_iso):
    """Send one re-entry alert after a strong move, retest, and fresh confirmation.

    This does NOT create a second scanner-performance trade. It is a second-chance
    execution alert on the same original setup.
    """
    if rec.get('reentry_alerted_at') or not rec.get('reentry_armed_at'):
        return False
    armed_ts = pd.Timestamp(rec['reentry_armed_at'])
    bar_ts = pd.Timestamp(ts_iso)
    if armed_ts.tzinfo is None:
        armed_ts = armed_ts.tz_localize('UTC')
    if bar_ts.tzinfo is None:
        bar_ts = bar_ts.tz_localize('UTC')
    if bar_ts <= armed_ts:
        return False

    entry = float(rec['entry'])
    stop = float(rec['stop'])
    risk = abs(entry - stop)
    if risk <= 0:
        return False
    zone_low = float(rec.get('entry_low', entry - 0.10 * risk))
    zone_high = float(rec.get('entry_high', entry + 0.10 * risk))
    high = float(bar['High']); low = float(bar['Low']); close = float(bar['Close']); op = float(bar['Open'])
    touches_zone = low <= zone_high and high >= zone_low
    if not touches_zone:
        return False

    ema9 = float(metrics.get('EMA9', np.nan))
    ema20 = float(metrics.get('EMA20', np.nan))
    rsi_v = float(metrics.get('RSI', np.nan))
    max_chase_r = float(cfg.get('scanner', {}).get('reentry_max_chase_r', 0.25))
    if rec['side'] == 'LONG':
        structure_ok = ema9 > ema20 and rsi_v >= 50
        confirm = close > op and close >= entry and close <= entry + max_chase_r * risk
        emoji = '🟢'
    else:
        structure_ok = ema9 < ema20 and rsi_v <= 50
        confirm = close < op and close <= entry and close >= entry - max_chase_r * risk
        emoji = '🔴'
    if not (structure_ok and confirm):
        return False

    dec = decimals_for_price(entry)
    progress = float(rec.get('max_favorable_r', 0.0) or 0.0)
    risk_gbp = float(rec.get('risk_gbp', 0.0) or 0.0)
    text = (
        f"♻️ RE-ENTRY CONFIRMED — {rec['label']}\n"
        f"{emoji} {rec['side']} — original setup still valid\n"
        f"✅ Entry zone: {zone_low:.{dec}f} – {zone_high:.{dec}f}\n"
        f"📍 Fresh confirmation: {close:.{dec}f}\n"
        f"🛑 Stop: {float(rec['stop']):.{dec}f}\n"
        f"🎯 TP1: {float(rec['tp1']):.{dec}f}\n"
        f"🎯 TP2: {float(rec['tp2']):.{dec}f}\n"
        f"📈 Earlier move reached about +{progress:.1f}R, then retested entry and held\n"
        f"💷 Keep stop-out no more than ~£{risk_gbp:.2f}\n"
        f"⚠️ Same setup, second-chance entry — not counted as a new scanner trade. Don't chase outside the zone."
    )
    print('\n' + text + '\n')
    if telegram_notify(text):
        rec['reentry_alerted_at'] = ts_iso
        return True
    return False


def update_tracked_signals_for_symbol(df, item, cfg):
    """Update open signal outcomes from completed 5-minute bars, with no extra API calls.

    If a single OHLC bar touches both stop and target before we know which came
    first, the result is marked AMBIGUOUS rather than pretending we know the path.
    """
    if df is None or df.empty or len(df) < 2:
        return
    data = load_tracker()
    active = [r for r in data.get('signals', []) if r.get('symbol') == item['symbol'] and r.get('status') in {'OPEN', 'TP1_OPEN'}]
    if not active:
        return

    completed = df.iloc[:-1].copy()
    if completed.empty:
        return
    metrics_df = completed.copy()
    metrics_df['EMA9'] = ema(metrics_df['Close'], 9)
    metrics_df['EMA20'] = ema(metrics_df['Close'], 20)
    metrics_df['RSI'] = rsi(metrics_df['Close'], 14)
    changed = False

    for rec in active:
        try:
            created = pd.Timestamp(rec['created_at'])
            if created.tzinfo is None:
                created = created.tz_localize('UTC')
            else:
                created = created.tz_convert('UTC')
            # Backward-compatible migration for signals created before the
            # re-entry feature existed: reconstruct their best favorable move
            # from the already-fetched 5m history, without changing W/L results.
            if 'max_favorable_r' not in rec:
                entry0 = float(rec['entry']); stop0 = float(rec['stop'])
                risk0 = abs(entry0 - stop0)
                rec['max_favorable_r'] = 0.0
                rec.setdefault('reentry_armed_at', None)
                rec.setdefault('reentry_alerted_at', None)
                if risk0 > 0:
                    hist = completed.copy()
                    hist_idx = pd.to_datetime(hist.index, utc=True, errors='coerce')
                    hist = hist.loc[hist_idx >= created.floor('5min')]
                    if not hist.empty:
                        if rec['side'] == 'LONG':
                            moves = (hist['High'].astype(float) - entry0) / risk0
                        else:
                            moves = (entry0 - hist['Low'].astype(float)) / risk0
                        if not moves.empty:
                            best = float(max(0.0, moves.max()))
                            rec['max_favorable_r'] = best
                            threshold0 = float(cfg.get('scanner', {}).get('reentry_arm_r', 0.80 * float(cfg['risk'].get('tp1_r_multiple', 1.5))))
                            if best >= threshold0:
                                best_idx = moves.idxmax()
                                best_ts = pd.Timestamp(best_idx)
                                if best_ts.tzinfo is None:
                                    best_ts = best_ts.tz_localize('UTC')
                                else:
                                    best_ts = best_ts.tz_convert('UTC')
                                rec['reentry_armed_at'] = best_ts.isoformat()
                changed = True

            if 'max_adverse_r' not in rec:
                rec['max_adverse_r'] = 0.0
                entry0 = float(rec['entry']); stop0 = float(rec['stop']); risk0 = abs(entry0-stop0)
                if risk0 > 0:
                    hist = completed.copy()
                    hist_idx = pd.to_datetime(hist.index, utc=True, errors='coerce')
                    hist = hist.loc[hist_idx >= created.floor('5min')]
                    if not hist.empty:
                        if rec['side'] == 'LONG':
                            adverse = (entry0 - hist['Low'].astype(float)) / risk0
                        else:
                            adverse = (hist['High'].astype(float) - entry0) / risk0
                        if not adverse.empty:
                            rec['max_adverse_r'] = float(max(0.0, adverse.max()))
                changed = True

            last_checked = rec.get('last_checked_bar')
            last_ts = pd.Timestamp(last_checked) if last_checked else None
            if last_ts is not None:
                if last_ts.tzinfo is None:
                    last_ts = last_ts.tz_localize('UTC')
                else:
                    last_ts = last_ts.tz_convert('UTC')

            for idx, bar in completed.iterrows():
                ts = pd.Timestamp(idx)
                if ts.tzinfo is None:
                    ts = ts.tz_localize('UTC')
                else:
                    ts = ts.tz_convert('UTC')
                # Include the bar that was forming when the signal fired, then only new bars.
                if ts < created.floor('5min'):
                    continue
                if last_ts is not None and ts <= last_ts:
                    continue

                hit_stop, hit_tp1, hit_tp2 = _bar_hits(rec, bar)
                ts_iso = ts.isoformat()
                status = rec.get('status', 'OPEN')

                # Track both favorable and adverse excursion in R for exit research.
                entry = float(rec['entry']); stop = float(rec['stop'])
                risk_unit = abs(entry - stop)
                if risk_unit > 0 and status in {'OPEN', 'TP1_OPEN'}:
                    if rec['side'] == 'LONG':
                        favorable_r = (float(bar['High']) - entry) / risk_unit
                        adverse_r = (entry - float(bar['Low'])) / risk_unit
                    else:
                        favorable_r = (entry - float(bar['Low'])) / risk_unit
                        adverse_r = (float(bar['High']) - entry) / risk_unit
                    old_mfe = float(rec.get('max_favorable_r', 0.0) or 0.0)
                    old_mae = float(rec.get('max_adverse_r', 0.0) or 0.0)
                    if favorable_r > old_mfe:
                        rec['max_favorable_r'] = max(0.0, float(favorable_r)); changed = True
                    if adverse_r > old_mae:
                        rec['max_adverse_r'] = max(0.0, float(adverse_r)); changed = True
                    reentry_arm_r = float(cfg.get('scanner', {}).get('reentry_arm_r', 0.80 * float(cfg['risk'].get('tp1_r_multiple', 1.5))))
                    if status == 'OPEN' and rec.get('max_favorable_r', 0.0) >= reentry_arm_r and not rec.get('reentry_armed_at'):
                        rec['reentry_armed_at'] = ts_iso; changed = True

                if status == 'OPEN':
                    if hit_stop and hit_tp1:
                        rec['status'] = 'AMBIGUOUS'
                        rec['closed_at'] = ts_iso
                        rec['result_r'] = 0.0
                        rec['last_checked_bar'] = ts_iso
                        changed = True
                        _notify_tracker_result('⚠️ SCANNER RESULT: AMBIGUOUS', rec, 'Stop and TP1 touched inside the same 5m candle; order cannot be known, so it is excluded from W/L stats.', data)
                        break
                    if hit_stop:
                        rec['status'] = 'STOPPED'
                        rec['closed_at'] = ts_iso
                        rec['result_r'] = -1.0
                        rec['last_checked_bar'] = ts_iso
                        changed = True
                        _notify_tracker_result('🔴 SCANNER RESULT: STOP HIT', rec, '❌ Failed setup — recorded -1.0R', data)
                        break
                    if hit_tp2:
                        rec['status'] = 'TP2_HIT'
                        rec['tp1_hit_at'] = ts_iso
                        rec['closed_at'] = ts_iso
                        rec['result_r'] = float(cfg['risk'].get('tp2_r_multiple', 2.5))
                        rec['last_checked_bar'] = ts_iso
                        changed = True
                        _notify_tracker_result('🔥 SCANNER RESULT: TP2 HIT', rec, f'Full target reached — recorded +{rec["result_r"]:.1f}R', data)
                        break
                    if hit_tp1:
                        rec['status'] = 'TP1_OPEN'
                        rec['tp1_hit_at'] = ts_iso
                        rec['result_r'] = float(cfg['risk'].get('tp1_r_multiple', 1.5))
                        changed = True
                        _notify_tracker_result('✅ SCANNER RESULT: TP1 HIT', rec, f'Successful setup — baseline +{rec["result_r"]:.1f}R; still watching for TP2', data)

                elif status == 'TP1_OPEN':
                    # Primary win is already established. If stop and TP2 touch in the
                    # same later bar, conservatively keep only the TP1 result.
                    if hit_tp2 and not hit_stop:
                        rec['status'] = 'TP2_HIT'
                        rec['closed_at'] = ts_iso
                        rec['result_r'] = float(cfg['risk'].get('tp2_r_multiple', 2.5))
                        rec['last_checked_bar'] = ts_iso
                        changed = True
                        _notify_tracker_result('🔥 SCANNER RESULT: TP2 HIT', rec, f'Full target reached — upgraded to +{rec["result_r"]:.1f}R', data)
                        break
                    if hit_stop:
                        rec['status'] = 'TP1_ONLY'
                        rec['closed_at'] = ts_iso
                        rec['result_r'] = float(cfg['risk'].get('tp1_r_multiple', 1.5))
                        rec['last_checked_bar'] = ts_iso
                        changed = True
                        _notify_tracker_result('✅ SCANNER RESULT: TP1 ONLY', rec, f'TP1 was reached before reversal; TP2 missed — recorded +{rec["result_r"]:.1f}R', data)
                        break

                # If the original setup ran to roughly 80% of TP1 (configurable), then
                # returned to the entry zone on a later candle, require a fresh
                # bullish/bearish confirmation before suggesting a re-entry.
                if rec.get('status') == 'OPEN' and idx in metrics_df.index:
                    metrics = metrics_df.loc[idx]
                    if _maybe_notify_reentry(rec, bar, metrics, cfg, ts_iso):
                        changed = True

                rec['last_checked_bar'] = ts_iso
                changed = True
        except Exception as e:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] tracker {rec.get("id", "?")}: update error {e}')

    if changed:
        save_tracker(data)


def expire_old_tracked_signals(cfg):
    hours = float(cfg.get('scanner', {}).get('tracking_expiry_hours', 6.0))
    now = datetime.now(timezone.utc)
    data = load_tracker()
    changed = False
    for rec in data.get('signals', []):
        if rec.get('status') not in {'OPEN', 'TP1_OPEN'}:
            continue
        try:
            created = pd.Timestamp(rec['created_at'])
            if created.tzinfo is None:
                created = created.tz_localize('UTC')
            else:
                created = created.tz_convert('UTC')
            age_h = (pd.Timestamp(now) - created).total_seconds() / 3600.0
            if age_h < hours:
                continue
            if rec['status'] == 'TP1_OPEN':
                rec['status'] = 'TP1_ONLY'
                rec['result_r'] = float(cfg['risk'].get('tp1_r_multiple', 1.5))
                detail = f'TP1 was reached; TP2 not reached before {hours:g}h expiry — recorded +{rec["result_r"]:.1f}R'
                title = '✅ SCANNER RESULT: TP1 ONLY'
            else:
                rec['status'] = 'EXPIRED'
                rec['result_r'] = 0.0
                detail = f'Neither TP1 nor stop reached within {hours:g}h — expired, 0.0R'
                title = '⏳ SCANNER RESULT: EXPIRED'
            rec['closed_at'] = now.isoformat()
            changed = True
            _notify_tracker_result(title, rec, detail, data)
        except Exception as e:
            print(f'[{datetime.now().strftime("%H:%M:%S")}] tracker expiry error: {e}')
    if changed:
        save_tracker(data)


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
                df = td_data.get(symbol)
            else:
                df = fetch_item(item, cfg)
            if df is None:
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: insufficient/unavailable data')
                continue
            if not data_is_fresh(df, item, cfg):
                age = candle_age_minutes(df)
                age_txt = f'{age:.1f}m old' if age is not None else 'unknown age'
                print(f'[{datetime.now().strftime("%H:%M:%S")}] {symbol}: market/data stale ({age_txt}); skip')
                continue

            # Track outcomes of previously-issued signals using the same completed
            # 5-minute data already fetched for this symbol (no extra API credits).
            update_tracked_signals_for_symbol(df, item, cfg)

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
                register_tracked_signal(sig, item)
                maybe_execute_ig_demo(sig, source='5m')
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
    ig_status = ig_demo_connection_test()
    global IG_EXECUTOR
    IG_EXECUTOR = IGDemoExecutor(cfg)
    if ig_status.get('ok'):
        IG_EXECUTOR.ensure_login()
    print('Market Scanner started.')
    print('Decision Engine v2: ENABLED')
    print('Aggressive v2 demo profile: ENABLED')
    print(f"Watchlist: {len(cfg['watchlist'])} instruments | min score {cfg['scanner']['minimum_score']} | core interval 5m")
    print('1-minute entry watcher: ENABLED for near-qualified 5m setups')
    print('Auto performance tracker: ENABLED (TP1 / TP2 / stop / expiry)')
    print(f'Tracker storage: {tracker_path()}')
    print(f"Twelve Data FX/metals: {'READY' if td_key else 'MISSING API KEY'}")
    print('IG demo:', ig_status.get('message'))
    print(f"IG demo auto execution: {'ON' if IG_EXECUTOR.auto else 'OFF'} | allowed types: {','.join(sorted(IG_EXECUTOR.allowed_types))} | max open {IG_EXECUTOR.max_open}")
    print('Press Ctrl+C to stop.\n')

    if cfg.get('notifications', {}).get('telegram', True):
        account = float(cfg.get('risk', {}).get('account_cash_gbp', 1000))
        risk_pct = float(cfg.get('risk', {}).get('risk_per_trade_pct', 1.0))
        risk_gbp = account * risk_pct / 100.0
        td_line = '✅ FX/metals live feed ready' if td_key else '⚠️ FX/metals disabled until TWELVE_DATA_API_KEY is added'
        persistent_tracker = bool(os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '').strip() or os.getenv('SCANNER_DATA_DIR', '').strip())
        storage_line = '💾 Performance history: persistent' if persistent_tracker else '⚠️ Performance history resets on redeploy until a Railway volume is mounted'
        startup_message = (
            '✅ Market Scanner Online\n'
            '🧠 Decision Engine v2: ENABLED\n'
            '🔄 Core strategy: confirmed 5-minute setups\n'
            '👀 1-minute entry watcher: ACTIVE when a setup is close\n'
            '🤖 Auto result tracking: ON (TP1 / TP2 / stop / expiry)\n'
            '✅ Entry ranges: ON | ♻️ Re-entry confirmation: ON\n'
            f'{tracker_summary_line()}\n'
            f'{storage_line}\n'
            f'📊 Watchlist: {len(cfg["watchlist"])} instruments\n'
            f'⭐ Minimum score: {cfg["scanner"]["minimum_score"]}/8\n'
            f'💷 Account basis: £{account:,.2f}\n'
            f'🛑 Planned risk/trade: ~£{risk_gbp:,.2f}\n'
            f'{td_line}\n\n'
            f'{format_ig_demo_status(ig_status)}\n'
            f'🤖 IG demo auto execution: {"ON" if IG_EXECUTOR.auto else "OFF"}\n'
            f'🧪 Auto markets: {", ".join(sorted(IG_EXECUTOR.allowed_types))} | Max open: {IG_EXECUTOR.max_open}\n'
            f'🛑 Demo daily caps: {IG_EXECUTOR.max_trades_day} trades / {IG_EXECUTOR.max_stops_day} stop-outs\n'
            f'🎯 Phase-1 IG exit: full position closes at TP1\n'
            f'{IG_EXECUTOR.stats_line()}'
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
            expire_old_tracked_signals(cfg)
            if IG_EXECUTOR is not None:
                IG_EXECUTOR.monitor()

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
