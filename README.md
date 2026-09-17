# News-aware trade scanner

Run `python fast_runtime.py` (also the Docker entrypoint). Python 3.12 is the
tested runtime. `config.yaml` enables the reliable, **alert-only** lifecycle by
default. It does not submit, amend, or close broker orders, including IG demo
orders. The historical research files and legacy demo code remain separate.

## Behavior

### Tracking controls (upgrade v2)

In the existing Telegram bot, reply to a delivered alert with `/entered`
(optionally `/entered 123.45` with your actual fill price), `/skipped`, or
`/closed`. Alternatively include the full alert ID, e.g. `/entered ID 123.45`.
`/closed` means you already fully closed it yourself. These commands record
your actions; **they never submit, modify or close broker orders**.

An alert and a user-confirmed open position have separate lifecycles. News
withdrawals, target outcomes, invalidations and the three-hour alert expiry do
not close a marked-open position. Its available-price stop checks and news
warnings continue until `/closed`, subject to the remaining quota and provider
availability. Actual fill size, partial closes, broker stops and account P&L
are not reconciled. Existing broker trades and legacy alerts are not imported.
Unmarked alerts still receive normal hypothetical monitoring.

`/status` shows worker health, event coverage and marked-open positions.
`/report` shows completed eligible reference-price outcomes, unscored states,
results by setup, and cumulative closed-outcome drawdown. It subtracts recorded
spread estimates and a configurable **assumed** 0.10R slippage sensitivity;
this is not measured execution slippage or an actual-account equity curve.
Excluding withdrawals and uncertain paths can bias the scored sample. No edge
or win rate is claimed before there are eligible outcomes.

