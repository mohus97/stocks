import numpy as np
import pandas as pd
import pytest
import scanner
from market_data import clean_frame, closed_bars, fresh_frame, resample_closed
from conftest import frame


def test_completed_bars_do_not_require_an_unfinished_last_row(clock):
    d = frame('2026-09-17 11:45', [(100, 101, 99, 100)] * 3, '5min')
    assert len(closed_bars(d, 5)) == 3


def test_unfinished_candle_is_excluded(clock):
    d = frame('2026-09-17 11:50', [(100, 101, 99, 100)] * 3, '5min')
    assert len(closed_bars(d, 5)) == 2


def test_resample_uses_bar_start_and_discards_partial_groups(clock):
    d = frame('2026-09-17 11:00', [(100+i, 101+i, 99+i, 100+i) for i in range(11)], '5min')
    out = resample_closed(d, '15min')
    assert list(out.index.minute) == [0, 15, 30]
    assert out.iloc[0].Close == 102
    assert out.iloc[0].Open == 100


def test_missing_bar_does_not_create_complete_higher_timeframe(clock):
    d = frame('2026-09-17 11:00', [(100, 101, 99, 100)] * 12, '5min')
    assert len(resample_closed(d, '1h')) == 1
    assert resample_closed(d.drop(d.index[3]), '1h').empty


@pytest.mark.parametrize('corruption', ['duplicate', 'nan', 'infinity', 'inverted', 'zero'])
def test_corrupt_price_data_is_rejected(corruption):
    d = frame('2026-09-17 11:00', [(100., 101., 99., 100.)] * 3)
    if corruption == 'duplicate':
        d.index = [d.index[0]] * 3
    else:
        d.iloc[-1, 1] = {'nan': np.nan, 'infinity': np.inf, 'inverted': 98, 'zero': 0}[corruption]
    assert clean_frame(d).empty


def test_future_and_stale_quotes_fail(clock):
    assert not fresh_frame(frame('2026-09-17 12:01', [(100, 101, 99, 100)]), 2)
    assert not fresh_frame(frame('2026-09-17 11:57', [(100, 101, 99, 100)]), 2)


def test_rsi_extremes_are_not_nan():
    assert scanner.rsi(pd.Series(range(30))).iloc[-1] == 100
    assert scanner.rsi(pd.Series([100.] * 30)).iloc[-1] == 50
    assert scanner.rsi(pd.Series(range(30, 0, -1))).iloc[-1] == 0


@pytest.mark.parametrize('side,live', [('LONG', 99.9), ('SHORT', 100.1)])
def test_direct_signal_requires_breakout_retention(monkeypatch, cfg, side, live):
    snap = {'row': {'Close': 100.2 if side == 'LONG' else 99.8},
            'prev': {'High': 100., 'Low': 100.}, 'live_price': live, 'atr': 1.,
            'results': {s: {'veto': s != side, 'breakout': True, 'directional_candle': True, 'score': 7.} for s in ('LONG', 'SHORT')}}
    monkeypatch.setattr(scanner, '_decision_snapshot', lambda *args: snap)
    assert scanner.score_signal(None, {'symbol': 'NVDA'}, cfg) is None


def test_closed_history_snapshot_has_no_phantom_htf_bar(clock, cfg):
    prices = 100 + np.arange(120) * 0.02 + np.sin(np.arange(120)) * 0.1
    d = frame('2026-09-17 02:00', [(p, p+.2, p-.2, p+.02) for p in prices], '5min')
    snap = scanner._decision_snapshot(d, {'symbol': 'EURUSD', 'type': 'forex'}, cfg)
    assert snap is not None
    assert snap['row'].name == pd.Timestamp('2026-09-17T11:55Z')
    assert snap['context']['setup_bar'] == '2026-09-17T11:55:00+00:00'
