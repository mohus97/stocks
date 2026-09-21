"""Authenticated Telegram tracking controls; never brokerage commands.

Polling follows https://core.telegram.org/bots/api#getupdates. First activation
discards the historical queue; subsequent offsets and changes commit together.
"""
from collections import Counter, defaultdict
import json
import logging
import math
import os
import re

import requests

from news_guard import parse_time

LOG = logging.getLogger(__name__)
HELP = (
    'Scanner controls — tracking only, never broker orders.\n'
    '/entered ID [actual fill price] — mark a trade you already entered\n'
    '/skipped ID — mark an unused alert\n'
    '/closed ID — confirm you fully closed the position yourself\n'
    'You can also reply to an alert with /entered [price], /skipped or /closed.\n'
    '/status — health and marked-open positions\n'
    '/report — hypothetical forward results and cost sensitivity\n'
    'An invalidation does not close your trade. Marked-open trades remain monitored '
    'until /closed, subject to data availability. Existing broker positions are not imported.'
)


def performance_report(records, slippage_r=0.10):
    """Report eligibility, not a fabricated broker equity curve or win rate."""
    delivered = [r for r in records if r.get('delivered_at')]
    eligible = sorted([r for r in delivered if r.get('message_id') and not r.get('monitor_gap')
                       and not r.get('path_uncertain') and r['status'] in {'STOPPED', 'TP2_HIT', 'TIME_EXIT'}
                       and isinstance(r.get('result_r'), (float, int))
                       and math.isfinite(r['result_r'])], key=lambda r: r.get('closed_at', r['created_at']))
    # A missing legacy spread estimate cannot silently count as zero cost.
    eligible = [r for r in eligible if isinstance(r.get('context', {}).get('spread_r'), (float, int))
                and math.isfinite(r['context']['spread_r']) and r['context']['spread_r'] >= 0]
    values = [r['result_r'] - r['context']['spread_r'] - slippage_r for r in eligible]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    groups = defaultdict(list)
    for r, value in zip(eligible, values):
        ctx = r.get('context', {})
        key = f"v{ctx.get('lifecycle_version', 'legacy')} / {ctx.get('quality_tier', '?')} / {ctx.get('trigger_mode', 'unclassified')}"
        groups[key].append(value)
    statuses = Counter(r['status'] for r in delivered if r not in eligible)
    lines = [f'Forward reference-price report: {len(delivered)} delivered/possibly delivered alerts.',
             f'Complete eligible outcomes: {len(values)}; unscored/open: {len(delivered) - len(values)}.',
             f'Costs: recorded spread estimate + assumed {slippage_r:.2f}R slippage per trade.']
    lines.append(f'Outcome coverage: {len(values)}/{len(delivered)}; risk-warned alerts: {sum(bool(r.get("risk_warnings")) for r in delivered)}.')
    if values:
        lines += [f'Net sensitivity: {sum(values):+.2f}R; average {sum(values)/len(values):+.2f}R.',
                  f'Positive net outcomes: {sum(v > 0 for v in values)}/{len(values)}.',
                  f'Closed-outcome cumulative drawdown: {drawdown:.2f}R (not account or intratrade drawdown).']
        for name, vals in sorted(groups.items())[:10]:
            lines.append(f'{name}: n={len(vals)}, mean {sum(vals)/len(vals):+.2f}R')
    else:
        lines.append('No eligible completed sample yet. No win rate or edge can be claimed.')
    if statuses:
        lines.append('Other states: ' + ', '.join(f'{k}={v}' for k, v in sorted(statuses.items())))
    lines.append('Original stop/target/time scenarios continue after warnings, regardless of user actions. Unknown/legacy withdrawals remain unscored; incomplete coverage can bias results. '
                 'These are hypothetical results, NOT actual fills/P&L or proof of profitability.')
    return '\n'.join(lines)


