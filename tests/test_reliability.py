from copy import deepcopy
from datetime import timedelta
import json
import pandas as pd
import pytest
import requests

import scanner
import reliability
from reliability import DeliveryError, ReliableScanner, evaluate_bar, format_trade, prepare_signal, signal_key
from news_guard import Event
from conftest import frame, publish


def test_single_plan_matches_telegram_ledger_and_sizing(engine, signal, cfg):
    rec = publish(engine, signal)
    assert rec['tp1'] == 101.5 and rec['tp2'] == 103.
    assert '101.50' in engine.sent[0] and '103.00' in engine.sent[0]
    assert rec['status'] == 'OPEN' and rec['message_id'] == 1
    assert rec['risk_gbp'] <= cfg['risk']['account_cash_gbp'] * 0.01
    assert rec['context']['exit_plan'] == '70_30_original_stop'


def test_dedupe_happens_before_delivery_even_if_price_changes(engine, signal):
    publish(engine, signal)
    signal.price += .01
    engine.publish(signal, {'symbol': 'NVDA', 'type': 'stock'})
    engine.deliver_once()
    assert len(engine.sent) == 1 and len(engine.records(False)) == 1


def test_opposite_active_signal_is_not_emitted(engine, signal):
    publish(engine, signal)
    signal.side, signal.stop = 'SHORT', 102.
    signal.context['trigger_level'] = 100.5
    engine.publish(signal, {'symbol': 'NVDA', 'type': 'stock'})
    assert len(engine.records()) == 1


@pytest.mark.parametrize('kind', ['index', 'B+', 'nan'])
def test_unverified_index_lower_tier_and_nan_are_blocked(engine, signal, kind):
    if kind == 'index': signal.market_type = 'index'
    if kind == 'B+': signal.context['quality_tier'] = 'B+'
    if kind == 'nan': signal.price = float('nan')
    engine.publish(signal, {'symbol': signal.symbol, 'type': signal.market_type})
    assert engine.records(False) == []


def test_new_news_withdraws_existing_signal_once(engine, signal, clock):
    rec = publish(engine, signal)
    e = Event('war1', 'Missile strike closes Strait of Hormuz', 'world', 'https://example.com/event', clock().isoformat(), 'headline')
    engine.news.events[e.id] = e
    engine.review_news()
    engine.review_news()
    engine.deliver_once()
    row = engine.records(False)[0]
    assert row['status'] == 'OPEN' and row['entry_withdrawn']
    assert 'news:war1' in row['risk_warnings']
    assert sum('RISK WARNING' in text for text in engine.sent) == 1
    assert any(e.url in text for text in engine.sent)


def test_withdrawal_delivery_retries_after_restart(engine, signal, clock, tmp_path, cfg):
    rec = publish(engine, signal)
    with engine.lock, engine.db:
        engine._transition(rec, 'INVALIDATED', 'Breakout failed')
    engine.notify = lambda text: (_ for _ in ()).throw(DeliveryError())
    engine.deliver_once()
    clock.advance(10)
    sent = []
    restored = ReliableScanner(cfg, tmp_path, clock=clock, news=engine.news, notify=lambda text: sent.append(text) or 123)
    restored.deliver_once()
    restored.deliver_once()
    assert len(sent) == 1 and 'INVALIDATED' in sent[0]
    restored.db.close()


def test_send_race_does_not_resurrect_cancelled_signal(engine, signal, clock):
    engine.publish(signal, {'symbol': 'NVDA', 'type': 'stock'})
    def send_and_cancel(text):
        with engine.lock, engine.db:
            rec = engine.records()[0]
            engine._transition(rec, 'INVALIDATED', 'News arrived while sending')
        return 9
    engine.notify = send_and_cancel
    engine.deliver_once()
    assert engine.records(False)[0]['status'] == 'INVALIDATED'
    assert engine.records(False)[0]['message_id'] == 9


def test_unconfirmed_delivery_remains_monitored_not_claimed_success(engine, signal):
    engine.notify = lambda text: (_ for _ in ()).throw(DeliveryError(5, uncertain=True))
    engine.publish(signal, {'symbol': 'NVDA', 'type': 'stock'})
    engine.deliver_once()
    rec = engine.records()[0]
    assert rec['status'] == 'DELIVERY_UNKNOWN' and not rec.get('message_id')


def test_expired_unsent_signal_never_becomes_a_trade(engine, signal, clock, monkeypatch):
    engine.publish(signal, {'symbol': 'NVDA', 'type': 'stock'})
    clock.advance(46)
    monkeypatch.setattr(engine, '_pending_is_valid', ReliableScanner._pending_is_valid.__get__(engine))
    engine.deliver_once()
    assert not engine.sent
    assert engine.records(False)[0]['status'] == 'UNSENT'


def test_final_price_recheck_rejects_failed_breakout(engine, signal, clock, monkeypatch):
    engine.publish(signal, {'symbol': 'NVDA', 'type': 'stock'})
    monkeypatch.setattr(engine, '_pending_is_valid', ReliableScanner._pending_is_valid.__get__(engine))
    monkeypatch.setattr(engine, 'fetch_monitor_frame', lambda rec, **kw: frame('2026-09-17 12:00', [(99.3,99.6,99.2,99.4)]))
    engine.deliver_once()
    assert not engine.sent and engine.records(False)[0]['status'] == 'UNSENT'


