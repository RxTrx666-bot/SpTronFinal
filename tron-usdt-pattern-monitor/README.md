# TRON USDT TRC-20 Test-Transfer Pattern Monitor

A 24/7, **read-only** TRON blockchain monitor that learns, **independently for every
Sender → Recipient relationship**, when a smaller *test* transfer is repeatedly
followed by a much larger transfer. Once a relationship has shown this pattern often
enough, it goes onto an **automatic watchlist**. If that sender later repeats its
learned test transfer to the same recipient, you get a 🔴 **RED Telegram alert
straight away, before the large transfer happens**.

- It monitors **USDT TRC-20 only** (contract events of the USDT contract). It ignores TRX, TRC-10 tokens and every other TRC-20 token.
- There is **no global test amount**. One relationship may test with 5 USDT, another with 250 USDT, another with 1,000 USDT. Each one's range is learned from its own history.
- You never add wallets by hand. The watchlist is built from on-chain behaviour.
- It is **read-only**. It never signs, sends or moves funds, and it never touches private keys, seed phrases or wallets. The only TRON calls are HTTP reads: the contract events feed, plus a constant `symbol()`/`decimals()` call that checks the configured contract.

> ⚠️ **About the contract address.** The official Tether USDT TRC-20 contract on TRON
> mainnet is **`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`**, and that is the default.
> The original specification mentioned `TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7`, which is
> *not* Tether's contract. You can still set it through `USDT_CONTRACT_ADDRESS` if you
> really mean to. The monitor logs a warning at startup whenever the configured
> contract is not the official one.

---

