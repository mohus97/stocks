from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast_runtime  # Test the Docker entrypoint's complete policy import chain.
import scanner
import market_data
from news_guard import NewsGuard
from reliability import ReliableScanner


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 17, 12, 0, 15, tzinfo=timezone.utc)
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Tests must not use live providers, Telegram, or broker APIs')
    monkeypatch.setattr(requests.sessions.Session, 'request', deny)
    monkeypatch.setattr(scanner.yf, 'download', deny)
    monkeypatch.setattr(scanner, 'RELIABILITY', None)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(market_data, 'utc_now', lambda: pd.Timestamp(value()))
    monkeypatch.setattr(scanner.time, 'time', lambda: value().timestamp())
    return value


@pytest.fixture
def cfg():
    return deepcopy(scanner.load_config())


@pytest.fixture
def engine(tmp_path, cfg, clock, monkeypatch):
    news = NewsGuard(cfg['news'], tmp_path / 'news.json', clock=clock)
    news.success = {s: clock().isoformat() for s in ('world', 'business', 'calendar')}
    sent = []
    def notify(text):
        sent.append(text)
        return len(sent)
    value = ReliableScanner(cfg, tmp_path, clock=clock, news=news, notify=notify)
    value.sent = sent
    monkeypatch.setattr(value, '_pending_is_valid', lambda rec: True)
    monkeypatch.setenv('SCANNER_DATA_DIR', str(tmp_path))
    yield value
    value.db.close()


@pytest.fixture
def signal(clock):
    return scanner.Signal('NVDA', 'NVIDIA', 'stock', 'LONG', 100., 98., 103., 105.,
        99.8, 100.2, 7., 'confirmed setup', 10.02, 501., clock().isoformat(),
        {'quality_tier': 'A-TIER', 'setup_bar': '2026-09-17T11:55:00+00:00',
         'trigger_level': 99.5, 'atr_value': 1.0, 'trigger_mode': '5m_breakout'})


def frame(start, rows, freq='min'):
    """Rows are explicit O/H/L/C tuples."""
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'],
                        index=pd.date_range(start, periods=len(rows), freq=freq, tz='UTC'))


def publish(engine, signal):
    engine.publish(signal, {'symbol': signal.symbol, 'name': signal.label, 'type': signal.market_type})
    engine.deliver_once()
    return engine.records()[0]