def test_pre_alert_highs_and_lows_cannot_win_or_lose(engine, signal, clock):
    rec = publish(engine, signal)  # sent at 12:00:15
    clock.advance(110)
    d = frame('2026-09-17 11:59', [(100, 105, 97, 100), (100, 105, 97, 100), (100, 100.2, 99.8, 100), (100, 100.2, 99.8, 100)])
    engine.process_prices(rec['id'], d)
    result = engine.records()[0]
    assert result['status'] == 'OPEN' and result['tp1_at'] is None
    assert result['last_bar'] == '2026-09-17T12:01:00+00:00'


def test_failed_breakout_warns_before_stop(engine, signal, clock):
    rec = publish(engine, signal)
    clock.advance(180)
    d = frame('2026-09-17 12:01', [(99.4,99.5,99.1,99.3), (99.3,99.4,99.1,99.2), (99.2,99.3,99.1,99.2)])
    engine.process_prices(rec['id'], d)
    engine.deliver_once()
    assert engine.records()[0]['entry_withdrawn']
    assert 'breakout-failed' in engine.records()[0]['risk_warnings']
    assert any('Breakout failed' in text for text in engine.sent)


def test_intrabar_stop_breach_alerts_without_claiming_a_fill(engine, signal, clock):
    rec = publish(engine, signal)
    clock.advance(60)
    d = frame('2026-09-17 12:01', [(100,100.1,97.5,97.9)])
    engine.process_prices(rec['id'], d)
    result = engine.records(False)[0]
    assert result['status'] == 'OPEN' and result['result_r'] is None
    assert 'stop-breach' in result['risk_warnings']


def test_ambiguous_bar_is_not_counted_as_a_win(engine, signal, clock):
    rec = publish(engine, signal)
    clock.advance(120)
    d = frame('2026-09-17 12:01', [(100,104,97,100), (100,101,99,100)])
    engine.process_prices(rec['id'], d)
    result = engine.records(False)[0]
    assert result['status'] == 'AMBIGUOUS' and result['result_r'] is None


def test_partial_profit_and_runner_stop_are_weighted_correctly(engine, signal):
    rec = publish(engine, signal)
    assert evaluate_bar(rec, pd.Series({'Open':100, 'High':101.6, 'Low':99.5, 'Close':101.5}), 'first')
    assert rec['status'] == 'TP1_OPEN'
    evaluate_bar(rec, pd.Series({'Open':100, 'High':100.2, 'Low':97.9, 'Close':98}), 'second')
    assert rec['result_r'] == pytest.approx(0.7 * .75 - .3)


def test_gap_loss_is_not_capped_at_one_r(engine, signal):
    rec = publish(engine, signal)
    evaluate_bar(rec, pd.Series({'Open':96, 'High':97, 'Low':95, 'Close':96}), 'gap')
    assert rec['result_r'] == -2.


def test_monitoring_gaps_exclude_simulated_performance(engine, signal, clock):
    rec = publish(engine, signal)
    clock.advance(180)
    d = frame('2026-09-17 12:02', [(100,103.1,99.8,103), (103,103.1,102.9,103)])
    engine.process_prices(rec['id'], d)
    result = engine.records(False)[0]
    assert result['status'] == 'TP2_HIT' and result['result_r'] is None


def test_missing_prices_generate_one_warning_then_recovery(engine, signal, clock):
    rec = publish(engine, signal)
    engine.process_prices(rec['id'], None)
    engine.process_prices(rec['id'], None)
    engine.deliver_once()
    assert sum('Price monitoring degraded' in s for s in engine.sent) == 1
    clock.advance(60)
    engine.process_prices(rec['id'], frame('2026-09-17 12:01', [(100,100.1,99.9,100)]))
    engine.deliver_once()
    assert any('monitoring restored' in s for s in engine.sent)


def test_shared_credit_limits_and_daily_restart(engine, clock, cfg, tmp_path):
    assert engine.reserve_td(4, 'core')
    assert engine.reserve_td(4, 'core')
    assert not engine.reserve_td(1, 'monitor')
    restored = ReliableScanner(cfg, tmp_path, clock=clock, news=engine.news)
    assert not restored.reserve_td(1, 'core')
    clock.advance(60)
    assert restored.reserve_td(1, 'monitor')
    with restored.db:
        restored.db.execute('INSERT INTO credits VALUES (?,?,?)', (clock().timestamp()-60,790,'core'))
    assert restored.reserve_td(1, 'monitor')  # Risk checks may use the final credit.
    assert not restored.reserve_td(1, 'core')
    clock.advance(86400)
    assert restored.reserve_td(4, 'core')
    restored.db.close()


def test_telegram_json_failure_is_not_success(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'test-chat')
    class Response:
        ok = True
        def json(self): return {'ok': False, 'parameters': {'retry_after': 37}}
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: Response())
    with pytest.raises(DeliveryError) as err:
        reliability.send_telegram('test')
    assert err.value.retry_after == 37