class TelegramControls:
    def __init__(self, engine, request=None):
        self.engine = engine
        self.request = request or self._request
        self.initialised = False

    def _request(self, method, payload=None):
        token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        if not token:
            raise RuntimeError('Telegram credentials unavailable')
        try:
            response = requests.post(f'https://api.telegram.org/bot{token}/{method}',
                                     json=payload or {}, timeout=(3, 20))
            body = response.json()
            if not response.ok or not body.get('ok'):
                raise RuntimeError('Telegram controls request rejected')
            return body['result']
        except (requests.RequestException, ValueError, KeyError):
            # Exception URLs can contain the bot token; never propagate them.
            raise RuntimeError('Telegram controls request failed') from None

    def _offset(self):
        row = self.engine.db.execute("SELECT value FROM settings WHERE key='telegram_offset'").fetchone()
        return int(row['value']) if row else None

    def _save_offset(self, value):
        self.engine.db.execute("INSERT OR REPLACE INTO settings VALUES ('telegram_offset',?)", (str(value),))

    def poll(self):
        if not self.initialised:
            if self.request('getWebhookInfo').get('url'):
                raise RuntimeError('Existing Telegram webhook: polling not enabled; webhook left unchanged')
            with self.engine.lock:
                offset = self._offset()
            if offset is None:
                tail = self.request('getUpdates', {'offset': -1, 'limit': 1, 'timeout': 0, 'allowed_updates': ['message']})
                with self.engine.lock, self.engine.db:
                    self._save_offset(max((u['update_id'] for u in tail), default=-1) + 1)
            self.initialised = True
            LOG.info('Telegram controls ready; saved offset and chat/sender authorisation enabled')
            self.engine.system_notice('controls-ready:' + self.engine.clock().isoformat(),
                                      '✅ Tracking controls ready. Send /help, or reply to a new alert with /entered, /skipped or /closed. No broker orders are executed.')
        with self.engine.lock:
            offset = self._offset()
        updates = self.request('getUpdates', {'offset': offset, 'limit': 50, 'timeout': 10, 'allowed_updates': ['message']})
        for update in sorted(updates, key=lambda u: u['update_id']):
            self.handle_update(update)

    def authorised(self, message):
        chat = message.get('chat') or {}
        sender = message.get('from') or {}
        expected_chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        if (not expected_chat or str(chat.get('id')) != expected_chat or sender.get('is_bot', False)
                or message.get('forward_origin')):
            return False
        if chat.get('type') == 'private':
            return str(sender.get('id')) == expected_chat
        allowed = {v.strip() for v in os.getenv('TELEGRAM_ALLOWED_USER_IDS', '').split(',') if v.strip()}
        return str(sender.get('id')) in allowed

    def handle_update(self, update):
        """Offset, position mutation and acknowledgement are one transaction."""
        e = self.engine
        with e.lock, e.db:
            ident = update['update_id']
            offset = self._offset()
            if offset is not None and ident < offset:
                return
            message = update.get('message') or {}
            if self.authorised(message):
                text = str(message.get('text', ''))
                if text.startswith('/'):
                    try:
                        age = e.clock().timestamp() - float(message.get('date', 0))
                        reply = ('Command is old or has an invalid timestamp; resend it if still intended.'
                                 if not -30 <= age <= 900 else self.command(text, message))
                    except (ValueError, TypeError):
                        reply = 'Invalid command. Send /help for the supported formats.'
                    if reply:
                        e._queue('command:' + str(ident), reply, kind='CONTROL')
            self._save_offset(ident + 1)

    def command(self, text, message):
        parts = text.strip().split()
        name = parts[0].split('@')[0].lower()
        e = self.engine
        if name == '/help':
            return HELP
        if name == '/report':
            return performance_report(e.records(False), float(e.settings.get('report_slippage_r', 0.10)))
        if name == '/status':
            missing = e.news.unavailable()
            sources = e.news.degraded_sources()
            lines = [f'Scanner workers: {"healthy" if e.workers_ready() else "warming up or degraded"}.',
                     'Required event coverage: ' + (', '.join(missing) if missing else 'ready'),
                     'Reduced sources: ' + (', '.join(sources) if sources else 'none'),
                     f'Active alert scenarios: {len(e.records())}; marked-open trades: {len(e.open_positions())}.']
            for ident, pos in e.open_positions().items():
                lines.append(f"{pos['symbol']} #{ident} — OPEN; data {'degraded' if pos.get('degraded') else 'see latest check'}; last check {pos.get('last_price_check', 'not yet received')}")
            lines.append('Marked-open trades only; not a broker account reconciliation.')
            return '\n'.join(lines)
        if name not in {'/entered', '/skipped', '/closed'}:
            return None
        args = parts[1:]
        reply_id = (message.get('reply_to_message') or {}).get('message_id')
        replied = e.db.execute('SELECT signal_id FROM outbox WHERE message_id=? AND signal_id IS NOT NULL', (reply_id,)).fetchone() if reply_id else None
        if replied:
            ident = replied['signal_id']
        elif args and re.fullmatch('[0-9a-f]{6,20}', args[0]):
            rows = e.db.execute('SELECT id FROM signals WHERE id LIKE ?', (args.pop(0) + '%',)).fetchall()
            if len(rows) != 1:
                return 'Signal ID is missing or ambiguous. Use the full ID or reply to the alert.'
            ident = rows[0]['id']
        else:
            return 'Include the signal ID, or reply to its alert. Send /help for examples.'
        row = e.db.execute('SELECT body FROM signals WHERE id=?', (ident,)).fetchone()
        if not row:
            return 'Unknown signal ID.'
        rec = json.loads(row['body'])
        if not rec.get('delivered_at'):
            return 'This alert was never sent; it cannot be marked as an entered trade.'
        old = e.db.execute('SELECT * FROM positions WHERE signal_id=?', (ident,)).fetchone()
        if name == '/entered':
            if len(args) > 1:
                return 'Use /entered ID with an optional single fill price.'
            fill = float(args[0]) if args else None
            if fill is not None and (not math.isfinite(fill) or fill <= 0):
                return 'Fill price must be a positive finite number.'
            if old and old['status'] in {'ENTERED', 'CLOSED'}:
                return f"#{ident} is already {old['status']}; no state changed."
            body = {'symbol': rec['symbol'], 'entered_at': e.clock().isoformat(), 'fill_price': fill,
                    'fill_source': 'user_reported' if fill is not None else 'not_supplied'}
            status = 'ENTERED'
            reply = f'Marked #{ident} OPEN. Monitoring continues until /closed {ident}, even if the alert expires or is invalidated. No broker order was placed.'
            if rec.get('entry_withdrawn') or rec['status'] not in {'OPEN', 'TP1_OPEN'}:
                reply += '\n⚠️ The underlying setup is already withdrawn/finished. This records your existing trade; it does NOT recommend entering now.'
        elif name == '/skipped':
            if args:
                return 'Use /skipped ID without a price.'
            if old and old['status'] in {'ENTERED', 'CLOSED'}:
                return 'An entered/closed trade cannot be marked skipped. Use /closed only after fully closing it yourself.'
            body, status = {'symbol': rec['symbol'], 'at': e.clock().isoformat()}, 'SKIPPED'
            reply = f'Marked #{ident} SKIPPED. Hypothetical alert outcomes remain separate.'
        else:
            if args:
                return 'Use /closed ID after fully closing at the broker; no price or P&L is inferred.'
            if not old or old['status'] != 'ENTERED':
                return 'No marked-open position for that ID. Nothing changed.'
            body, status = json.loads(old['body']), 'CLOSED'
            body['closed_at'] = e.clock().isoformat()
            reply = f'Marked #{ident} CLOSED on your confirmation. Position monitoring ended; no broker order was changed or P&L inferred.'
        e.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?,?)', (ident, status, json.dumps(body)))
        return reply