## Contents
1. [How it works](#how-it-works)
2. [Requirements](#1-requirements)
3. [VPS setup](#2-vps-setup)
4. [Docker installation](#3-docker-installation)
5. [Project installation](#4-project-installation)
6. [PostgreSQL setup](#5-postgresql-setup)
7. [Creating `.env`](#6-creating-env)
8. [TRON API credentials](#7-tron-api-credentials)
9. [Creating the Telegram bot](#8-creating-the-telegram-bot)
10. [Configuring the Telegram chat ID](#9-configuring-the-telegram-chat-id)
11. [Starting](#10-starting-the-application) · [Stopping](#11-stopping) · [Restarting](#12-restarting) · [Logs](#13-viewing-logs) · [Status](#14-checking-status)
12. [Simulation mode](#15-simulation-mode)
13. [Automated tests](#16-automated-tests)
14. [Troubleshooting](#17-troubleshooting)
15. [Updating](#18-updating-the-application)
16. [Configuration reference](#configuration-reference) · [Sizing](#sizing--performance) · [Project layout](#project-layout)

---

## How it works

```
TRON BLOCKCHAIN (TronGrid /v1/contracts/<USDT>/events)
        ↓   one centralised collector: confirmed stream + unconfirmed fast path + backfill
USDT EVENT DECODER        – only USDT `Transfer` events; hex → base58; exact integer amounts
        ↓
DEDUPLICATION             – in-memory LRU + UNIQUE(transaction_hash, event_index) in PostgreSQL
        ↓
TRANSACTION PROCESSOR     – stores every transfer; UNCONFIRMED → CONFIRMED upgrades
        ↓
SENDER → RECIPIENT INDEX  – wallet_pairs statistics (count, volume, min/max/avg/median)
        ↓
PATTERN ENGINE            – per-pair TEST → LARGE sequence detection + learned model + confidence
        ↓
AUTOMATIC WATCHLIST       – CANDIDATE / ACTIVE / PAUSED / WEAKENED / EXPIRED
        ↓
WATCHLIST MATCH ENGINE    – in-memory lookup per transfer; learned test band; pending follow-ups
        ↓
TELEGRAM ALERT ENGINE     – durable outbox, priority queue, retries, latency measurement
```

### What counts as a pattern (per relationship)

1. **Sequence detection.** For one Sender → Recipient pair, a transfer *L* is paired
   with the most recent unused earlier transfer *T* of the **same pair** when
   `L ≥ MIN_LARGE_TO_TEST_RATIO × T` and `L` arrives within `MAX_FOLLOWUP_HOURS` of `T`.
   A transfer used as a LARGE can never later be used as a TEST.
   `MIN_LARGE_TO_TEST_RATIO` only decides whether one transfer is substantially larger
   than another. It is **not** a test-amount limit.
2. **Learning the test behaviour.** The test amounts of the pair's sequences are
   clustered in log space around the pair's own centre (`TEST_CLUSTER_BAND_FACTOR`,
   default 3×), with recent sequences weighted more (half-life
   `PATTERN_HALF_LIFE_DAYS`). The result is the pair's learned range (for example
   5–10, 200–300 or 950–1,100 USDT), plus typical and median values, the large range,
   the typical ratio and the typical follow-up time.
3. **Confidence** in [0, 1] combines eight signals: sequence count, test-amount
   consistency, large-amount consistency, test/large separation (ratio), timing
   consistency and promptness, **success rate** (how often a test-like transfer really
   was followed by a large one), relationship consistency (how much of the pair's
   activity the pattern explains) and recency. The score maps to LOW, MEDIUM or HIGH.
4. **ACTIVE watchlist** needs all of the following:
   - at least `MIN_SUCCESSFUL_SEQUENCES` consistent sequences
   - confidence of at least `MIN_PATTERN_CONFIDENCE`
   - success rate of at least `MIN_SUCCESS_RATE`
   - a consistent test cluster
   - recent activity (within `PATTERN_EXPIRY_DAYS`)

   Two consistent sequences make the relationship a CANDIDATE (🟡 alert).
5. **Matching.** For a transfer of an ACTIVE pair, the amount is compared with that
   pair's band: `[learned min ÷ TEST_MATCH_FACTOR, learned max × TEST_MATCH_FACTOR]`,
   capped at `smallest learned large ÷ MIN_LARGE_TO_TEST_RATIO`. A match creates a
   `test_events` row and a 🔴 alert immediately.
6. **Follow-up.** For the pair's learned window (derived from its historical
   follow-up times, capped by `MAX_FOLLOWUP_HOURS`), any transfer of the same pair that
   is at least `MIN_LARGE_TO_TEST_RATIO` × the test amount triggers 🚨 **LARGE
   FOLLOW-UP**. The pair is then re-analysed, so the sequence count goes from 3 to 4
   and every statistic is recalculated. Tests whose window runs out with no large
   transfer are marked EXPIRED and lower the success rate.

### Dust and false-positive protection
- **No transfer is a test because it is small.** A test is only recognised on a
  watchlisted relationship, and only after repeated, consistent and successful history.
- Dust from address poisoning (for example 0.000001 USDT) never forms sequences
  (`DUST_FLOOR_USDT` = 0.01). The pair that sends dust also never sends the large
  transfer, so its success rate is zero.
- Random small transfers, one-off sequences and irregular business flows fail the
  consistency and success-rate gates. In the test suite, fewer than 1% of random-amount
  relationships qualify.
- Flood guard: if one relationship produces more than `FLOOD_MAX_TESTS_PER_HOUR`
  matching tests in an hour, it is automatically PAUSED for `FLOOD_PAUSE_MINUTES`.

### Pattern decay
Older sequences count for less (exponential half-life). When behaviour changes, for
example from 5 → 20K to 5,000 → 100K, the dominant cluster moves to the new behaviour
and the old 5 USDT test stops matching. A relationship whose evidence weakens becomes
**WEAKENED**. Alerts stay off unless `ALERT_ON_WEAKENED=true`. With no sequence for
`PATTERN_EXPIRY_DAYS` it becomes **EXPIRED**. Historical data is never deleted when the
state changes.

### Speed, unconfirmed transfers and latency
- The confirmed stream is polled every `POLL_INTERVAL_SECONDS` (default 1 s) from a
  persisted cursor.
- With `ENABLE_UNCONFIRMED=true`, not-yet-solidified events are also polled. A known
  test can therefore trigger an alert about one minute before it is solidified. The
  row is stored as `UNCONFIRMED` and upgraded to `CONFIRMED` later **without a second
  alert**, unless you set `SEND_CONFIRMATION_ALERTS=true`. An unconfirmed transfer that
  never confirms is marked `DROPPED`.
- Only confirmed transfers are used for learning.
- Every alert records the following timestamps:
  - `blockchain_event_time`
  - `blockchain_detection_time`
  - `processing_start_time` and `processing_end_time`
  - `telegram_send_start_time` and `telegram_send_end_time`
  - `total_detection_latency_ms`

  Each value is logged and summarised in `/stats` as p50/p95. Detection is **not**
  instantaneous. It is bounded by block time (about 3 s), TronGrid indexing, the poll
  interval and Telegram delivery. Measure it on your VPS.

### Reliability
- **Duplicates.** `UNIQUE(transaction_hash, event_index)` on `transactions` stops
  duplicate rows. `test_events`, `followup_events` and `alerts.dedup_key` are also
  unique, and every insert uses `INSERT … ON CONFLICT DO NOTHING`. As a result a
  transfer produces at most one test alert and one follow-up alert. This holds across
  API retries, duplicate deliveries, crashes and restarts. A single TRON transaction
  can emit several USDT `Transfer` events, which is why `event_index` is part of the
  key. For ordinary transfers the hash alone is unique.
- **Restart.** The confirmed cursor and the backfill progress are stored in
  `collector_state`. A transfer that was stored but not yet matched keeps
  `processed = false` and is re-matched on restart. Analysis work is kept in
  `wallet_pairs.needs_analysis`. Alerts are stored in an outbox table and re-sent until
  they are delivered.
- **Failures.** API timeouts, 5xx errors and 429s are retried with exponential backoff
  and jitter, and `Retry-After` is respected. The cursor never advances when the API
  or the database fails. One malformed event is skipped and counted without stopping
  the batch. Telegram failures are retried with backoff and `retry_after`. Crashed
  background tasks are restarted by a supervisor. Docker and systemd restart the
  process. SIGTERM triggers a graceful shutdown.
- **Delivery.** Alerts are delivered at least once. Telegram calls never block
  blockchain processing.

---

## 1. Requirements
- Ubuntu 22.04/24.04 (or any Linux) VPS: **2 vCPU, 4 GB RAM minimum**. Disk: see
  [Sizing](#sizing--performance). Start with 100 GB SSD for 30 days of history.
- Docker Engine 24+ with the Compose plugin (recommended), **or** Python 3.11+ with
  PostgreSQL 14+.
- A TronGrid API key (free) and a Telegram bot.

## 2. VPS setup
```bash
ssh root@YOUR_VPS_IP
apt update && apt -y upgrade
apt -y install git curl ca-certificates ufw
adduser --disabled-password --gecos "" tronmon
ufw allow OpenSSH && ufw --force enable      # the monitor needs no inbound ports
timedatectl set-timezone UTC
```

## 3. Docker installation
```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker --version && docker compose version
```

## 4. Project installation
```bash
mkdir -p /opt && cd /opt
git clone https://github.com/RxTrx666-bot/SpTronFinal.git
cp -r SpTronFinal/tron-usdt-pattern-monitor /opt/tron-usdt-pattern-monitor
cd /opt/tron-usdt-pattern-monitor
```

## 5. PostgreSQL setup
**Docker (recommended).** PostgreSQL 16 is part of `docker-compose.yml` and stores its
data in the named volume `pgdata`, which survives restarts and upgrades. You only set
`POSTGRES_PASSWORD` in `.env`. The schema is created automatically on first start
(Alembic migrations, `AUTO_MIGRATE=true`).

**Native PostgreSQL** (for the systemd deployment):
```bash
apt -y install postgresql
sudo -u postgres psql -c "CREATE USER tron WITH PASSWORD 'YOUR_LONG_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE tron_usdt OWNER tron;"
# schema: applied automatically at startup, or explicitly:
.venv/bin/python -m app.main migrate
# (alternative: psql -U tron -h localhost tron_usdt -f migrations/001_initial_schema.sql)
```

## 6. Creating `.env`
```bash
cp .env.example .env
chmod 600 .env
nano .env
```
Fill in at least: `POSTGRES_PASSWORD` (letters and digits, long),
`DATABASE_URL` (native install only), `TRON_API_KEY`, `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`. Never commit `.env`.

## 7. TRON API credentials
1. Go to <https://www.trongrid.io> and sign up.
2. Dashboard → **Create API Key** → copy it.
3. In `.env`, set `TRON_API_URL=https://api.trongrid.io` and `TRON_API_KEY=<your key>`.

Without a key TronGrid rate-limits requests heavily and the backfill will be very
slow. For 30+ days of history, consider a paid TronGrid plan or your own
TronGrid-compatible event server, and point `TRON_API_URL` at it.

## 8. Creating the Telegram bot
1. In Telegram, open **@BotFather** → `/newbot` → pick a name and a username.
2. Copy the token (`123456789:AA…`) into `TELEGRAM_BOT_TOKEN`.

## 9. Configuring the Telegram chat ID
- **Private chat.** Send any message to your new bot, then run:
  ```bash
  curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
  ```
- **Group.** Add the bot to the group and send a message there. Group IDs are
  negative, for example `-1001234567890`.
- Put the number in `TELEGRAM_CHAT_ID`. For several recipients, separate the IDs with
  commas, for example `TELEGRAM_CHAT_ID=8020903132,8972433273`. Every alert goes to each
  chat, and each of them can use the commands. Every person must open the bot and press
  **Start** once, or Telegram won't let the bot message them.

The bot answers commands **only** in the chats listed there. Commands:
- `/start`, `/help`
- `/status`
- `/watchlist [page]`
- `/stats`
- `/pattern <sender> <recipient>`
- `/pause <sender> <recipient>`, `/resume <sender> <recipient>`

There is deliberately no command for adding wallets.

Check the whole configuration (database, TRON API, token contract, Telegram test
message):
```bash
docker compose run --rm monitor python -m app.main check
```

## 10. Starting the application
**Docker:**
```bash
cd /opt/tron-usdt-pattern-monitor
docker compose up -d --build
```
**systemd + Docker** (starts at boot):
```bash
cp systemd/tron-usdt-monitor-docker.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tron-usdt-monitor-docker
```
**Native systemd (virtualenv):**
```bash
apt -y install python3-venv
cd /opt/tron-usdt-pattern-monitor
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
chown -R tronmon:tronmon /opt/tron-usdt-pattern-monitor
cp systemd/tron-usdt-monitor.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tron-usdt-monitor
```
The unit uses `Restart=always` and `After=network-online.target`, and starts at boot.

On first start the monitor does the following:
1. Begins live monitoring immediately.
2. Backfills `INITIAL_HISTORY_DAYS` in the background.
3. Runs pattern discovery over the history.
4. Sends one "📚 HISTORICAL BACKFILL COMPLETE" summary with the initial watchlist.
   Individual per-pattern alerts for history are suppressed unless
   `BACKFILL_NOTIFY_INDIVIDUAL=true`.

## 11. Stopping
```bash
docker compose stop                    # Docker
systemctl stop tron-usdt-monitor       # native
```

## 12. Restarting
```bash
docker compose restart monitor         # Docker
systemctl restart tron-usdt-monitor    # native
```
The monitor resumes from its saved cursor, so no transfers are lost.

## 13. Viewing logs
```bash
docker compose logs -f --tail=200 monitor
journalctl -u tron-usdt-monitor -f          # native
docker compose logs monitor | grep -E "Known watchlist test|Large follow-up|Telegram alert sent"
```
Set `LOG_FORMAT=json` to get one JSON object per line (for Loki, ELK and similar).

## 14. Checking status
```bash
docker compose ps                      # container health (heartbeat-based HEALTHCHECK)
systemctl status tron-usdt-monitor     # native
```
In Telegram, `/status` shows stream lag, backfill progress, watchlist counts and
queues. `/stats` shows counters and measured alert latency. Every 60 s the logs also
contain a `Heartbeat` line with lag and counters.

SQL examples:
```bash
docker compose exec postgres psql -U tron tron_usdt -c \
 "select status, confidence, test_amount_min_raw/1e6 as test_min, test_amount_max_raw/1e6 as test_max, successful_sequences, sender, recipient from watchlist order by confidence_score desc limit 20;"
docker compose exec postgres psql -U tron tron_usdt -c \
 "select alert_type, total_detection_latency_ms, created_at from alerts order by id desc limit 20;"
```

## 15. Simulation mode
The simulation runs the **real** collector, parser, pipeline, pattern engine, watchlist,
matcher and dispatcher against a scripted TRON event stream, using SQLite in memory.
No network is needed.
```bash
docker compose run --rm monitor python -m app.main simulate
# or natively:
.venv/bin/python -m app.main simulate
# also deliver the simulated alerts to your Telegram chat:
.venv/bin/python -m app.main simulate --telegram
```

**Part 1: historical backfill.**
- It learns **A→B 5–10 USDT → 20K–40K**, **C→D 200–300 → 35K–50K** and **E→F 950–1,100 → 80K–120K**.
- All three become ACTIVE with no manual input.
- Dust, random small transfers, a one-off sequence, an irregular business flow, another token and a TRX payload are all rejected.

**Part 2: live stream.**
- An **unconfirmed** 5 USDT A→B transfer gives an immediate 🔴 RED alert, and no large transfer exists yet.
- Its confirmation produces no second alert.
- A duplicate API delivery produces no alert.
- 20,000 USDT gives 🚨 LARGE FOLLOW-UP, and the model is updated from 3 to 4 sequences.
- 250 USDT (C→D) and 1,000 USDT (E→F) give RED alerts.
- Noise to other recipients produces no alert.

**Part 3: real-time learning, `TEST → LARGE → TEST → LARGE → TEST`.**
- It starts with no history (with `MIN_SUCCESSFUL_SEQUENCES=2`, because five events contain only two sequences).
- The relationship becomes ACTIVE after the second LARGE.
- The final TEST produces the 🔴 RED alert **before** the next LARGE.
- That LARGE then produces the follow-up alert.

The run ends with `✅ All simulation checks passed.` and exit code 0.

## 16. Automated tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q                                  # SQLite
TEST_DATABASE_URL="postgresql+asyncpg://tron:PASS@localhost/tron_test" python -m pytest -q   # PostgreSQL
docker compose run --rm monitor python -m pytest -q  # inside the container
```

| # | Requirement | Test(s) |
|---|---|---|
| 1–3 | USDT-only, TRX & other-token rejection | `test_event_parser.py::test_usdt_transfer_is_accepted`, `test_trx_transfer_is_rejected`, `test_other_trc20_token_is_rejected`, `test_trc10_style_payload_rejected_in_batch` |
| 4–5 | Sender / recipient detection | `test_sender_and_recipient_decoded_from_hex`, `test_positional_result_keys_supported` |
| 6, 14 | Relationship matching / independence | `test_pipeline.py::test_relationships_are_independent`, `test_pattern_engine.py::test_models_are_independent` |
| 7–8 | Sequence detection, multiple sequences | `test_single_sequence_detected`, `test_multiple_sequences_detected`, `test_large_not_reused_as_test` |
| 9–10, 23 | Automatic watchlist, no manual wallets, backfill | `test_backfill_creates_watchlist_automatically`, `test_backfill_resumes_after_interruption` |
| 11–13 | 5→20K, 250→40K, 1,000→100K | `test_patterns_of_any_test_size_are_learned[*]`, `test_known_test_triggers_red_alert[*]`, `test_no_global_test_amount` |
| 15–16 | Dust, random small transfers | `test_dust_never_becomes_a_test`, `test_random_small_transfers_rejected`, `test_random_amount_relationships_rarely_qualify`, `test_dust_and_random_small_transfers_never_alert` |
| 17–19 | Test alert, before large, follow-up | `test_known_test_triggers_red_alert`, `test_alert_before_large_then_followup_and_learning`, `test_realtime_learning_sequence` |
| 20–21 | Duplicate tx / alert prevention | `test_duplicate_transactions_stored_once`, `test_duplicate_alert_prevention` |
| 22 | Restart recovery | `test_restart_recovery`, `test_crash_between_store_and_match_is_recovered`, `test_alert_outbox_survives_restart` |
| 24–25 | Model update, decay | `test_model_updates_with_new_sequence`, `test_pattern_decay_old_behaviour_is_replaced`, `test_decay_single_changed_sequence_weakens`, `test_expired_pattern` |
| 26 | Decimal precision | `test_decimal_precision_is_exact` |
| 27–28 | Unconfirmed / confirmation update | `test_unconfirmed_then_confirmed`, `test_confirmation_alert_optional`, `test_unconfirmed_never_confirmed_is_dropped` |
| 29 | Telegram failure recovery | `test_resilience.py::test_telegram_failure_is_retried_until_delivered`, `test_telegram_client_errors_and_rate_limit` |
| 30 | TRON API failure recovery | `test_trongrid_client_retries_then_succeeds`, `test_collector_does_not_advance_cursor_on_api_failure`, `test_collector_does_not_advance_cursor_on_db_failure` |
| — | Read-only / no secrets | `test_readonly.py` |
| — | Simulation passes | `test_simulation.py` |

## 17. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `TRON API unavailable; will retry` with 401/403 | Wrong or missing `TRON_API_KEY`, or the VPS blocks outbound HTTPS. Test with `curl -H "TRON-PRO-API-KEY: $KEY" "https://api.trongrid.io/v1/contracts/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t/events?limit=1"` |
| Many `429` warnings, backfill slow | Rate limit reached. Add or upgrade the API key, or lower `BACKFILL_CONCURRENCY` and `INITIAL_HISTORY_DAYS`. |
| `Database not reachable yet` | PostgreSQL is not up or `DATABASE_URL` is wrong. Run `docker compose ps` and `docker compose logs postgres`. A password with `@ : / %` must be URL-encoded. |
| No Telegram messages | Run `python -m app.main check`. You must message the bot first (private chat), use a negative group ID, and make sure the bot was not removed from the group. Failed messages stay `PENDING` in `alerts` and are retried. |
| `USDT_CONTRACT_ADDRESS is not the official…` | Use `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` unless you really intend another contract. |
| Watchlist stays empty | This is normal at first. Patterns need `MIN_SUCCESSFUL_SEQUENCES` consistent sequences. Wait for the backfill (`/status`) or increase `INITIAL_HISTORY_DAYS`. `/pattern <s> <r>` explains why a pair is not active. |
| Too many alerts for one pair | That pair will be PAUSED by the flood guard. You can also `/pause <s> <r>`. Raising `MIN_PATTERN_CONFIDENCE` or `MIN_SUCCESSFUL_SEQUENCES` makes activation stricter. |
| Container `unhealthy` | The heartbeat is older than 3 minutes: the maintenance loop is stuck or the DB is down. Check the logs, then `docker compose restart monitor`. |
| Disk filling | Lower `RETENTION_DAYS`. Check with `docker system df` and `SELECT pg_size_pretty(pg_database_size('tron_usdt'));` |

## 18. Updating the application
```bash
cd /opt/SpTronFinal && git pull
rsync -a --exclude .env tron-usdt-pattern-monitor/ /opt/tron-usdt-pattern-monitor/
cd /opt/tron-usdt-pattern-monitor
docker compose up -d --build                 # migrations run automatically on start
# native: .venv/bin/pip install -r requirements.txt && systemctl restart tron-usdt-monitor
```
Back up the database first:
```bash
docker compose exec postgres pg_dump -U tron tron_usdt | gzip > backup-$(date +%F).sql.gz
```

---

## Configuration reference
All settings are environment variables (see `.env.example`). The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `TRON_API_URL` / `TRON_API_KEY` | trongrid / – | TronGrid-compatible event API |
| `USDT_CONTRACT_ADDRESS` | `TR7NH…Lj6t` | Token contract monitored (only this one) |
| `DATABASE_URL` | – | `postgresql+asyncpg://user:pass@host:5432/db` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | – | Alert destination; if either is empty, alerts are printed to stdout |
| `POLL_INTERVAL_SECONDS` | 1 | Confirmed-stream poll interval |
| `ENABLE_UNCONFIRMED` | true | Early alerts from unsolidified blocks |
| `INITIAL_HISTORY_DAYS` | 30 | History scanned on first start |
| `MIN_SUCCESSFUL_SEQUENCES` | 3 | Consistent sequences needed for ACTIVE |
| `MIN_LARGE_TO_TEST_RATIO` | 10 | "Substantially larger" = at least this many × the preceding transfer (not a test amount!) |
| `MAX_FOLLOWUP_HOURS` | 168 | Maximum test → large gap |
| `MIN_PATTERN_CONFIDENCE` | HIGH | LOW / MEDIUM / HIGH needed for ACTIVE |
| `MIN_SUCCESS_RATE` | 0.5 | Share of test-like transfers that must have led to a large one |
| `TEST_MATCH_FACTOR` | 2.0 | Relative band around the learned test range |
| `PATTERN_HALF_LIFE_DAYS` | 30 | Recency weighting (decay) |
| `PATTERN_EXPIRY_DAYS` | 60 | No sequence for this long → EXPIRED |
| `ALERT_MAX_TX_AGE_MINUTES` | 60 | Do not raise live alerts for older transfers (for example after long downtime) |
| `FLOOD_MAX_TESTS_PER_HOUR` | 6 | Automatic PAUSE threshold |
| `RETENTION_DAYS` | 150 | Delete stored transfers older than this (0 = keep all) |
| `DISPLAY_TIMEZONE` | UTC | For example `Asia/Shanghai` shows "UTC+8" in alerts |
| `LOG_LEVEL` / `LOG_FORMAT` | INFO / text | `DEBUG` logs every transfer; `json` for log shippers |

## Sizing & performance
- USDT is the busiest token on TRON, with **roughly 2–3 million transfers per day**
  (tens per second). The single collector handles this with bulk inserts (200 events
  per statement), atomic per-pair upserts, an in-memory watchlist lookup on the hot
  path, a cheap prefilter before per-pair analysis, and analysis workers that run off
  the critical path.
- **Storage.** Every transfer is stored, because learning needs it. This comes to
  about 1–1.5 GB/day including indexes, so 30 days is about 40 GB. Set
  `RETENTION_DAYS` to at least `PATTERN_LOOKBACK_DAYS` and size the disk to match.
- **Backfill cost.** 30 days is on the order of 70M events, or about 350k TronGrid
  requests. That takes hours even with a key. Live monitoring runs during the
  backfill. Start with `INITIAL_HISTORY_DAYS=7` if your plan is limited.
- Run **one** instance per database. The in-memory watchlist cache is per process.

## Project layout
```
app/
  collector/   tron_listener.py (TronGrid client, confirmed/unconfirmed/backfill loops), event_parser.py, address.py
  detector/    pattern_engine.py (sequences + learned model), confidence.py, test_detector.py, followup_detector.py
  watchlist/   manager.py (state machine, alerts), model.py (in-memory cache)
  database/    models.py, repository.py, session.py, migrations/ (Alembic)
  telegram/    bot.py (Bot API client + commands), alerts.py (outbox dispatcher), messages.py (formatting)
  processing/  pipeline.py (store → match → alert), analysis.py (learning), deduplication.py, maintenance.py
  simulation/  source.py (TronGrid-compatible fake stream), runner.py
  config/      settings.py
  main.py      entry point: run | simulate | migrate | check
migrations/    001_initial_schema.sql (plain SQL equivalent of the Alembic migration)
tests/         pytest suite (SQLite by default, PostgreSQL via TEST_DATABASE_URL)
systemd/       tron-usdt-monitor.service (native), tron-usdt-monitor-docker.service
```
