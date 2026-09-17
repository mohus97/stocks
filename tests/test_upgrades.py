from datetime import timedelta
import json
import logging
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

import scanner
from conftest import frame, publish
from news_guard import Event
from reliability import ReliableScanner
from scanner_controls import TelegramControls, performance_report


def command(client, clock, monkeypatch, text, ident=1, sender=123, chat=123, chat_type='private', **extra):
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    msg = {'text': text, 'date': clock().timestamp(), 'chat': {'id': chat, 'type': chat_type},
           'from': {'id': sender}, **extra}
    client.handle_update({'update_id': ident, 'message': msg})


@pytest.mark.parametrize('side,stop,rows', [
    ('LONG', 98, [(100,100.1,97.5,97.9)]),
    ('SHORT', 102, [(100,102.5,99.9,102.1)]),
])
def test_entry_minute_current_close_warns_but_no_fill_is_inferred(engine, signal, clock, side, stop, rows):
    signal.side, signal.stop = side, stop
    signal.context['trigger_level'] = 99.5 if side == 'LONG' else 100.5
    rec = publish(engine, signal)
    clock.advance(20)
    engine.process_prices(rec['id'], frame('2026-09-17 12:00', rows))
    result = engine.records(False)[0]
    assert result['status'] == 'INVALIDATED'
    assert result['result_r'] is None
    assert 'crossing time' in result['reason']


def test_entry_minute_old_extremes_do_not_trigger_warning(engine, signal, clock):
    rec = publish(engine, signal)
    clock.advance(20)
    engine.process_prices(rec['id'], frame('2026-09-17 12:00', [(100,104,97,100)]))
    assert engine.records()[0]['status'] == 'OPEN'
    assert not engine.records()[0]['tp1_at']


def test_bar_before_entry_minute_cannot_trigger_current_stop_warning(engine, signal, clock):
    rec = publish(engine, signal)
    clock.advance(10)
    engine.process_prices(rec['id'], frame('2026-09-17 11:59', [(100,100,97,97.9)]))
    assert engine.records()[0]['status'] == 'OPEN'


@pytest.mark.parametrize('sender,chat', [(999,123),(123,999)])
def test_commands_reject_other_senders_and_chats(engine, signal, clock, monkeypatch, sender, chat):
    rec = publish(engine, signal)
    command(TelegramControls(engine), clock, monkeypatch, '/entered ' + rec['id'], sender=sender, chat=chat)
    assert engine.open_positions() == {}


