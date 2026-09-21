import json
import logging
import pandas as pd
import pytest
import scanner
from reliability import ReliableScanner
from scanner_controls import performance_report
from conftest import frame, publish


@pytest.mark.parametrize('reason', ['momentum exhausted', 'no trend/vol or momentum confirmation',
    'breakout into nearby structure', 'extended upside impulse — wait for retrace', 'extreme volatility'])
def test_entry_veto_does_not_withdraw_existing_trade(engine, signal, clock, monkeypatch, reason):
    publish(engine, signal)
    clock.advance(600)
    snapshot = {'row': pd.Series(name=pd.Timestamp('2026-09-17T12:05Z')),
        'results': {'LONG': {'veto':True,'veto_reasons':[reason],'score':5},
                    'SHORT': {'veto':True,'score':3}}}
    monkeypatch.setattr(scanner, '_decision_snapshot', lambda *a: snapshot)
    engine.observe_5m(frame('2026-09-17 12:05', [(101,102,100,101)]), {'symbol':'NVDA'})
    assert engine.records()[0]['status'] == 'OPEN'
    assert not engine.records()[0].get('entry_withdrawn')


def warn(engine, rec):
    with engine.lock, engine.db:
        engine._warn(rec, 'breakout-failed', 'Breakout failed')


@pytest.mark.parametrize('outcome,rows,expected', [
    ('TP2_HIT', [(100,103.2,99.8,103),(103,103.2,102.8,103)], .975),
    ('STOPPED', [(100,100.2,97.8,98),(98,98.2,97.8,98)], -1),
])
def test_warned_trade_still_records_wins_and_losses(engine, signal, clock, outcome, rows, expected):
    rec = publish(engine, signal)
    warn(engine, rec)
    clock.advance(120)
    engine.process_prices(rec['id'], frame('2026-09-17 12:01', rows))
    result = engine.records(False)[0]
    assert result['status'] == outcome
    assert result['result_r'] == pytest.approx(expected)
    report = performance_report([result])
    assert 'Complete eligible outcomes: 1' in report
    assert 'risk-warned alerts: 1' in report
    assert 'v3 / A-TIER' in report


def test_warning_and_tracking_survive_restart_without_duplicates(engine, signal, clock, cfg, tmp_path, caplog):
    rec = publish(engine, signal)
    with caplog.at_level(logging.INFO): warn(engine, rec)
    assert any('SIGNAL_WARNING' in r.message and 'NVDA' in r.message for r in caplog.records)
    sent=[]
    restored = ReliableScanner(cfg, tmp_path, clock=clock, news=engine.news, notify=lambda s: sent.append(s) or 55)
    warn(restored, restored.records()[0])
    restored.deliver_once()
    assert len(sent) == 1
    assert restored.watched_records()[0]['entry_withdrawn']
    assert not restored._pending_is_valid(restored.records()[0])
    restored.db.close()


def test_tp1_runner_survives_warning_and_partial_stop_is_scored(engine, signal, clock):
    rec = publish(engine, signal)
    clock.advance(120)
    engine.process_prices(rec['id'], frame('2026-09-17 12:01', [(100,101.6,99.9,101.5),(101.5,101.6,101.4,101.5)]))
    rec=engine.records()[0]
    assert rec['tp1_at']
    warn(engine, rec)
    clock.advance(60)
    engine.process_prices(rec['id'], frame('2026-09-17 12:02', [(101.5,101.6,97.9,98),(98,98.1,97.9,98)]))
    rec=engine.records(False)[0]
    assert rec['status']=='STOPPED'
    assert rec['result_r']==pytest.approx(.225)


