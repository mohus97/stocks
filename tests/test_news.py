from datetime import timedelta
import pytest
from news_guard import Event, NewsGuard, headline_relevant, parse_calendar, parse_rss


def event(clock, title, source='world', kind='headline', currencies=()):
    return Event('event1', title, source, 'https://example.com/story', clock().isoformat(), kind, currencies)


def test_calendar_timezone_and_relevance(clock, engine):
    payload = [{'title': 'BOE Official Bank Rate', 'country': 'GBP', 'impact': 'High', 'date': '2026-09-17T08:15:00-04:00'}]
    events = parse_calendar(payload, clock())
    engine.news.events = {e.id: e for e in events}
    assert engine.news.gate({'symbol': 'GBPUSD', 'type': 'forex'}) == 'BOE Official Bank Rate'
    assert engine.news.gate({'symbol': 'USDJPY', 'type': 'forex'}) is None


def test_calendar_stale_or_missing_timezone_is_invalid(clock):
    for at in ('2026-08-01T12:00:00Z', '2026-09-17T12:00:00'):
        with pytest.raises(ValueError):
            parse_calendar([{'title': 'CPI', 'country': 'USD', 'impact': 'High', 'date': at}], clock())


def test_blackout_expiry(clock, engine):
    e = event(clock, 'US CPI', kind='scheduled', currencies=('USD',))
    engine.news.events[e.id] = e
    assert engine.news.risks({'symbol': 'NVDA', 'type': 'stock'})
    clock.advance(46 * 60)
    assert not engine.news.risks({'symbol': 'NVDA', 'type': 'stock'})


def test_old_and_future_headlines_do_not_manufacture_catalysts(clock, engine):
    for delta in (-3600, 300):
        e = Event(str(delta), 'Emergency rate cut', 'world', 'https://example.com', (clock() + timedelta(seconds=delta)).isoformat(), 'headline')
        engine.news.events[e.id] = e
    assert not engine.news.risks({'symbol': 'NVDA', 'type': 'stock'})


@pytest.mark.parametrize('title,symbol,expected', [
    ('Missile strike closes Strait of Hormuz', 'NVDA', True),
    ('New chip export restrictions announced', 'AMD', True),
    ('Nvidia earnings guidance cut', 'NVDA', True),
    ('Nvidia earnings guidance cut', 'TSLA', False),
    ('Football player injures arm', 'ARM', False),
    ('Company changes marketing strategy', 'MSTR', False),
    ('Fed announces interest rate decision', 'NVDA', True),
    ('BOE cuts interest rates', 'NVDA', False),
    ('Israel attacks Iran', 'NVDA', True),
])
def test_headline_mapping(clock, title, symbol, expected):
    names = {'NVDA': 'NVIDIA', 'AMD': 'AMD', 'TSLA': 'Tesla', 'ARM': 'Arm', 'MSTR': 'Strategy'}
    assert headline_relevant(event(clock, title), {'symbol': symbol, 'name': names[symbol], 'type': 'stock'}) is expected


def test_rss_requires_original_timestamp():
    rss = b'<rss><channel><item><title>News</title><link>https://example.com</link></item></channel></rss>'
    with pytest.raises(ValueError):
        parse_rss(rss, 'world', 'https://example.com/rss')
    rss = rss.replace(b'</title>', b'</title><pubDate>Thu, 17 Sep 2026 12:00:00 GMT</pubDate>')
    assert len(parse_rss(rss, 'world', 'https://example.com/rss')) == 1


def test_html_or_entity_document_is_not_news():
    for body in (b'<html>Access denied</html>', b'<!DOCTYPE rss><rss/>'):
        with pytest.raises(ValueError):
            parse_rss(body, 'world', 'https://example.com')


def test_missing_any_required_feed_pauses_entries(clock, engine):
    engine.news.success.pop('calendar')
    assert 'calendar' in engine.news.gate({'symbol': 'NVDA', 'type': 'stock'})
    engine.news.success['calendar'] = clock().isoformat()
    clock.advance(601)
    assert 'world' in engine.news.gate({'symbol': 'NVDA', 'type': 'stock'})


def test_feed_failure_preserves_known_event_and_last_success(tmp_path, cfg, clock):
    def fail(*args, **kwargs):
        raise TimeoutError()
    news = NewsGuard(cfg['news'], tmp_path/'cache.json', get=fail, clock=clock)
    e = event(clock, 'Emergency rate cut')
    news.events[e.id] = e
    news.success = {'world': clock().isoformat()}
    news.refresh()
    restored = NewsGuard(cfg['news'], tmp_path/'cache.json', get=fail, clock=clock)
    assert restored.events[e.id] == e
    assert restored.success['world'] == clock().isoformat()
    assert 'calendar' in restored.unavailable()


def test_week_rollover_requires_current_calendar(engine, clock):
    from datetime import datetime, timezone
    clock.now = datetime(2026, 9, 20, 4, 15, tzinfo=timezone.utc)  # Sunday in New York.
    engine.news.success = {s:clock().isoformat() for s in ('world','business')}
    engine.news.success['calendar'] = '2026-09-20T03:45:00+00:00'  # Saturday.
    assert engine.news.unavailable() == ['calendar']


@pytest.mark.parametrize('item', [
    {'symbol': 'NVDA', 'type': 'stock', 'name': 'NVIDIA'},
    {'symbol': 'EURUSD', 'type': 'forex'},
    {'symbol': 'XAUUSD', 'type': 'metal'},
])
@pytest.mark.parametrize('title,blocked', [
    ('Early voting begins in midterms as campaign shifts focus to cost of living and Iran war – US politics live', False),
    ('Voters debate the cost of the Ukraine war', False),
    ('Iran launches attacks on shipping', True),
    ('China imposes blockade on Taiwan', True),
    ('Iran declares war', True),
    ('War escalates in Ukraine', True),
])
def test_background_war_mentions_vs_conflict_developments(engine, clock, item, title, blocked):
    e = event(clock, title)
    engine.news.events = {e.id: e}
    assert bool(engine.news.gate(item)) is blocked
