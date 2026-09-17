"""Read-only deployment preflight. No credentials, Telegram messages or orders."""
from pathlib import Path
import tempfile

from news_guard import NewsGuard


def main():
    with tempfile.TemporaryDirectory(prefix='scanner-feed-check-') as folder:
        guard = NewsGuard({}, Path(folder) / 'events.json')
        guard.refresh()
        missing = guard.unavailable()
        for name in ('world', 'business', 'fed', 'ecb', 'boe', 'calendar'):
            print(f'{name}: {"OK" if name in guard.success else "UNAVAILABLE"}')
        print('Required coverage:', 'PAUSED: ' + ', '.join(missing) if missing else 'READY')
        return 1 if missing else 0


if __name__ == '__main__':
    raise SystemExit(main())