def test_group_commands_require_explicit_sender_allowlist(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    client = TelegramControls(engine)
    monkeypatch.delenv('TELEGRAM_ALLOWED_USER_IDS', raising=False)
    command(client, clock, monkeypatch, '/entered ' + rec['id'], chat_type='group')
    assert not engine.open_positions()
    monkeypatch.setenv('TELEGRAM_ALLOWED_USER_IDS', '123')
    command(client, clock, monkeypatch, '/entered ' + rec['id'], ident=2, chat_type='group')
    assert rec['id'] in engine.open_positions()


def test_entered_survives_invalidation_expiry_and_restart(engine, signal, clock, cfg, tmp_path, monkeypatch):
    rec = publish(engine, signal)
    client = TelegramControls(engine)
    command(client, clock, monkeypatch, '/entered ' + rec['id'] + ' 100.05')
    with engine.lock, engine.db:
        engine._transition(engine.records()[0], 'INVALIDATED', 'test reversal')
    assert not engine.records()
    assert engine.watched_records()[0]['id'] == rec['id']
    restored = ReliableScanner(cfg, tmp_path, clock=clock, news=engine.news)
    assert restored.open_positions()[rec['id']]['fill_price'] == 100.05
    assert restored.watched_records()[0]['status'] == 'INVALIDATED'
    restored.db.close()


def test_command_dedup_and_close_are_persistent(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    client = TelegramControls(engine)
    command(client, clock, monkeypatch, '/entered ' + rec['id'], ident=10)
    command(client, clock, monkeypatch, '/closed ' + rec['id'], ident=10)
    assert rec['id'] in engine.open_positions()  # Duplicate update must do nothing.
    command(client, clock, monkeypatch, '/closed ' + rec['id'], ident=11)
    assert not engine.open_positions()
    command(client, clock, monkeypatch, '/entered ' + rec['id'], ident=12)
    assert not engine.open_positions()  # A historical command cannot reopen it.


def test_reply_to_alert_resolves_signal_and_fill(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    command(TelegramControls(engine), clock, monkeypatch, '/entered 100.1', reply_to_message={'message_id':rec['message_id']})
    assert engine.open_positions()[rec['id']]['fill_price'] == 100.1


@pytest.mark.parametrize('bad', ['nan','inf','-1','0','abc','100 101'])
def test_invalid_fill_rejected(engine, signal, clock, monkeypatch, bad):
    rec = publish(engine, signal)
    command(TelegramControls(engine), clock, monkeypatch, '/entered ' + rec['id'] + ' ' + bad)
    assert not engine.open_positions()


def test_old_command_does_not_mark_open(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    command(TelegramControls(engine), clock, monkeypatch, '/entered ' + rec['id'], date=clock().timestamp()-901)
    assert not engine.open_positions()


def test_skip_cannot_hide_an_entered_position(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    client = TelegramControls(engine)
    command(client, clock, monkeypatch, '/entered ' + rec['id'])
    command(client, clock, monkeypatch, '/skipped ' + rec['id'], ident=2)
    assert rec['id'] in engine.open_positions()


def test_first_poll_discards_history_then_persists_new_updates(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    calls = []
    def request(method, payload=None):
        calls.append((method, payload))
        if method == 'getWebhookInfo': return {'url':''}
        if payload['offset'] == -1: return [{'update_id':40}]
        return [{'update_id':41, 'message':{'chat':{'id':123,'type':'private'},'from':{'id':123},
                 'date':clock().timestamp(),'text':'/entered ' + rec['id']}}]
    client = TelegramControls(engine, request=request)
    client.poll()
    client.poll()
    assert client._offset() == 42
    assert len(engine.open_positions()) == 1
    assert sum(c[0] == 'getWebhookInfo' for c in calls) == 1


def test_existing_webhook_is_not_deleted(engine):
    calls = []
    def request(method, payload=None):
        calls.append(method)
        return {'url':'https://existing.example/webhook'}
    with pytest.raises(RuntimeError, match='webhook'):
        TelegramControls(engine, request=request).poll()
    assert calls == ['getWebhookInfo']


def test_controls_transport_never_exposes_token(engine, monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'secret-token')
    monkeypatch.setattr(requests, 'post', lambda *a, **k: (_ for _ in ()).throw(requests.Timeout('https://secret-token')))
    with pytest.raises(RuntimeError) as err:
        TelegramControls(engine)._request('getUpdates')
    assert 'secret-token' not in str(err.value)


def test_position_keeps_stop_monitoring_after_news_withdrawal(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    client = TelegramControls(engine)
    command(client, clock, monkeypatch, '/entered ' + rec['id'])
    with engine.lock, engine.db:
        engine._transition(engine.records()[0], 'NEWS_WITHDRAWN', 'headline')
    clock.advance(60)
    d = frame('2026-09-17 12:01', [(100,100.1,97,97.8)])
    monkeypatch.setattr(engine, 'fetch_monitor_frame', lambda rec: d)
    engine.monitor_once()
    engine.monitor_once()
    engine.deliver_once()
    assert rec['id'] in engine.open_positions()
    assert sum('marked-open position is beyond' in s for s in engine.sent) == 1
    command(client, clock, monkeypatch, '/closed ' + rec['id'], ident=2)
    assert not engine.watched_records()


def test_position_data_outage_and_recovery_are_not_silent(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    command(TelegramControls(engine), clock, monkeypatch, '/entered ' + rec['id'])
    engine.process_position_prices(rec, None)
    engine.process_position_prices(rec, None)
    engine.deliver_once()
    assert sum('marked-open position has stale' in s for s in engine.sent) == 1
    engine.process_position_prices(rec, frame('2026-09-17 12:00', [(100,100.1,99.9,100)]))
    engine.deliver_once()
    assert any('Fresh data restored for your marked-open' in s for s in engine.sent)


def test_quota_reserves_active_risk_not_discovery(engine, signal, clock):
    rec = publish(engine, signal)
    rec['item']['provider'] = 'twelvedata'
    with engine.lock, engine.db:
        engine._save(rec)
        engine.db.execute('INSERT INTO credits VALUES (?,?,?)', (clock().timestamp()-60, 770, 'core'))
    assert not engine.reserve_td(1, 'entry')
    assert not engine.reserve_td(4, 'core')
    assert engine.reserve_td(1, 'monitor')


def test_minute_quota_keeps_room_for_risk_worker(engine, signal, clock):
    rec = publish(engine, signal)
    rec['item']['provider'] = 'twelvedata'
    with engine.lock, engine.db:
        engine._save(rec)
    assert engine.reserve_td(7, 'core')
    assert not engine.reserve_td(1, 'entry')
    assert engine.reserve_td(1, 'monitor')


def test_core_stall_fails_worker_health(engine, clock):
    clock.advance(421)
    engine.health.update({n:clock().isoformat() for n in ('news','prices','delivery')})
    assert not engine.workers_ready()


def test_heartbeat_is_structured_and_contains_no_credentials(engine, caplog):
    with caplog.at_level(logging.INFO): engine.heartbeat_once()
    line = next(r.message for r in caplog.records if 'SCANNER_HEALTH ' in r.message)
    data = json.loads(line.split('SCANNER_HEALTH ')[1])
    assert data['version'] == 2 and data['marked_open'] == 0
    assert 'token' not in line.lower()


def test_independent_news_category_fallback(engine, clock):
    engine.news.success.pop('world')
    engine.news.success.pop('business')
    assert not engine.news.unavailable()
    assert {'world','business'} <= set(engine.news.degraded_sources())
    engine.news.success.pop('bbc_world')
    assert engine.news.unavailable() == ['world']


def test_central_bank_outage_blocks_affected_currency_and_warns(engine):
    engine.news.success.pop('boe')
    assert 'boe' in engine.news.gate({'symbol':'GBPUSD','type':'forex'})
    assert engine.news.gate({'symbol':'USDJPY','type':'forex'}) is None
    engine.review_news()
    engine.review_news()
    engine.deliver_once()
    assert sum('Reduced event coverage: boe' in s for s in engine.sent) == 1


def test_report_is_cost_adjusted_and_excludes_unknown_outcomes(engine, signal):
    rec = publish(engine, signal)
    winner = dict(rec, status='TP2_HIT', result_r=.975, closed_at='2026-09-17T13:00:00Z')
    loser = dict(rec, status='STOPPED', result_r=-1, closed_at='2026-09-17T14:00:00Z')
    ambiguous = dict(rec, status='AMBIGUOUS', result_r=None)
    text = performance_report([winner,loser,ambiguous], .10)
    assert 'Complete eligible outcomes: 2' in text
    assert '-0.33R' in text and 'drawdown: 1.15R' in text
    assert 'AMBIGUOUS=1' in text and 'NOT actual fills' in text


def test_empty_report_claims_no_edge():
    assert 'No win rate or edge can be claimed' in performance_report([])


def test_risk_fetch_continues_after_discovery_session(cfg, monkeypatch, capsys):
    monkeypatch.setenv('TWELVE_DATA_API_KEY', 'test-private-key')
    monkeypatch.setattr(scanner, 'twelve_data_active', lambda cfg: False)
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise requests.Timeout('url?apikey=test-private-key')
    monkeypatch.setattr(requests, 'get', fail)
    assert scanner.fetch_twelvedata_1m(SimpleNamespace(data_symbol='GBP/USD'), dict(cfg, _td_purpose='monitor')) is None
    assert len(calls) == 1
    assert 'test-private-key' not in capsys.readouterr().out
    scanner.fetch_twelvedata_1m(SimpleNamespace(data_symbol='GBP/USD'), cfg)
    assert len(calls) == 1  # Discovery remains session-limited.


def test_telegram_offset_survives_new_client(engine, clock, monkeypatch):
    command(TelegramControls(engine), clock, monkeypatch, '/help', ident=100)
    calls = []
    def request(method, payload=None):
        if method == 'getWebhookInfo': return {'url':''}
        calls.append(payload)
        return []
    TelegramControls(engine, request=request).poll()
    assert calls[0]['offset'] == 101


def test_position_stop_not_lost_when_hypothetical_scenario_finishes(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    command(TelegramControls(engine), clock, monkeypatch, '/entered ' + rec['id'])
    clock.advance(130)
    # A target in a complete bar, then a current price beyond the original stop.
    d = frame('2026-09-17 12:01', [(100,104,99.5,103), (103,103.1,97,97.5)])
    monkeypatch.setattr(engine, 'fetch_monitor_frame', lambda rec: d)
    engine.monitor_once()
    engine.deliver_once()
    assert engine.records(False)[0]['status'] == 'TP2_HIT'
    assert any('marked-open position is beyond' in text for text in engine.sent)
    assert engine.open_positions()


def test_forwarded_commands_are_not_tracking_instructions(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    command(TelegramControls(engine), clock, monkeypatch, '/entered ' + rec['id'], forward_origin={'type':'user'})
    assert not engine.open_positions()
