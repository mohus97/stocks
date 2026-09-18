"""Source-linked event risk, not a headline-to-buy/sell sentiment model.

Only the configured publishers and calendar are read. Feed text is data, never
instructions. Unknown/missing timestamps cannot manufacture a fresh catalyst.
"""
from dataclasses import asdict, dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

LOG = logging.getLogger(__name__)
UTC = timezone.utc
DEFAULT_FEEDS = (
    ('world', 'https://www.theguardian.com/world/rss'),
    ('business', 'https://www.theguardian.com/business/rss'),
    ('bbc_world', 'https://feeds.bbci.co.uk/news/world/rss.xml'),
    ('bbc_business', 'https://feeds.bbci.co.uk/news/business/rss.xml'),
    ('fed', 'https://www.federalreserve.gov/feeds/press_monetary.xml'),
    ('ecb', 'https://www.ecb.europa.eu/rss/press.html'),
    ('boe', 'https://www.bankofengland.co.uk/rss/news'),
)
CALENDAR_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'


def parse_time(value):
    if not value:
        raise ValueError('missing timestamp')
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        parsed = parsedate_to_datetime(str(value))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires a timezone')
    return parsed.astimezone(UTC)


def digest(*values):
    return hashlib.sha256('|'.join(str(v) for v in values).encode()).hexdigest()[:20]


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    source: str
    url: str
    at: str
    kind: str
    currencies: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, 'currencies', tuple(self.currencies))


def parse_rss(body, source, url):
    if len(body) > 2_000_000 or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('unsupported RSS document')
    root = ET.fromstring(body)
    if root.tag not in {'rss', '{http://www.w3.org/2005/Atom}feed', '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF'}:
        raise ValueError('not a news feed')
    events = []
    for item in root.iter():
        if item.tag.split('}')[-1] not in {'item', 'entry'}:
            continue
        fields = {c.tag.split('}')[-1]: c for c in item}
        def value(name):
            node = fields.get(name)
            return ''.join(node.itertext()).strip() if node is not None else ''
        title = re.sub(r'\s+', ' ', value('title')).strip()[:500]
        try:
            at = parse_time(value('pubDate') or value('published') or value('date') or value('updated'))
        except (ValueError, TypeError, OverflowError):
            continue
        link = value('link')
        if not link and fields.get('link') is not None:
            link = fields['link'].get('href', '')
        if not title or urlparse(link).scheme not in {'https', 'http'}:
            continue
        identity = value('guid') or value('id') or link
        events.append(Event(digest(source, identity, title), title, source, link, at.isoformat(), 'headline'))
    if not events:
        raise ValueError('feed has no timestamped headlines')
    return events


