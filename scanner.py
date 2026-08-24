#!/usr/bin/env python3
import os
import time
import json
import math
import hashlib
import re
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
    entry_low: float
    entry_high: float
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
        self.max_open = max(1, int(os.getenv('IG_MAX_OPEN_POSITIONS', '1')))
        self.max_trades_day = max(1, int(os.getenv('IG_MAX_DEMO_TRADES_PER_DAY', '4')))
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
        best, best_score = None, -9999
        for term in terms[:2]:
            r, data = self.request('GET', '/markets', '1', params={'searchTerm': term})
            if r is None or not r.ok or not isinstance(data, dict):
                continue
            for market in data.get('markets') or []:
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
                if score > best_score:
                    best, best_score = market, score
            if best and best_score >= 70:
                break
        return best

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
        deal_ccy_obj = next((c for c in currencies if bool(c.get('isDefault'))), None) or currencies[0]
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
            'stopDistance': round(stop_distance, precision),
            'limitDistance': round(limit_distance, precision),
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
            telegram_notify(f'❌ IG DEMO ORDER NOT ACCEPTED — {sig.label}\nReason: {reason}')
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

    entry_low, entry_high = make_entry_zone(price, stop, cfg)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    return Signal(
        symbol=item['symbol'], label=item.get('name', item['symbol']), market_type=market_type,
        side=side, price=price, stop=float(stop), tp1=float(tp1), tp2=float(tp2),
        entry_low=entry_low, entry_high=entry_high,
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
    entry_low, entry_high = make_entry_zone(entry_price, stop, cfg)
    return Signal(
        symbol=c.symbol,
        label=c.label,
        market_type=c.market_type,
        side=c.side,
        price=float(entry_price),
        stop=float(stop),
        tp1=float(tp1),
        tp2=float(tp2),
        entry_low=entry_low,
        entry_high=entry_high,
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
                item = next((i for i in cfg.get('watchlist', []) if i.get('symbol') == sig.symbol), None)
                register_tracked_signal(sig, item)
                maybe_execute_ig_demo(sig, source='1m')
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
    provider = (item or {}).get('provider', 'yahoo')
    data_symbol = (item or {}).get('data_symbol', sig.symbol)
    rec = {
        'id': tid,
        'symbol': sig.symbol,
        'label': sig.label,
        'market_type': sig.market_type,
        'provider': provider,
        'data_symbol': data_symbol,
        'side': sig.side,
        'entry': sig.price,
        'entry_low': sig.entry_low,
        'entry_high': sig.entry_high,
        'stop': sig.stop,
        'tp1': sig.tp1,
        'tp2': sig.tp2,
        'score': sig.score,
        'risk_gbp': sig.risk_gbp,
        'created_at': sig.timestamp,
        'status': 'OPEN',
        'last_checked_bar': None,
        'tp1_hit_at': None,
        'closed_at': None,
        'result_r': 0.0,
        'max_favorable_r': 0.0,
        'reentry_armed_at': None,
        'reentry_alerted_at': None,
    }
    data['signals'].append(rec)
    save_tracker(data)
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
    text = (
        f'{title} — {rec["label"]}\n'
        f'{rec["side"]} from {float(rec["entry"]):.{dec}f} | score {float(rec.get("score", 0)):.1f}/8\n'
        f'{detail}\n'
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

                # Track maximum favorable excursion in R. Once the original trade
                # has travelled far enough toward TP1, arm a possible second-chance
                # entry if price later retests the original entry zone and confirms.
                entry = float(rec['entry']); stop = float(rec['stop'])
                risk_unit = abs(entry - stop)
                if risk_unit > 0 and status == 'OPEN':
                    if rec['side'] == 'LONG':
                        favorable_r = (float(bar['High']) - entry) / risk_unit
                    else:
                        favorable_r = (entry - float(bar['Low'])) / risk_unit
                    old_mfe = float(rec.get('max_favorable_r', 0.0) or 0.0)
                    if favorable_r > old_mfe:
                        rec['max_favorable_r'] = max(0.0, float(favorable_r))
                        changed = True
                    reentry_arm_r = float(cfg.get('scanner', {}).get('reentry_arm_r', 0.80 * float(cfg['risk'].get('tp1_r_multiple', 1.5))))
                    if rec.get('max_favorable_r', 0.0) >= reentry_arm_r and not rec.get('reentry_armed_at'):
                        rec['reentry_armed_at'] = ts_iso
                        changed = True

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
