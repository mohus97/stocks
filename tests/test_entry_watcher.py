import copy
import pandas as pd
import pytest
import scanner
from conftest import frame


def candidate(clock):
    return scanner.Candidate('NVDA','NVIDIA','stock','yahoo','NVDA','LONG',7.,100.,1.,99.9,98.,101.,'setup',
                             clock().timestamp()-90, {'score_components': {'htf_regime': 2.}})


def trigger_frame(clock):
    return frame('2026-09-17 11:56', [(99.7,99.8,99.6,99.7),(99.7,99.8,99.6,99.7),
                 (99.8,99.95,99.7,99.9),(99.9,100.25,99.89,100.2),(100.2,100.22,100.19,100.21)])


@pytest.mark.parametrize('failure', ['stale','lost_trigger','pre_arm'])
def test_watcher_rejects_invalid_trigger(clock, cfg, monkeypatch, tmp_path, failure):
    d = trigger_frame(clock)
    c = candidate(clock)
    if failure == 'stale': d.index -= pd.Timedelta(minutes=5)
    if failure == 'lost_trigger': d.iloc[-1] = [99.9, 100., 99.8, 99.9]
    if failure == 'pre_arm': c.armed_at = clock().timestamp()-5
    monkeypatch.setattr(scanner, 'fetch_yahoo_1m', lambda *args: d)
    monkeypatch.setenv('SCANNER_DATA_DIR', str(tmp_path))
    def forbidden(*args, **kwargs): raise AssertionError('Invalid trigger reached revalidation')
    monkeypatch.setattr(scanner, '_revalidate_armed_candidate_5m', forbidden)
    scanner.watch_1m_entries(cfg, {'NVDA': c})
    assert not (tmp_path/'scanner_performance.json').exists()


def test_revalidated_candidate_replaces_old_score_and_atr(clock, cfg, monkeypatch, tmp_path):
    d = trigger_frame(clock)
    c = candidate(clock)
    fresh = {'context': {'setup_bar': '2026-09-17T11:55Z'}, 'atr': 2., 'close':100.1, 'd':d,
             'results': {'LONG': {'score':5.5,'components': {'htf_regime':1.5}}}}
    monkeypatch.setenv('SCANNER_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(scanner, 'fetch_yahoo_1m', lambda *args: d)
    monkeypatch.setattr(scanner, '_revalidate_armed_candidate_5m', lambda *args:(True,'valid',fresh))
    captured = []
    def build(current, price, config, trigger_meta):
        captured.append(copy.deepcopy(current))
        return None
    monkeypatch.setattr(scanner, 'build_signal_from_candidate', build)
    scanner.watch_1m_entries(cfg, {'NVDA': c})
    assert len(captured) == 1
    assert captured[0].score == 5.5 and captured[0].atr_value == 2.
    assert captured[0].context['score_components']['htf_regime'] == 1.5