def parse_calendar(payload, now):
    if not isinstance(payload, list) or not payload:
        raise ValueError('empty or invalid calendar')
    dates, events = [], []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError('invalid calendar row')
        # Invalid high-impact times must not silently remove the blackout.
        at = parse_time(row.get('date'))
        dates.append(at)
        if str(row.get('impact', '')).lower() != 'high':
            continue
        currency = str(row.get('country', '')).upper()
        if not re.fullmatch('[A-Z]{3}', currency) or not row.get('title'):
            raise ValueError('invalid high-impact event')
        title = str(row['title'])[:300]
        events.append(Event(digest(title, currency, at.isoformat()), title,
                            'Forex Factory', 'https://www.forexfactory.com/calendar',
                            at.isoformat(), 'scheduled', (currency,)))
    local = now.astimezone(ZoneInfo('America/New_York'))
    start = (local - timedelta(days=(local.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    if not any(start <= d < end for d in dates):
        raise ValueError('calendar is not for the current week')
    return events


def instrument_currencies(item):
    symbol = re.sub('[^A-Z]', '', item.get('data_symbol', item['symbol']).upper())
    if item.get('type') == 'forex' and len(symbol) == 6:
        return {symbol[:3], symbol[3:]}
    return {'USD'} if item.get('type') in {'stock', 'metal'} else {'USD', 'GBP', 'EUR', 'JPY'}


def headline_relevant(event, item):
    title = event.title.lower()
    currencies = instrument_currencies(item)
    if re.search(r'\b(invasion|invades|air ?strikes?|missile strikes?|nuclear attack|strait of hormuz|'
                 r'banking crisis|emergency rate|oil embargo|trade war|ceasefire|cease-fire|tariffs?)\b', title):
        return True
    # A country plus a background mention of "war" is not a fresh shock.
    # Keep concrete conflict actions and explicit outbreak/escalation language.
    if re.search(r'\b(iran|israel|russia|ukraine|china|taiwan)\b', title) and re.search(
            r'\b(attacks?|strikes?|bombing|bombardment|blockade|invasion|'
            r'(?:declares?|declared|declaration of) war|war (?:begins|erupts|breaks out|escalates|widens|spreads))\b', title):
        return True
    economic = re.search(r'\b(interest rates?|rate (cut|rise|hike|decision)|inflation|cpi|payrolls?|'
                         r'jobs report|monetary policy|fomc|economic recession)\b', title)
    if economic:
        if event.source == 'fed' or re.search(r'\b(fed|federal reserve|us|u\.s\.|america)\b', title):
            return 'USD' in currencies
        if event.source == 'boe' or re.search(r'\b(boe|bank of england|uk|britain|british)\b', title):
            return 'GBP' in currencies
        if event.source == 'ecb' or re.search(r'\b(ecb|eurozone|euro area)\b', title):
            return 'EUR' in currencies
        if re.search(r'\b(japan|boj|bank of japan)\b', title):
            return 'JPY' in currencies
        return True  # Unattributed macro surprise: conservative temporary pause.
    if event.source == 'fed':
        return 'USD' in currencies  # This feed is specifically monetary policy.
    if item.get('type') != 'stock':
        return False
    symbol = item['symbol'].upper()
    aliases = {
        'ARM': ['arm holdings', 'arm shares', 'arm stock'],
        'MSTR': ['microstrategy', 'strategy shares', 'strategy stock'],
        'META': ['meta', 'facebook'], 'GOOGL': ['alphabet', 'google'],
        'SMCI': ['super micro', 'supermicro'], 'COIN': ['coinbase'],
    }.get(symbol, [item.get('name', symbol), symbol])
    if any(re.search(r'(?<!\w)' + re.escape(a.lower()) + r'(?!\w)', title) for a in aliases):
        return True
    if symbol in {'NVDA', 'AMD', 'AVGO', 'ARM', 'MU', 'SMCI'}:
        return bool(re.search(r'\b(chip|semiconductor)s?\b.*\b(ban|export|restriction|sanction)s?\b|'
                              r'\b(ban|export|restriction|sanction)s?\b.*\b(chip|semiconductor)s?\b', title))
    if symbol in {'COIN', 'MSTR'} and re.search(r'\b(bitcoin|crypto|cryptocurrency)\b', title):
        return True
    return False


class NewsGuard:
    def __init__(self, config, cache_path, get=requests.get, clock=None):
        self.cfg = config
        self.path = Path(cache_path)
        self.get = get
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = threading.RLock()
        self.events = {}
        self.success = {}
        self.attempts = {}
        self.feeds = config.get('feeds', DEFAULT_FEEDS)
        try:
            cached = json.loads(self.path.read_text())
            self.events = {e['id']: Event(**e) for e in cached['events']}
            self.success = cached['success']
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def refresh(self):
        now = self.clock()
        sources = list(self.feeds) + [('calendar', self.cfg.get('calendar_url', CALENDAR_URL))]
        due = []
        for name, url in sources:
            interval = self.cfg.get('calendar_poll_seconds', 3600) if name == 'calendar' else self.cfg.get('poll_seconds', 120)
            with self.lock:
                if now.timestamp() - self.attempts.get(name, 0) < interval:
                    continue
                self.attempts[name] = now.timestamp()
            due.append((name, url))
        def fetch(source):
            name, url = source
            response = self.get(url, timeout=(3, 8), headers={'User-Agent': 'MarketScanner/2.0 (RSS event risk monitor)'})
            response.raise_for_status()
            events = parse_calendar(response.json(), now) if name == 'calendar' else parse_rss(response.content, name, url)
            if name in {'world', 'business', 'bbc_world', 'bbc_business'} and not any(
                    timedelta(minutes=-2) <= now - parse_time(e.at) <= timedelta(hours=12) for e in events):
                raise ValueError('news feed publication times are stale')
            return events
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [(name, pool.submit(fetch, (name, url))) for name, url in due]
            for name, result in results:
                self._accept_result(name, result, now)
        with self.lock:
            self.events = {k: e for k, e in self.events.items()
                           if parse_time(e.at) >= now - timedelta(hours=2)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'events': [asdict(e) for e in self.events.values()], 'success': self.success}))
            tmp.replace(self.path)

    def _accept_result(self, name, result, now):
        try:
            events = result.result()
            with self.lock:
                if name == 'calendar':
                    self.events = {k: e for k, e in self.events.items() if e.kind != 'scheduled'}
                for event in events:
                    at = parse_time(event.at)
                    if event.kind == 'scheduled' or timedelta(minutes=-2) <= now - at <= timedelta(hours=2):
                        self.events[event.id] = event
                self.success[name] = now.isoformat()
        except Exception as exc:
            LOG.warning('Event source %s unavailable (%s)', name, type(exc).__name__)
            # Retry failed feeds sooner without hammering a public endpoint.
            interval = self.cfg.get('calendar_poll_seconds', 3600) if name == 'calendar' else self.cfg.get('poll_seconds', 120)
            backoff = 300 if name == 'calendar' else 60
            self.attempts[name] = now.timestamp() - max(0, interval - backoff)

    def degraded_sources(self, now=None):
        """Connectivity freshness, not the age of the last central-bank story."""
        now = now or self.clock()
        with self.lock:
            missing = []
            for source in [name for name, _ in self.feeds] + ['calendar']:
                maximum = self.cfg.get('calendar_max_age_seconds', 7200) if source == 'calendar' else self.cfg.get('max_age_seconds', 600)
                last = self.success.get(source)
                if not last or not 0 <= (now - parse_time(last)).total_seconds() <= maximum:
                    missing.append(source)
            return missing

    def unavailable(self, now=None):
        now = now or self.clock()
        missing = []
        with self.lock:
            # A quiet central bank feed is normal. General coverage + calendar
            # must both be working; a single surviving feed is not full coverage.
            degraded = self.degraded_sources(now)
            for source in ('world', 'business', 'calendar'):
                maximum = self.cfg.get('calendar_max_age_seconds', 7200) if source == 'calendar' else self.cfg.get('max_age_seconds', 600)
                last = self.success.get(source)
                if source in {'world', 'business'} and source in degraded and 'bbc_' + source not in degraded and 'bbc_' + source in dict(self.feeds):
                    continue  # Independent coverage of the same category.
                if not last or not 0 <= (now - parse_time(last)).total_seconds() <= maximum:
                    missing.append(source)
                elif source == 'calendar':
                    # Never reuse last week's cache across the weekly rollover.
                    def week_start(value):
                        local = value.astimezone(ZoneInfo('America/New_York'))
                        return (local - timedelta(days=(local.weekday() + 1) % 7)).date()
                    if week_start(parse_time(last)) != week_start(now):
                        missing.append(source)
        return missing

    def risks(self, item, now=None):
        now = now or self.clock()
        found = []
        currencies = instrument_currencies(item)
        with self.lock:
            for event in self.events.values():
                delta = (now - parse_time(event.at)).total_seconds() / 60
                if event.kind == 'scheduled':
                    if currencies.intersection(event.currencies) and -self.cfg.get('before_minutes', 30) <= delta <= self.cfg.get('after_minutes', 45):
                        found.append(event)
                elif 0 <= delta <= self.cfg.get('headline_pause_minutes', 45) and headline_relevant(event, item):
                    found.append(event)
        return sorted(found, key=lambda e: e.at, reverse=True)

    def gate(self, item, now=None):
        unavailable = self.unavailable(now)
        if unavailable:
            return 'event coverage unavailable: ' + ', '.join(unavailable)
        currencies = instrument_currencies(item)
        missing = self.degraded_sources(now)
        relevant_missing = [s for s, c in (('fed', 'USD'), ('boe', 'GBP'), ('ecb', 'EUR'))
                            if s in missing and c in currencies]
        if relevant_missing:
            return 'central-bank coverage unavailable: ' + ', '.join(relevant_missing)
        risks = self.risks(item, now)
        if risks:
            return risks[0].title
        return None