@pytest.mark.parametrize('side,close,expected', [('LONG',101,.5),('LONG',99,-.5),('SHORT',99,.5)])
def test_time_exit_uses_predeadline_close_not_later_price(engine, signal, clock, side, close, expected):
    signal.side=side
    if side=='SHORT':
        signal.stop=102
        signal.context['trigger_level']=100.5
    engine.settings['max_signal_minutes']=3
    rec=publish(engine,signal)
    warn(engine,rec)
    clock.advance(240)
    engine.process_prices(rec['id'],frame('2026-09-17 12:01', [
        (100,101,99,100),(100,101,99,close),(100,110,90,100),(100,101,99,100)]))
    rec=engine.records(False)[0]
    assert rec['status']=='TIME_EXIT'
    assert rec['exit_mark']['at']=='2026-09-17T12:03:00+00:00'
    assert rec['result_r']==pytest.approx(expected)
    assert 'Complete eligible outcomes: 1' in performance_report([rec])


def test_time_exit_retains_tp1_weight(engine, signal, clock):
    engine.settings['max_signal_minutes']=3
    rec=publish(engine,signal)
    clock.advance(180)
    engine.process_prices(rec['id'],frame('2026-09-17 12:01',[
        (100,101.6,99.9,101.5),(101.5,101.6,100.9,101),(101,101.1,100.9,101)]))
    rec=engine.records(False)[0]
    assert rec['status']=='TIME_EXIT'
    assert rec['result_r']==pytest.approx(.675)


def test_missing_timeout_path_stays_unknown_not_fabricated(engine, signal, clock, monkeypatch):
    engine.settings['max_signal_minutes']=3
    rec=publish(engine,signal)
    clock.advance(240)
    monkeypatch.setattr(engine,'fetch_monitor_frame',lambda rec: None)
    engine.monitor_once()
    rec=engine.records(False)[0]
    assert rec['status']=='EXPIRED' and rec['result_r'] is None
    assert 'Outcome coverage: 0/1' in performance_report([rec])


def test_intrabar_warning_resolves_to_stop_after_candle_closes(engine, signal, clock):
    rec=publish(engine,signal)
    clock.advance(60)
    d=frame('2026-09-17 12:01', [(100,100.1,97.5,97.9)])
    engine.process_prices(rec['id'],d)
    assert 'stop-breach' in engine.records()[0]['risk_warnings']
    clock.advance(60)
    engine.process_prices(rec['id'],d)
    rec=engine.records(False)[0]
    assert rec['status']=='STOPPED' and rec['result_r']==-1


def test_repeated_warning_persists_last_processed_bar(engine,signal,clock):
    rec=publish(engine,signal)
    clock.advance(180)
    d=frame('2026-09-17 12:01',[(99.4,99.5,99.1,99.3)]*3)
    engine.process_prices(rec['id'],d)
    clock.advance(60)
    engine.process_prices(rec['id'],frame('2026-09-17 12:02',[(99.4,99.5,99.1,99.3)]*3))
    assert engine.records()[0]['last_bar']=='2026-09-17T12:03:00+00:00'
    engine.deliver_once()
    assert sum('RISK WARNING' in s for s in engine.sent)==1


def test_delivery_minute_uncertainty_cannot_turn_into_counted_win(engine,signal,clock):
    rec=publish(engine,signal)
    clock.advance(120)
    engine.process_prices(rec['id'],frame('2026-09-17 12:00',[
        (100,104,97,100),(100,103.2,99.8,103),(103,103.1,102.9,103)]))
    rec=engine.records(False)[0]
    assert rec['status']=='TP2_HIT' and rec['path_uncertain']
    assert rec['result_r'] is None
    assert 'Outcome coverage: 0/1' in performance_report([rec])


def test_news_warning_then_stop_counts_loss(engine,signal,clock):
    from news_guard import Event
    rec=publish(engine,signal)
    engine.news.events={'news':Event('news','Israel attacks Iran','world','https://example.com',clock().isoformat(),'headline')}
    engine.review_news()
    clock.advance(120)
    engine.process_prices(rec['id'],frame('2026-09-17 12:01',[(100,100.1,97.9,98),(98,98.1,97.9,98)]))
    rec=engine.records(False)[0]
    assert rec['status']=='STOPPED' and rec['result_r']==-1
    assert 'Complete eligible outcomes: 1' in performance_report([rec])
