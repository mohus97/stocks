"""Durable, alert-only lifecycle, independent risk monitoring and delivery.

The ledger measures reference-price scenarios, never real brokerage fills.
SQLite transactions couple each state change to a retryable Telegram outbox.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pandas as pd
import requests

from market_data import clean_frame, closed_bars, continuous_tail, fresh_frame
from news_guard import NewsGuard, digest, parse_time

LOG = logging.getLogger(__name__)
UTC = timezone.utc
ACTIVE = ('PENDING', 'DELIVERY_UNKNOWN', 'OPEN', 'TP1_OPEN')


class DeliveryError(Exception):
    def __init__(self, retry_after=5, uncertain=False):
        self.retry_after = min(3600, max(1, retry_after))
        self.uncertain = uncertain


def send_telegram(text):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        raise DeliveryError(60)
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          json={'chat_id': chat, 'text': text[:4000],
                                'link_preview_options': {'is_disabled': True}}, timeout=(3, 8))
        data = r.json()
    except (requests.RequestException, ValueError):
        # Never log request URLs or exception text: the URL includes the token.
        raise DeliveryError(5, uncertain=True) from None
    if not r.ok or not data.get('ok') or not (data.get('result') or {}).get('message_id'):
        delay = (data.get('parameters') or {}).get('retry_after', 10)
        raise DeliveryError(delay)
    return int(data['result']['message_id'])


def signal_key(sig):
    ctx = sig.context or {}
    return digest('reliable_v1', sig.symbol, sig.side, ctx.get('setup_bar'),
                  ctx.get('trigger_bar', ''), ctx.get('trigger_mode', '5m'))


def prepare_signal(sig, cfg):
    """Set ONE exit plan used by both notification and forward tracking."""
    from spread_runtime import _spread_profile
    from scanner import _r_target_price
    ctx = dict(sig.context or {})
    tier = ctx.get('quality_tier')
    settings = cfg.get('reliability', {})
    if sig.market_type == 'index':
        return None, 'cash-index/broker CFD price basis is unverified'
    if tier != 'A-TIER':
        if not settings.get('allow_fast', False):
            return None, 'only A-tier signals enabled'
        from fast_runtime import _fast_candidate
        if not _fast_candidate(sig)[0]:
            return None, 'FAST quality gate failed'
    values = [sig.price, sig.stop, sig.entry_low, sig.entry_high, sig.risk_gbp]
    if not all(math.isfinite(float(v)) and v > 0 for v in values):
        return None, 'invalid price or risk'
    if sig.side not in {'LONG', 'SHORT'} or not sig.entry_low <= sig.price <= sig.entry_high:
        return None, 'invalid entry geometry'
    if (sig.side == 'LONG' and sig.stop >= sig.entry_low) or (sig.side == 'SHORT' and sig.stop <= sig.entry_high):
        return None, 'stop crosses the entry range'
    profile = _spread_profile(sig)
    spread_r = profile.get('spread_r')
    maximum = settings.get('max_spread_r', 0.20) if tier == 'A-TIER' else 0.15
    if spread_r is None or not math.isfinite(spread_r) or spread_r < 0 or spread_r > maximum:
        return None, 'spread cost unavailable or too high'
    tp1_r, tp2_r = (0.75, 1.50) if tier == 'A-TIER' else (0.50, 1.00)
    sig.tp1 = _r_target_price(sig.price, sig.stop, sig.side, tp1_r)
    sig.tp2 = _r_target_price(sig.price, sig.stop, sig.side, tp2_r)
    budget = cfg['risk']['account_cash_gbp'] * cfg['risk']['risk_per_trade_pct'] / 100
    worst_entry = sig.entry_high if sig.side == 'LONG' else sig.entry_low
    worst_distance = abs(worst_entry - sig.stop) + profile['spread']
    notional_cap = cfg['risk']['account_cash_gbp'] * cfg['risk']['max_cash_exposure_pct'] / 100
    sig.suggested_exposure_gbp = min(notional_cap, budget * sig.price / worst_distance)
    sig.risk_gbp = sig.suggested_exposure_gbp * worst_distance / sig.price
    ctx.update({'exit_plan': '70_30_original_stop', 'bank_fraction': 0.70,
                'tp1_r': tp1_r, 'tp2_r': tp2_r, 'spread_r': spread_r,
                'spread_source': profile['source'], 'spread_price_units': profile['spread'],
                'risk_budget_gbp': budget, 'sizing_is_estimate': True})
    sig.context = ctx
    return sig, None


def format_trade(rec):
    from scanner import decimals_for_price
    d = decimals_for_price(rec['price'])
    ctx = rec['context']
    return (
        f"🚨 {rec['label']} · {rec['side']} · {ctx['quality_tier']}\n"
        f"ID: {rec['id']}\n"
        f"New-entry deadline: {(parse_time(rec['created_at']) + timedelta(minutes=2)):%H:%M:%S} UTC\n"
        f"Entry: {rec['entry_low']:.{d}f}–{rec['entry_high']:.{d}f}\n"
        f"TP1 (70%): {rec['tp1']:.{d}f} · TP2 (30%): {rec['tp2']:.{d}f}\n"
        f"Stop for remaining position: {rec['stop']:.{d}f}\n"
        f"Estimated risk: £{rec['risk_gbp']:.2f}; notional cap £{rec['suggested_exposure_gbp']:.2f}\n"
        f"Spread estimate: {ctx['spread_r']:.2f}R — verify broker bid/ask and size.\n"
        "News/calendar checked. Enter only inside the range.\n"
        "Monitoring alerts follow this ID; no orders are placed or closed.\n"
        f"Record your action: /entered {rec['id']} [fill price], /skipped {rec['id']} or /closed {rec['id']}"
    )


def evaluate_bar(rec, bar, at):
    """Conservative hypothetical 70/30 exits; original stop stays in place."""
    long = rec['side'] == 'LONG'
    stop = float(bar.Low) <= rec['stop'] if long else float(bar.High) >= rec['stop']
    tp1 = float(bar.High) >= rec['tp1'] if long else float(bar.Low) <= rec['tp1']
    tp2 = float(bar.High) >= rec['tp2'] if long else float(bar.Low) <= rec['tp2']
    risk = abs(rec['price'] - rec['stop'])
    r1 = abs(rec['tp1'] - rec['price']) / risk
    r2 = abs(rec['tp2'] - rec['price']) / risk
    banked = bool(rec.get('tp1_at'))
    if stop and (tp2 if banked else tp1):
        rec.update(status='AMBIGUOUS', result_r=None)
        return 'Ambiguous candle: stop and target both touched; no win/loss assigned.'
    if stop:
        # Price gaps can make the loss worse than the nominal stop distance.
        gap_r = (float(bar.Open) - rec['price']) / risk * (1 if long else -1)
        stop_r = min(-1.0, gap_r)
        result = 0.7 * r1 + 0.3 * stop_r if banked else stop_r
        rec.update(status='STOPPED', result_r=result)
        return 'Stop touched on the data feed. Setup withdrawn; check the broker position now.'
    if tp2:
        rec.update(status='TP2_HIT', tp1_at=rec.get('tp1_at') or at,
                   result_r=0.7 * r1 + 0.3 * r2)
        return 'Both displayed targets touched on the data feed (hypothetical 70/30 exit).'
    if tp1 and not banked:
        rec.update(status='TP1_OPEN', tp1_at=at)
        return 'TP1 touched on the data feed; 30% runner still monitored at the original stop.'
    return None


class ReliableScanner:
    def __init__(self, cfg, folder, notify=send_telegram, clock=None, news=None):
        self.cfg = cfg
        self.settings = cfg.get('reliability', {})
        self.clock = clock or (lambda: datetime.now(UTC))
        self.notify = notify
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.folder / 'reliable_signals.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS active_signals ON signals(status, symbol);
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, signal_id TEXT,
                kind TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS credits (at REAL NOT NULL, count INTEGER NOT NULL, purpose TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS credit_time ON credits(at);
            CREATE TABLE IF NOT EXISTS positions (signal_id TEXT PRIMARY KEY,
                status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.commit()
        self.frames = {}
        self.news = news or NewsGuard(cfg.get('news', {}), self.folder / 'event_cache.json', clock=self.clock)
        self.stop_event = threading.Event()
        self.threads = []
        self.health = {'core': self.clock().isoformat()}
        self.command_client = None

    def watched_records(self):
        """Alert lifecycle and user-confirmed position lifecycle are independent."""
        with self.lock:
            rows = self.db.execute("""SELECT s.body FROM signals s LEFT JOIN positions p
                ON s.id=p.signal_id WHERE s.status IN ('PENDING','DELIVERY_UNKNOWN','OPEN','TP1_OPEN')
                OR p.status='ENTERED'""").fetchall()
            return [json.loads(r['body']) for r in rows]

    def open_positions(self):
        with self.lock:
            return {r['signal_id']: json.loads(r['body']) for r in
                    self.db.execute("SELECT * FROM positions WHERE status='ENTERED'").fetchall()}

    def records(self, active_only=True):
        with self.lock:
            rows = self.db.execute("SELECT body FROM signals WHERE status IN ('PENDING','DELIVERY_UNKNOWN','OPEN','TP1_OPEN')" if active_only else 'SELECT body FROM signals').fetchall()
            return [json.loads(r['body']) for r in rows]

    def _save(self, rec):
        self.db.execute('INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?)',
                        (rec['id'], rec['symbol'], rec['status'], parse_time(rec['created_at']).timestamp(), json.dumps(rec)))

    def _queue(self, key, message, signal_id=None, kind='UPDATE'):
        self.db.execute('INSERT OR IGNORE INTO outbox(id,signal_id,kind,message,due) VALUES (?,?,?,?,?)',
                        (key, signal_id, kind, message, self.clock().timestamp()))

    def system_notice(self, key, message):
        with self.lock, self.db:
            self._queue(key, message)

    def cached_frame(self, symbol):
        with self.lock:
            frame = self.frames.get(symbol)
            return frame.copy() if frame is not None else None

    def reserve_td(self, count, purpose):
        now = self.clock()
        # Twelve Data's daily credit reset is UTC, not the user's local date.
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        minute = math.floor(now.timestamp() / 60) * 60
        with self.lock, self.db:
            used = self.db.execute('SELECT COALESCE(SUM(count),0) FROM credits WHERE at>=?', (day,)).fetchone()[0]
            used_min = self.db.execute('SELECT COALESCE(SUM(count),0) FROM credits WHERE at>=?', (minute,)).fetchone()[0]
            limit = self.settings.get('td_daily_credits', 800)
            minute_limit = self.settings.get('td_minute_credits', 8)
            reserve = 0
            # Active risk checks take precedence over looking for new entries.
            active = {r['symbol'] for r in self.watched_records()
                      if r.get('delivered_at') and r['item'].get('provider') == 'twelvedata'}
            minute_reserve = 0
            if purpose not in {'monitor', 'position'}:
                reserve = len(active) * self.settings.get('monitor_reserve_minutes', 30)
                # Once risk workers have used their reserved share this minute,
                # the remainder is available to setup discovery.
                spent = self.db.execute("SELECT COALESCE(SUM(count),0) FROM credits WHERE at>=? AND purpose IN ('monitor','position')", (minute,)).fetchone()[0]
                minute_reserve = max(0, len(active) - spent)
            if used + count + reserve > limit or used_min + count + minute_reserve > minute_limit:
                return False
            self.db.execute('INSERT INTO credits VALUES (?,?,?)', (now.timestamp(), count, purpose))
            self.db.execute('DELETE FROM credits WHERE at < ?', (day - 86400,))
            return True

    def publish(self, sig, item):
        if not self.cfg.get('notifications', {}).get('telegram', True):
            return False
        if self.threads and not self.workers_ready():
            LOG.warning('Entry blocked: a monitoring/delivery worker is not healthy')
            return False
        gate = self.news.gate(item, self.clock())
        if gate:
            LOG.info('%s entry blocked: %s', sig.symbol, gate)
            return False
        sig, reason = prepare_signal(sig, self.cfg)
        if sig is None:
            LOG.info('Entry blocked: %s', reason)
            return False
        if not sig.context.get('setup_bar') or not sig.context.get('trigger_level'):
            return False
        rec = asdict(sig)
        rec.update(id=signal_key(sig), item=dict(item), status='PENDING',
                   created_at=self.clock().isoformat(), delivered_at=None, result_r=None,
                   last_bar=None, tp1_at=None, monitoring_degraded=False)
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM signals WHERE id=?', (rec['id'],)).fetchone():
                return False
            active = self.watched_records()
            if any(r['symbol'] == sig.symbol for r in active):
                return False
            if len(active) >= self.settings.get('max_active_signals', 3):
                return False
            # US stocks are a correlated bucket, not independent opportunities.
            if sum(r['market_type'] == sig.market_type for r in active) >= self.settings.get('max_active_per_type', 2):
                return False
            total_risk = sum(r['risk_gbp'] for r in active) + sig.risk_gbp
            cap = self.cfg['risk']['account_cash_gbp'] * self.settings.get('max_total_risk_pct', 2) / 100
            if total_risk > cap + 1e-8:
                return False
            last = self.db.execute("SELECT created FROM signals WHERE symbol=? AND status NOT IN ('UNSENT','PENDING') ORDER BY created DESC LIMIT 1", (sig.symbol,)).fetchone()
            if last and self.clock().timestamp() - last['created'] < self.cfg['scanner']['cooldown_minutes'] * 60:
                return False
            self._save(rec)
            self._queue('entry:' + rec['id'], format_trade(rec), rec['id'], 'ENTRY')
        # The delivery worker owns confirmation and tracking. The legacy caller
        # must not register a shadow trade or execute an IG order on enqueue.
        return False

    def _update_message(self, rec, reason):
        return (f"⚠️ {rec['label']} · {rec['side']} · {rec['status']}\nID: {rec['id']}\n"
                f"Observed: {self.clock():%Y-%m-%d %H:%M:%S} UTC\n"
                f"{reason}\nNo broker order has been changed. Actual fills/P&L may differ.")

    def _transition(self, rec, status, reason, event_id=None):
        previous = rec['status']
        rec.update(status=status, reason=reason, closed_at=self.clock().isoformat())
        if status not in {'STOPPED', 'TP2_HIT'} or rec.get('monitor_gap') or not rec.get('message_id'):
            rec['result_r'] = None
        rec['result_r_net_estimate'] = (rec['result_r'] - rec['context'].get('spread_r', 0)
                                        if rec['result_r'] is not None else None)
        self._save(rec)
        self.db.execute("UPDATE outbox SET status='CANCELLED' WHERE signal_id=? AND kind='ENTRY' AND status='PENDING'", (rec['id'],))
        if rec.get('delivered_at') or previous == 'DELIVERY_UNKNOWN':
            self._queue(f"update:{rec['id']}:{event_id or status}", self._update_message(rec, reason), rec['id'])

    def observe_5m(self, frame, item):
        from scanner import _decision_snapshot
        d = clean_frame(frame)
        if d.empty:
            return
        with self.lock:
            self.frames[item['symbol']] = d.copy()
        if not fresh_frame(d, self.cfg['scanner'].get('max_candle_age_minutes', 7), self.clock()):
            return
        snap = _decision_snapshot(d, item, self.cfg)
        if not snap:
            return
        with self.lock, self.db:
            for rec in self.records():
                if rec['symbol'] != item['symbol'] or not rec.get('delivered_at'):
                    continue
                if pd.Timestamp(snap['row'].name) < pd.Timestamp(rec['delivered_at']).ceil('5min'):
                    continue
                side = snap['results'][rec['side']]
                opposite = snap['results']['SHORT' if rec['side'] == 'LONG' else 'LONG']
                # Falling score alone is not proof of a mistake. Require a hard
                # veto or a materially stronger confirmed opposing direction.
                if side['veto'] or (not opposite['veto'] and opposite['score'] >= 6.5 and opposite['score'] >= side['score'] + 1):
                    reasons = ', '.join(side.get('veto_reasons', [])) or 'confirmed opposing 5m thesis'
                    self._transition(rec, 'INVALIDATED', 'Setup invalidated: ' + reasons + '. Do not use the old entry; review any open position.')

    def review_news(self):
        now = self.clock()
        missing = self.news.unavailable(now)
        degraded = bool(missing)
        was_degraded = self.health.get('news_degraded')
        if was_degraded != degraded:
            self.health['news_degraded'] = degraded
            if degraded:
                self.system_notice('news-down:' + now.isoformat(), '⚠️ News/calendar coverage unavailable: ' + ', '.join(missing) + '. New trade alerts paused; price monitoring continues.')
            elif was_degraded:
                self.system_notice('news-up:' + now.isoformat(), '✅ News/calendar coverage restored. Qualified entries may resume outside event blackout windows.')
        degraded_sources = tuple(self.news.degraded_sources(now))
        previous_sources = self.health.get('degraded_sources', ())
        if degraded_sources != previous_sources:
            self.health['degraded_sources'] = degraded_sources
            self.system_notice('sources:' + now.isoformat(),
                '⚠️ Reduced event coverage: ' + ', '.join(degraded_sources) + '. Required coverage rules still apply.'
                if degraded_sources else '✅ All configured news sources are reachable again.')
        with self.lock, self.db:
            for rec in self.watched_records():
                risks = self.news.risks(rec['item'], now)
                if risks:
                    event = risks[0]
                    reason = (f"Event risk: {event.title}\n{event.source}: {event.url}\n"
                              "Previous entry withdrawn. Review open exposure; wait for a new confirmed setup.")
                    if rec['status'] in ACTIVE:
                        self._transition(rec, 'NEWS_WITHDRAWN', reason, event.id)
                    else:
                        self._queue(f"position-news:{rec['id']}:{event.id}", self._update_message(rec,
                            reason + '\nYour marked-open position remains monitored until /closed.'), rec['id'])

    def fetch_monitor_frame(self, rec, purpose='monitor'):
        from scanner import fetch_twelvedata_1m, fetch_yahoo_1m
        if rec['item'].get('provider') == 'twelvedata':
            config = dict(self.cfg, _td_purpose=purpose)
            return fetch_twelvedata_1m(SimpleNamespace(data_symbol=rec['item'].get('data_symbol', rec['symbol'])), config)
        return fetch_yahoo_1m(rec['item'].get('data_symbol', rec['symbol']))

    def _pending_is_valid(self, rec):
        if self.clock().timestamp() - parse_time(rec['created_at']).timestamp() > self.settings.get('entry_delivery_ttl_seconds', 45):
            return False
        if self.news.gate(rec['item'], self.clock()):
            return False
        if self.threads and not self.workers_ready():
            return False
        frame = self.fetch_monitor_frame(rec, purpose='entry')
        if not fresh_frame(frame, 2, self.clock()):
            return False
        price = float(frame.iloc[-1].Close)
        trigger = rec['context']['trigger_level']
        return rec['entry_low'] <= price <= rec['entry_high'] and (price > trigger if rec['side'] == 'LONG' else price < trigger)

    def deliver_once(self):
        with self.lock:
            pending = self.db.execute("SELECT * FROM outbox WHERE status='PENDING' AND due<=? ORDER BY CASE kind WHEN 'ENTRY' THEN 1 ELSE 0 END,due LIMIT 20", (self.clock().timestamp(),)).fetchall()
        for row in pending:
            if row['kind'] == 'ENTRY':
                with self.lock:
                    stored = self.db.execute('SELECT body FROM signals WHERE id=?', (row['signal_id'],)).fetchone()
                    rec = json.loads(stored['body'])
                if rec['status'] not in {'PENDING', 'DELIVERY_UNKNOWN'}:
                    with self.lock, self.db:
                        self.db.execute("UPDATE outbox SET status='CANCELLED' WHERE id=?", (row['id'],))
                    continue
                if not self._pending_is_valid(rec):
                    with self.lock, self.db:
                        current = json.loads(self.db.execute('SELECT body FROM signals WHERE id=?', (rec['id'],)).fetchone()['body'])
                        if current['status'] in {'PENDING', 'DELIVERY_UNKNOWN'}:
                            self._transition(current, 'UNSENT' if current['status'] == 'PENDING' else 'WITHDRAWN', 'Entry expired or failed its final news/price check. Do not act on an earlier copy of this alert.')
                    continue
            # Persist the send attempt before network I/O. A crash after Telegram
            # accepts a message must not leave an unmonitored possible alert.
            with self.lock, self.db:
                latest = self.db.execute('SELECT status FROM outbox WHERE id=?', (row['id'],)).fetchone()
                if latest['status'] != 'PENDING':
                    continue
                if row['kind'] == 'ENTRY':
                    rec = json.loads(self.db.execute('SELECT body FROM signals WHERE id=?', (row['signal_id'],)).fetchone()['body'])
                    rec['status'] = 'DELIVERY_UNKNOWN'
                    rec['delivered_at'] = rec.get('delivered_at') or self.clock().isoformat()
                    self._save(rec)
            try:
                message_id = self.notify(row['message'])
                if not message_id:
                    raise DeliveryError()
            except Exception as exc:
                delay = max(getattr(exc, 'retry_after', 5), min(60, 2 ** min(row['attempts'] + 1, 6)))
                LOG.warning('Telegram delivery pending for %s (%s)', row['id'], type(exc).__name__)
                with self.lock, self.db:
                    self.db.execute('UPDATE outbox SET attempts=attempts+1,due=? WHERE id=?', (self.clock().timestamp() + delay, row['id']))
                continue
            with self.lock, self.db:
                self.db.execute("UPDATE outbox SET status='SENT',message_id=? WHERE id=?", (int(message_id), row['id']))
                if row['kind'] == 'ENTRY':
                    rec = json.loads(self.db.execute('SELECT body FROM signals WHERE id=?', (row['signal_id'],)).fetchone()['body'])
                    rec['message_id'] = int(message_id)
                    if rec['status'] == 'DELIVERY_UNKNOWN':
                        rec['status'] = 'OPEN'
                    self._save(rec)  # Never resurrect a concurrently withdrawn setup.
            LOG.info('Telegram accepted notification %s (message_id=%s)', row['id'], int(message_id))

    def process_prices(self, ident, frame):
        now = self.clock()
        with self.lock, self.db:
            row = self.db.execute('SELECT body FROM signals WHERE id=?', (ident,)).fetchone()
            if not row:
                return
            rec = json.loads(row['body'])
            if rec['status'] not in ACTIVE or not rec.get('delivered_at'):
                return
            if not fresh_frame(frame, 2, now):
                if not rec['monitoring_degraded']:
                    rec['monitoring_degraded'] = True
                    self._queue('degraded:' + ident + ':' + now.isoformat(), self._update_message(rec, 'Price monitoring degraded (stale/missing data or API quota). Immediate invalidation checks unavailable; monitor the broker yourself.'), ident)
                    self._save(rec)
                return
            if rec['monitoring_degraded']:
                rec['monitoring_degraded'] = False
                self._queue('recovered:' + ident + ':' + now.isoformat(), self._update_message(rec, 'Fresh price monitoring restored.'), ident)
            bars = closed_bars(frame, 1, now)
            start = pd.Timestamp(rec['delivered_at']).ceil('min')
            last = pd.Timestamp(rec['last_bar']) if rec.get('last_bar') else None
            new = bars.loc[bars.index >= start]
            if last is not None:
                new = new.loc[new.index > last]
            for idx, bar in new.iterrows():
                expected = last + pd.Timedelta(minutes=1) if last is not None else start
                if idx > expected:
                    rec['monitor_gap'] = True
                reason = evaluate_bar(rec, bar, idx.isoformat())
                rec['last_bar'] = idx.isoformat()
                last = idx
                if reason:
                    if rec.get('monitor_gap'):
                        rec['result_r'] = None
                        reason += ' Earlier data was missing; outcome excluded from performance statistics.'
                    if rec['status'] == 'TP1_OPEN':
                        self._queue('tp1:' + ident, self._update_message(rec, reason), ident)
                    else:
                        self._transition(rec, rec['status'], reason)
                        return
            # A current observed stop breach warrants an immediate warning even
            # before candle close, but must not fabricate a precise fill/P&L.
            latest = clean_frame(frame).iloc[-1]
            # Do not reuse OHLC extremes from before delivery for P&L. A fresh
            # available CLOSE in the entry minute can nevertheless warrant a
            # risk warning; candle timestamps are opens, not quote timestamps.
            if latest.name >= pd.Timestamp(rec['delivered_at']).floor('min'):
                price = float(latest.Close)
                breached = price <= rec['stop'] if rec['side'] == 'LONG' else price >= rec['stop']
                if breached:
                    self._transition(rec, 'INVALIDATED', 'Latest available price is beyond the stop. Do not use this setup; check the broker immediately. The exact crossing time and fill are unverified.')
                    return
            tail = bars.loc[bars.index >= start].tail(2)
            trigger = rec['context']['trigger_level']
            buffer = float(rec['context'].get('atr_value') or 0) * 0.1
            if len(tail) == 2 and continuous_tail(tail, 1, 2):
                failed = (tail.Close < trigger - buffer).all() if rec['side'] == 'LONG' else (tail.Close > trigger + buffer).all()
                if failed:
                    self._transition(rec, 'INVALIDATED', 'Breakout failed: two completed 1m candles lost the trigger level. Previous entry withdrawn; review any open position.')
                    return
            if len(bars.loc[bars.index >= start]) >= 2:
                last_bar = bars.iloc[-1]
                body = abs(float(last_bar.Close) - float(last_bar.Open))
                atr = float(rec['context'].get('atr_value') or 0)
                opposite = last_bar.Close < last_bar.Open if rec['side'] == 'LONG' else last_bar.Close > last_bar.Open
                if atr > 0 and opposite and body >= 0.8 * atr:
                    self._transition(rec, 'INVALIDATED', 'New opposing 1m price shock undermines the setup. Previous entry withdrawn; review any open position.')
                    return
            self._save(rec)

    def monitor_once(self):
        now = self.clock()
        positions = self.open_positions()
        active = [r for r in self.watched_records() if r.get('delivered_at')]
        active.sort(key=lambda r: (r['id'] not in positions, r.get('created_at', '')))
        # Process marked-open positions first, so candidates cannot race them
        # for the last credits. Limits keep this list small.
        with ThreadPoolExecutor(max_workers=3) as pool:
            for batch in ([r for r in active if r['id'] in positions], [r for r in active if r['id'] not in positions]):
                futures = [(r, pool.submit(self.fetch_monitor_frame, r)) for r in batch]
                for rec, future in futures:
                    try:
                        frame = future.result()
                        self.process_prices(rec['id'], frame)
                        self.process_position_prices(rec, frame)
                    except Exception as exc:
                        LOG.warning('Monitor %s failed (%s)', rec['symbol'], type(exc).__name__)
                        self.process_prices(rec['id'], None)
                        self.process_position_prices(rec, None)
        with self.lock, self.db:
            for rec in self.records():
                origin = rec.get('delivered_at') or rec['created_at']
                limit = self.settings.get('max_signal_minutes', 180) if rec.get('delivered_at') else 2
                if (now - parse_time(origin)).total_seconds() >= limit * 60:
                    self._transition(rec, 'EXPIRED', 'Signal time limit reached. Entry withdrawn; no flat-price or profitable exit is assumed.')

    def process_position_prices(self, rec, frame):
        """Never infer a broker close from a quote or signal expiry."""
        with self.lock, self.db:
            row = self.db.execute("SELECT body FROM positions WHERE signal_id=? AND status='ENTERED'", (rec['id'],)).fetchone()
            if not row:
                return
            pos = json.loads(row['body'])
            healthy = fresh_frame(frame, 2, self.clock())
            if not healthy:
                if not pos.get('degraded'):
                    self._queue('position-data:' + rec['id'] + ':' + self.clock().isoformat(),
                        self._update_message(rec, 'Your marked-open position has stale/missing prices or no API credits. Monitor it at the broker; tracking has NOT closed your position.'), rec['id'])
                pos['degraded'] = True
            else:
                if pos.get('degraded'):
                    self._queue('position-data-up:' + rec['id'] + ':' + self.clock().isoformat(),
                        self._update_message(rec, 'Fresh data restored for your marked-open position.'), rec['id'])
                pos['degraded'] = False
                pos['last_price_check'] = self.clock().isoformat()
                latest = clean_frame(frame).iloc[-1]
                if latest.name >= pd.Timestamp(pos['entered_at']).floor('min'):
                    price = float(latest.Close)
                    breached = price <= rec['stop'] if rec['side'] == 'LONG' else price >= rec['stop']
                    if breached and not pos.get('stop_warned'):
                        pos['stop_warned'] = True
                        self._queue('position-stop:' + rec['id'], self._update_message(rec,
                            'Your marked-open position is beyond the original stop on the available feed. Check the broker now. It stays marked open until you send /closed ' + rec['id']), rec['id'])
            self.db.execute('UPDATE positions SET body=? WHERE signal_id=?', (json.dumps(pos), rec['id']))

    def heartbeat_once(self):
        now = self.clock()
        with self.lock:
            oldest = self.db.execute("SELECT MIN(due) FROM outbox WHERE status='PENDING'").fetchone()[0]
            retries = self.db.execute("SELECT COALESCE(MAX(attempts),0) FROM outbox WHERE status='PENDING'").fetchone()[0]
        LOG.info('SCANNER_HEALTH %s', json.dumps({'version': 2, 'at': now.isoformat(),
            'workers_ready': self.workers_ready(), 'required_feeds_missing': self.news.unavailable(now),
            'degraded_sources': self.news.degraded_sources(now), 'marked_open': len(self.open_positions()),
            'max_pending_delivery_attempts': retries,
            'outbox_overdue_seconds': max(0, now.timestamp() - oldest) if oldest else 0}, sort_keys=True))

    def _loop(self, name, interval, action):
        while not self.stop_event.is_set():
            try:
                action()
                self.health[name] = self.clock().isoformat()
            except Exception as exc:
                LOG.error('%s worker failed (%s)', name, type(exc).__name__)
                self.system_notice('worker:' + name + ':' + self.clock().strftime('%Y-%m-%dT%H'), '⚠️ Scanner ' + name + ' worker failed. New entries paused until checks recover; monitor existing positions manually.')
            self.stop_event.wait(interval)

    def workers_ready(self):
        limits = {'news': 60, 'prices': self.settings.get('monitor_seconds', 60) + 90, 'delivery': 90, 'core': 420}
        if self.command_client is not None:
            limits['commands'] = 90
        return all(self.health.get(name) and
                   (self.clock() - parse_time(self.health[name])).total_seconds() < limit
                   for name, limit in limits.items())

    def start(self):
        import fcntl
        self.process_lock = open(self.folder / 'reliability.lock', 'a')
        fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Resume monitoring possible messages after a delivery-worker restart.
        self.news.refresh()
        self.review_news()
        self.system_notice('startup:' + self.clock().isoformat(),
            '✅ Scanner upgrade online: entry-minute stop warnings, persistent position tracking, prioritised risk checks.\n'
            'Checks: news every 120s; active prices about every 60s when feeds/quota allow. These are not guaranteed delivery times.\n'
            'Use /help for /entered, /skipped, /closed, /status and /report. Controls only update tracking; no broker orders are changed.')
        workers = [
                ('news', 10, lambda: (self.news.refresh(), self.review_news())),
                ('prices', self.settings.get('monitor_seconds', 60), self.monitor_once),
                ('delivery', 2, self.deliver_once)]
        if self.settings.get('telegram_controls', True):
            from scanner_controls import TelegramControls
            self.command_client = TelegramControls(self)
            workers.append(('commands', 5, self.command_client.poll))
        workers.append(('heartbeat', 60, self.heartbeat_once))
        for name, interval, action in workers:
            thread = threading.Thread(target=self._loop, args=(name, interval, action), name='scanner-' + name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=15)
        if hasattr(self, 'process_lock'):
            self.process_lock.close()