Commands are accepted only from the configured Telegram chat and its matching
private-chat user. Group chats additionally require explicitly configured
`TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric sender IDs). First enablement
baselines the old update queue rather than executing historical commands.
Subsequent offsets, tracking changes and acknowledgements are committed together
and survive restarts. Commands older than 15 minutes are rejected. Existing
webhooks are left untouched: a conflict pauses new alerts and produces a worker
warning rather than deleting somebody else's integration. `/help` lists formats.

Fresh available closes in the entry minute can now generate stop warnings.
Historical extremes in that partially elapsed candle still cannot manufacture
a win/loss, a crossing time or a broker fill.

- A-tier and filtered B+ alerts are enabled with the user's approval through
  `reliability.allow_fast: true`. B+ Telegram alerts are labelled **SPECULATIVE**:
  score at least 6.5, immediate 1m/5m confirmation, minimum structure/trend/momentum
  checks and at most 0.15R estimated spread. Setting this flag to false restores
  A-tier-only alerts. Cash-index levels are blocked in both tiers because
  their Trading 212 CFD price basis has not been verified.
- Closed, correctly bucketed 5m/15m/1h candles; incomplete aggregates and gaps
  cannot contribute an apparent higher-timeframe trend. Entries must retain
  their breakout. The 1m trigger must be fresh and post-arm, and uses the
  revalidated score and risk inputs.
- News and high-impact calendar checks before generating **and delivering** an
  entry. The delivery worker checks a fresh 1m price against the entry range.
  Pending entry messages expire after 45 seconds; the displayed new-entry
  deadline is two minutes after setup creation. Never chase a later price.
- One active alert per instrument, a 30-minute cooldown, three active alerts
  overall, two per market type, and a 2% aggregate estimated risk cap. Estimates
  respect the configured notional cap and include the worst entry-range edge
  plus estimated spread. Actual CFD sizing and stop risk require the broker
  ticket, contract units, currency conversion, and live bid/ask.
- A-tier targets are 0.75R / 1.50R, 70% / 30%. Optional FAST targets are 0.50R /
  1.00R, also 70% / 30%. **The original stop remains for the runner**. The exact
  prices and fractions displayed in Telegram are also stored and monitored.
  No silent break-even move or old 1.5R/2.5R tracking mismatch.
- Independent price/news workers withdraw a setup for a confirmed opposing 5m
  thesis, a new opposing 1m shock, two closed 1m candles losing the trigger,
  an observed stop breach, or relevant event risk. Updates reference the
  original alert ID and detection time. News risk is a withdrawal of the old
  entry, not an assertion that a filled trade necessarily lost money.
- An unavailable event feed pauses new entries. Stale/missing prices, quota
  exhaustion, and worker failures create explicit monitoring warnings. A dead
  monitoring/delivery worker cannot silently authorize new entries.

## Event sources and response times

World and business reporting comes from the Guardian's
[world RSS](https://www.theguardian.com/world/rss) and
[business RSS](https://www.theguardian.com/business/rss), with independent
[BBC world](https://feeds.bbci.co.uk/news/world/rss.xml) and
[BBC business](https://feeds.bbci.co.uk/news/business/rss.xml) feeds. At least one
fresh feed in each category must work. Individual outages generate reduced-coverage
warnings, even if the other publisher provides coverage. Additional public
feeds cover [Fed monetary policy](https://www.federalreserve.gov/feeds/feeds.htm),
[ECB announcements](https://www.ecb.europa.eu/rss/press.html), and
[Bank of England news](https://www.bankofengland.co.uk/rss/news).
[Forex Factory's weekly JSON export](https://www.forexfactory.com/calendar)
provides the high-impact economic calendar. No news API key is needed.
An unavailable Fed/BoE/ECB feed blocks new entries with the corresponding
USD/GBP/EUR exposure; quiet but successfully fetched central-bank feeds are valid.

Source URLs and publication/event times are retained. Missing timestamps,
future stories, old headlines, invalid feeds, and stale calendar weeks cannot
manufacture fresh catalysts. Headline classification uses explicit currency,
company, sector, and macro/geopolitical rules. It never treats article text as
instructions or converts positive/negative sentiment directly into a trade.

| Check | Default cadence / window |
| --- | --- |
| World/business/central-bank RSS | Every 120 seconds, plus source/network delay |
| Economic calendar refresh | Hourly; scheduled events are checked every 10 seconds |
| High-impact event blackout | 30 minutes before through 45 minutes after |
| Relevant headline pause | 45 minutes after publication |
| Active 1m price monitoring | About every 60 seconds plus request duration, while quota permits |
| Main setup scan | Every five-minute time bucket, even if a prior tick overran |
| Telegram retries | Backoff, honoring `retry_after`; survive restarts |

This is **not** an instant newswire or complete earnings/filings feed. Public
RSS can lag and keyword rules can miss or overclassify events. Yahoo and Twelve
Data candle closes are not verified Trading 212 execution quotes. Static spread
estimates are not current broker spreads. No strategy score is a calibrated
probability or guarantee of profit. These changes fix data/control failures;
profitability still needs a new forward paper-trading sample and cost-adjusted
out-of-sample evaluation.

The configured Twelve Data limits are 800 credits/day and 8/minute, shared
across core scans, final entry checks, and monitoring. Each symbol costs a
credit. Credits are persisted with UTC day boundaries. Discovery and final-entry
checks reserve 30 minutes of credits per active FX/gold instrument, plus a share
of the minute budget for risk checks. Marked-open positions are fetched before
other alert scenarios. Risk checks can use the remaining budget even when new
discovery is paused, and are not cut off by the discovery session's end time.
**The free budget cannot support continuous 1m FX/gold monitoring
throughout every session.** When it is exhausted, 1m checks warn as degraded;
core scans may also pause for quota, while news checks continue. New
FX/gold entries cannot pass their final 1m check without fresh data/credits.

## Persistence and delivery

Mount Railway's existing volume at `/data`, or set `SCANNER_DATA_DIR` to a
persistent writable directory. New files are:

- `reliable_signals.sqlite3` (+ SQLite WAL/SHM): proposals, delivered alerts,
  lifecycle state, notification outbox, shared Twelve Data credits, marked
  positions, and Telegram update offset. The migration only adds tables.
- `event_cache.json`: last successful event data and feed-health timestamps.
- `reliability.lock`: single-process ownership of the alert worker.

Telegram delivery requires an API `ok` response and a message ID. Updates retry
until accepted. A timeout after sending has an uncertain outcome: the possible
alert is monitored, and a retry can repeat its **same ID**. Exactly-once delivery
cannot be guaranteed by Telegram `sendMessage`. Obsolete entry messages are
cancelled; warnings are retained. None of these paths executes a brokerage trade.

`OPEN`/`TP1_OPEN` mean an alert's **hypothetical reference-price scenario** is
being monitored, not that a user entered a position. Outcome bars must begin
at/after the delivery minute ceiling; the partly elapsed alert candle is
excluded. Same-bar stop/target ambiguity, monitoring gaps, unverified delivery,
withdrawals and expiries have no assigned final R. Complete known-path scenarios
store gross `result_r` and `result_r_net_estimate` after the spread estimate;
they are not actual fills/P&L. Partial exits are weighted and gap losses can
exceed 1R. Old `scanner_performance.json` statistics are preserved, not silently
rewritten, and their unverified alerts are not imported into the new ledger.
Existing broker positions need manual review during this migration.

### Operational health

Every 60 seconds the service logs a secret-free `SCANNER_HEALTH` JSON heartbeat.
It includes worker freshness (including the main scan loop), required-feed
failures, individual-source degradation, marked-open count and notification
retry/backlog indicators. A stopped main loop blocks new entries after seven
minutes without progress. Transport errors log exception types, never API URLs.
An independent ChatGPT hourly Railway watchdog can check these logs even if the
process is dead; this is an hourly operational backstop, not an instant pager.
Neither it nor the internal risk workers guarantee continuous market coverage.

## Install and verify

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python check_feeds.py
```

Tests prohibit live network/broker/Telegram requests and run through the actual
`fast_runtime` import chain. `check_feeds.py` separately performs public read-only
requests, reports source availability, and exits nonzero if required coverage
is unavailable. Do that check in the deployment network before relying on it.

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `TWELVE_DATA_API_KEY` in Railway
variables. Never commit their values. `IG_AUTO_TRADE` is not used by the new
alert-only runtime. Keep Railway serverless/sleep disabled and run one replica
against the existing volume. Do not apply unrelated staged Railway changes.

## Rollout and rollback

1. Run regression tests and review the PR.
2. Confirm required news/calendar endpoints are reachable from Railway and
   keep the service awake. A source failure must show a paused state.
3. Deploy the reviewed commit to the existing service, retaining `/data` and
   Telegram variables. Check startup and delivery logs and the Telegram online
   message. Begin a new paper-trading evaluation; there is no validated win
   rate for this version yet.
4. If rollout fails, redeploy the preceding known commit. Retain both the old
   JSON tracker and new SQLite ledger for diagnosis. Do not reset history.

The commit's `railway.json` runs `check_feeds.py` as a pre-deploy check. A failed
check prevents this version being activated; investigate the source/network
failure instead of removing the gate. The earlier commit has no such check.

Setting `reliability.enabled: false` returns to the legacy runtime, including
its old alert/tracker policy; that is a rollback switch, not an equally protected
mode. Use the reliable runtime for the fixes documented here.