def test_telegram_timeout_does_not_expose_token(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'private-token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'private-chat')
    def fail(*a, **kw): raise requests.Timeout('https://api.telegram.org/botprivate-token')
    monkeypatch.setattr(requests, 'post', fail)
    with pytest.raises(DeliveryError) as err:
        reliability.send_telegram('test')
    assert 'private-token' not in str(err.value)


def test_core_runtime_queues_without_legacy_tracking_or_execution(engine, signal, cfg, monkeypatch, clock):
    cfg['watchlist'] = [{'symbol':'NVDA','type':'stock'}]
    monkeypatch.setattr(scanner, 'RELIABILITY', engine)
    monkeypatch.setattr(scanner, 'fetch_item', lambda *args: frame('2026-09-17 12:00', [(100,101,99,100)]))
    monkeypatch.setattr(scanner, 'score_signal', lambda *args: signal)
    monkeypatch.setattr(engine, 'observe_5m', lambda *args: None)
    def forbidden(*args, **kwargs): raise AssertionError('Legacy path must not run')
    monkeypatch.setattr(scanner, 'telegram_notify', forbidden)
    monkeypatch.setattr(scanner, 'register_tracked_signal', forbidden)
    monkeypatch.setattr(scanner, 'maybe_execute_ig_demo', forbidden)
    scanner.scan_once(cfg, include_twelvedata=False)
    assert engine.records()[0]['status'] == 'PENDING'


def test_dead_monitor_worker_blocks_new_entries(engine, signal, clock):
    engine.threads = ['test-worker']
    engine.health.update({name: clock().isoformat() for name in ('news','delivery')})
    engine.publish(signal, {'symbol':'NVDA','type':'stock'})
    assert engine.records(False) == []


def test_active_5m_reversal_withdraws_thesis(engine, signal, clock, monkeypatch):
    rec = publish(engine, signal)
    clock.advance(10*60)
    snap = {'row': pd.Series(name=pd.Timestamp('2026-09-17T12:05Z')),
        'results': {'LONG': {'veto': True, 'veto_reasons': ['15m regime opposite'], 'score':2.},
                    'SHORT': {'veto': False, 'score':7.}}}
    monkeypatch.setattr(scanner, '_decision_snapshot', lambda *args: snap)
    engine.observe_5m(frame('2026-09-17 12:05',[(100,101,99,100)]), {'symbol':'NVDA','type':'stock'})
    assert '5m-thesis' in engine.records()[0]['risk_warnings']


def test_opposite_short_breakout_failure(engine, signal, clock):
    signal.side, signal.stop = 'SHORT', 102.
    signal.context['trigger_level'] = 100.5
    rec = publish(engine, signal)
    clock.advance(180)
    engine.process_prices(rec['id'], frame('2026-09-17 12:01',
        [(100.6,100.9,100.5,100.7),(100.7,101,100.6,100.8),(100.8,101,100.7,100.8)]))
    assert 'breakout-failed' in engine.records()[0]['risk_warnings']


def test_filtered_bplus_is_delivered_with_speculative_label(engine, signal, cfg):
    assert cfg['reliability']['allow_fast'] is True
    signal.score = 6.5
    signal.context.update(quality_tier='B+', score_components={
        'trigger_1m': 1.0, 'setup_5m': 0.5, 'structure_location': 1.0,
        'trend_volatility': 0.5, 'momentum_quality': 0.5})
    rec = publish(engine, signal)
    assert rec['status'] == 'OPEN'
    assert 'B+ · SPECULATIVE' in engine.sent[0]
    assert rec['tp1'] == 101.0 and rec['tp2'] == 102.0
    assert 'TP1 (70%): 101.00' in engine.sent[0]
    assert rec['risk_gbp'] <= cfg['risk']['account_cash_gbp'] * .01


@pytest.mark.parametrize('failure', ['disabled','score','confirmation','structure','momentum','spread'])
def test_bplus_optin_does_not_bypass_quality_or_spread(engine, signal, failure, monkeypatch):
    import spread_runtime
    signal.score = 6.5
    signal.context.update(quality_tier='B+', score_components={
        'trigger_1m': 1.0, 'setup_5m': 0.5, 'structure_location': 1.0,
        'trend_volatility': 0.5, 'momentum_quality': 0.5})
    if failure == 'disabled': engine.settings['allow_fast'] = False
    if failure == 'score': signal.score = 6.49
    if failure == 'confirmation': signal.context['score_components']['trigger_1m'] = 0
    if failure == 'structure': signal.context['score_components']['structure_location'] = 0.5
    if failure == 'momentum': signal.context['score_components']['momentum_quality'] = 0
    if failure == 'spread':
        original = spread_runtime._spread_profile
        monkeypatch.setattr(spread_runtime, '_spread_profile', lambda sig: dict(original(sig), spread_r=0.16))
    engine.publish(signal, {'symbol':signal.symbol,'type':'stock'})
    engine.deliver_once()
    assert not engine.sent and not engine.records(False)
