# TRON USDT TRC-20 Wallet Monitor

A read-only bot that watches **Wallet A** on TRON mainnet and automatically discovers every
wallet that Wallet A sends USDT TRC-20 to, however small the amount. It then sends a
**Telegram alert when any discovered wallet receives ≥ 500 USDT from any sender**.

```
Wallet A ──0.01 USDT──▶ Wallet 123         (discovered automatically, no minimum)
Somebody ──750 USDT───▶ Wallet 123         (🚨 Telegram alert)
```

* Only USDT TRC-20 is tracked. TRX, TRC-10, other TRC-20 tokens and other events are ignored.
* Amounts are integers in base units (1 USDT = 1,000,000). Floating point is never used.
* The threshold is inclusive: 499.999999 → no alert; 500 → alert; 500.000001 → alert.
* Duplicates are blocked by database constraints and survive restarts, crashes and retries.
* The bot is **read-only**: no private keys, no signing, no transactions.

---

## Quick start (Docker, Linux VPS)

```bash
git clone <this repo> && cd SpTronFinal/tron-usdt-wallet-monitor
cp .env.example .env        # then edit: TRON_API_KEY, ROOT_WALLET, TELEGRAM_*, POSTGRES_PASSWORD
docker compose up -d --build
docker compose logs -f monitor
curl -s localhost:8080/health
```

On first start the bot:

1. creates the database schema (Alembic migrations run automatically);
2. records **"monitoring started"** (persisted; history before it never alerts unless configured);
3. checks the token contract (`symbol()` / `decimals()`) and warns if it is not the official USDT;
4. sends **✅ Monitor started** to your Telegram chat.

Open your bot in Telegram and press **Start** once, otherwise Telegram will not let it message you.

Updating: `git pull && docker compose up -d --build`. Stopping: `docker compose down`
(data stays in the `pgdata` volume).

### Without Docker

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# PostgreSQL 14+ with a database, and DATABASE_URL in .env
python -m app.main
```

---

## How it works

```
                      ┌────────────────────────── TRON API client (TronGrid, read-only) ──────────────────────────┐
                      │   global semaphore MAX_CONCURRENT_API_REQUESTS + token bucket MAX_REQUESTS_PER_SECOND     │
                      └───────────────┬──────────────────────────────────────────────┬────────────────────────────┘
                                      │                                              │
          LIVE STREAM (fast path)     │                MONITORING SCHEDULER (safety net) + BACKFILL
  /v1/contracts/USDT/events  every    │      discovery queue ─┐   fixed worker pool (RECONCILE_WORKERS)
  POLL_INTERVAL_SECONDS, one request  │      periodic sweep  ─┴─▶ /v1/accounts/{w}/transactions/trc20
  for ALL wallets                     │                           from each wallet's checkpoint
                                      ▼                                              ▼
                              TRANSFER NORMALIZER  (USDT Transfer events only, validated addresses, integers)
                                      ▼
                    TRANSFER PROCESSOR, one PostgreSQL transaction per batch
                    ├─ store transfer      UNIQUE(tx_hash, event_index)
                    ├─ discovery engine    Wallet A → new address ⇒ wallets
                    └─ large detector      monitored wallet receives ≥ threshold ⇒ alerts (outbox)
                                      ▼
                    ALERT DISPATCHER (pending → sending → sent, retried until delivered) ──▶ Telegram
```

### Wallet A discovery

Every USDT `Transfer` event is checked against an in-memory registry of wallets, loaded from
PostgreSQL at startup. When **from = Wallet A** and **to = any other valid address**, the
recipient is inserted into `wallets` as `discovered`. It records hop = 1, the discovery
transaction, the amount and the on-chain time. There is no minimum amount: 0.000001 USDT counts.
An address that is already known is not inserted again (`UNIQUE(address)`).
Zero-value transfers are ignored because they are the usual address-poisoning spam.

### How discovered wallets are monitored

A discovered wallet starts being monitored in the same database transaction that discovers it.
From then on **every incoming USDT transfer from any sender** is checked. If the amount is
≥ `ALERT_MIN_AMOUNT_USDT` and the transfer happened at or after discovery, an alert row is
written. Outgoing transfers from discovered wallets are ignored. Self-transfers are ignored
unless `ALERT_ON_SELF_TRANSFER=true`.

Monitoring uses two independent paths:

1. **Live stream (seconds).** One poller reads the USDT contract's whole `Transfer` stream
   (~30 transfers/s on TRON) and matches each event in memory. **API cost does not grow with
   the number of wallets**: 10 or 100,000 monitored wallets still cost about one request per
   poll. By default, events are processed as soon as TronGrid sees them, about 3 s after the
   block (`REQUIRE_CONFIRMED=false`).
2. **Monitoring scheduler (minutes).** A fixed pool of workers sweeps every monitored wallet
   through `/v1/accounts/{wallet}/transactions/trc20`, starting from that wallet's checkpoint.
   It catches anything the stream could have missed: a longer API outage, an event that arrived
   late, or a wallet discovered after its deposit was already streamed. Newly discovered wallets
   jump the queue. Only unknown transfers ≥ the threshold, plus Wallet A's outgoing transfers,
   are looked up in detail, so a sweep stays cheap.

### Duplicate prevention

| Layer | Guarantee |
|---|---|
| `transfers` `UNIQUE(tx_hash, event_index)` | A transfer is stored once. One transaction can contain several USDT transfers, so `tx_hash` alone is not enough. |
| Same DB transaction for transfer + wallet + alert | Either everything is committed or nothing is. A re-processed transfer is recognised as already done and skipped, together with its alert. |
| `alerts` `UNIQUE(tx_hash, event_index, alert_type)` | At most one alert per transfer. |
| `wallets` `UNIQUE(address)` | A wallet is discovered once. |
| Checkpoints advance only after a commit, and only forward | A restart resumes exactly where processing stopped. The small overlap re-read is harmless because of the constraints above. |
| Outbox: `pending → sending → sent`, atomic claim | Telegram outages delay an alert but never lose or duplicate it. Two dispatchers can never send the same row. |

**The one theoretical exception:** the process crashes in the few milliseconds after Telegram
accepted a message but before `sent` is committed. That alert is re-sent once on restart,
marked *"♻️ Re-sent after a restart"*. Telegram offers no way to make delivery exactly-once,
so this is the safest trade-off: an alert is never silently lost.

### Restarts, crashes and outages

* **Stream cursor:** `checkpoints['stream:<contract>']` holds the newest block time fully
  processed. Each poll restarts at `cursor − STREAM_OVERLAP_SECONDS`.
* **Wallet checkpoints:** `checkpoints[<wallet>]` and `checkpoints['out:<Wallet A>']` are
  used by the scheduler.
* **Monitoring start** is stored once. Transfers that happened **while the bot was down**
  are caught up and still alert. Transfers from **before the bot was ever started** do not.
* **API failures:** timeouts, 429 and 5xx responses retry with exponential backoff, jitter
  and `Retry-After` support. A failing loop backs off up to 60 s and never takes the others down.
* **Database failures:** the batch rolls back and the cursor does not move. The loop retries
  until PostgreSQL is back, and `/health` reports `db_ok: false` meanwhile.
* **Telegram failures:** alerts stay `pending` and are retried with backoff, and the bot
  honours Telegram's `retry_after`. Messages are paced at about 1 per second per chat.
* **VPS reboot:** Docker `restart: unless-stopped` brings both containers back.

### Scaling to thousands of wallets

* The live path is **O(1) in API calls** regardless of wallet count. Matching is an in-memory
  dictionary lookup (about 100 bytes per wallet, so 1 million wallets ≈ 100 MB).
* There is never one loop per wallet. The scheduler has `RECONCILE_WORKERS` workers, and every
  API call from every component shares one semaphore (`MAX_CONCURRENT_API_REQUESTS`) and one
  rate limit (`MAX_REQUESTS_PER_SECOND`). More wallets only make a safety-net sweep take
  longer: 1,000 wallets at 10 req/s ≈ 100 s per sweep. The sweep repeats every
  `RECONCILE_INTERVAL_SECONDS` at most.
* All lookups are indexed: `to_address`, `from_address`, `tx_hash`, `block_number`,
  `timestamp`, `amount_base_units`. `/wallets` is paginated.

### Finality (reorgs)

TRON solidifies a block after about 19 blocks (≈ 1 minute), and reorgs deeper than 1–2
blocks are extremely rare. With `REQUIRE_CONFIRMED=false` (the default, fastest), an alert can
be about an unconfirmed transfer; the message shows **⏳ Unconfirmed (fast mode)**. Set
`REQUIRE_CONFIRMED=true` to alert only on irreversible blocks, about 1 minute later.

### Recursive tracking (MAX_HOPS)

`MAX_HOPS=1` (default): only Wallet A discovers wallets. The registry stores a `hop` for every
wallet and any wallet with `hop < MAX_HOPS` expands. `MAX_HOPS=2` therefore also discovers the
recipients of Wallet A's recipients (with live and scheduler support). Backfill only scans
Wallet A itself.

---

## Configuration

All settings are in `.env` (see [`.env.example`](.env.example)). The most important:

| Variable | Default | Meaning |
|---|---|---|
| `TRON_API_KEY` | – | TronGrid API key (strongly recommended) |
| `ROOT_WALLET` | – | Wallet A |
| `USDT_CONTRACT` | `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` | Official Tether USDT on TRON |
| `USDT_DECIMALS` | `6` | |
| `ALERT_MIN_AMOUNT_USDT` | `500` | **Alert threshold**, inclusive (`>=`), exact decimals e.g. `1000.5` |
| `POLL_INTERVAL_SECONDS` | `2` | Live poll interval (1, 3, 5, … all fine) |
| `REQUIRE_CONFIRMED` | `false` | `true` = only solidified blocks |
| `BACKFILL_ENABLED` | `false` | Scan Wallet A's history once at startup |
| `BACKFILL_LOOKBACK_DAYS` | `30` | How far back |
| `ALERT_ON_HISTORICAL` | `false` | Also alert on transfers from before monitoring started |
| `MAX_HOPS` | `1` | Recursion depth (1 = direct recipients only) |
| `ALERT_ON_DISCOVERY` | `false` | Message for every newly discovered wallet |
| `ALERT_ON_SELF_TRANSFER` | `false` | Alert when a wallet sends ≥ threshold to itself |
| `MAX_CONCURRENT_API_REQUESTS` | `5` | Global in-flight API request cap |
| `MAX_REQUESTS_PER_SECOND` | `10` | Global API rate limit |
| `RECONCILE_ENABLED` / `RECONCILE_INTERVAL_SECONDS` / `RECONCILE_WORKERS` | `true` / `300` / `2` | Safety-net scheduler |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID` | – | Alerts go to, and commands are accepted only from, this chat |
| `DATABASE_URL` / `POSTGRES_PASSWORD` | – | PostgreSQL |
| `HEALTH_PORT` | `8080` | `GET /health` |
| `LOG_FORMAT` | `text` | `text` or `json` |

**Changing the threshold:** set e.g. `ALERT_MIN_AMOUNT_USDT=1000` in `.env` and run
`docker compose up -d`. The value is converted exactly to base units, and more than 6 decimals
is rejected at startup.

### Backfill

With `BACKFILL_ENABLED=true` the bot pages through Wallet A's outgoing USDT history for
`BACKFILL_LOOKBACK_DAYS`, one window at a time. Progress is saved, so it resumes after a
restart. Every recipient becomes a discovered wallet and is monitored **from the moment
monitoring started**. Historical transfers never alert unless `ALERT_ON_HISTORICAL=true`. The
backfill runs alongside live monitoring and stops for good once complete (`system_state.backfill_done`).

---

## Telegram

### Commands (admin chat only; other chats are ignored)

| Command | |
|---|---|
| `/start`, `/help` | Introduction |
| `/status` | Root wallet, discovered and monitored counts, large transfers, last processed block, last successful API request, stream lag, uptime |
| `/stats` | Total discovered wallets, transfers processed, total alerts, alerts in the last 24 h, largest transfer |
| `/wallets [page]` | Discovered wallets, paginated with ◀️ / ▶️ buttons |
| `/recent` | Last 10 large transfers |
| `/pause` / `/resume` | Pause or resume alerts. Monitoring continues and alerts are recorded as `suppressed`; the setting survives restarts. |

### Example alert

```
🚨 LARGE USDT TRANSFER DETECTED

Network: TRON
Token: USDT TRC-20

Amount: 2,500 USDT

Discovered Wallet:
THgck9vJLbQcZezxBpbcf5uywJj6uP8hkR

Sender:
TDvQCwicQfWThCZcJMgm1VvMGqiVmY7EHC

Original Discovery:
Wallet A (TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn)

Discovery Transaction:
709b55bd3da0f5a838125bd0ee20c5bfdd7caba173912d4281cae816b79a201b

Large Transfer Transaction:
1f3cb18e896256d7d6bb8c11a6ec71f005c75de05e39beae5d93bbd1e2c8b7a9

Time Since Discovery:
3 hours 17 minutes

Transaction:
https://tronscan.org/#/transaction/1f3cb18e896256d7d6bb8c11a6ec71f005c75de05e39beae5d93bbd1e2c8b7a9

Timestamp:
2026-10-01 08:12:25 UTC

Status: ✅ Confirmed · Block 70280781
```

```
🔎 NEW WALLET DISCOVERED              (only with ALERT_ON_DISCOVERY=true)

Root Wallet:
Wallet A (TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn)

Destination:
THgck9vJLbQcZezxBpbcf5uywJj6uP8hkR

Amount:
0.01 USDT

Transaction:
https://tronscan.org/#/transaction/709b55bd…

Timestamp:
2026-10-01 08:12:25 UTC
```

---

## Health and logs

* `GET http://127.0.0.1:8080/health` returns `200 {"status":"ok",…}` or `503 {"status":"degraded",…}`.
  It reports degraded when the stream has not polled for `HEALTH_MAX_STALL_SECONDS` or the
  database is unreachable. Docker's `HEALTHCHECK` uses `python -m app.health`.
* Logs are structured: `key=value` text or JSON lines with `LOG_FORMAT=json`. They cover
  discovered wallets, large transfers, API latency, API errors and retries, checkpoint
  resumes, sweep progress, Telegram success and failure, and end-to-end latency:

```
[INFO] Wallet discovered address=THgck… hop=1 parent=TWkvff… amount=0.01 tx=709b… source=stream db_ms=24
[WARNING] LARGE TRANSFER DETECTED alert_id=1 wallet=THgck… sender=TDvQ… amount=2,500 chain_to_detect_ms=2370 db_ms=24
[INFO] Telegram alert sent alert_id=1 telegram_ms=7 chain_to_detect_ms=2370 detect_to_sent_ms=45 chain_to_sent_ms=2415
```

The API key, bot token and database password are registered with the log redactor and appear
as `***` if they ever occur in a log line or error message. They are never included in Telegram
messages.

---

## Database

PostgreSQL tables: `wallets`, `transfers`, `alerts`, `checkpoints`, `system_state`. The schema is
defined in [`app/db/models.py`](app/db/models.py). The migration lives in
[`app/db/migrations/versions/0001_initial_schema.py`](app/db/migrations/versions/0001_initial_schema.py),
with plain SQL in [`migrations/0001_initial_schema.sql`](migrations/0001_initial_schema.sql).
Only relevant transfers are stored (Wallet A's outgoing transfers and transfers received by
monitored wallets), not the whole USDT stream, so the database stays small.

Useful queries:

```sql
SELECT address, discovered_at, first_seen_tx FROM wallets WHERE wallet_type='discovered' ORDER BY discovered_at DESC LIMIT 20;
SELECT amount_usdt, discovered_wallet, sender, tx_hash, status FROM alerts ORDER BY id DESC LIMIT 20;
SELECT * FROM checkpoints;
```

---

## Tests

The tests run against a real PostgreSQL database, with fake TRON and Telegram APIs (no network):

```bash
pip install -r requirements-dev.txt
export TEST_DATABASE_URL=postgresql+asyncpg://tron:tron@localhost:5432/tron_usdt_test   # empty database
pytest -q
```

Or with Docker: `docker compose run --rm -e TEST_DATABASE_URL=postgresql+asyncpg://tron:$POSTGRES_PASSWORD@postgres:5432/tron_usdt monitor pytest -q`
(⚠️ this resets that database. Use a separate one if the bot has live data.)

Coverage includes the 11 required scenarios:

* dust discovery (0.01 and 0.000001);
* 499 / 500 / 500.000001;
* a large transfer from an unrelated sender;
* a transaction processed twice (one alert);
* restart resuming from the checkpoint;
* non-USDT and TRX transfers ignored;
* no duplicate wallets.

It also covers:

* atomic rollback;
* API and DB failures not moving the cursor;
* scheduler recovery of missed transfers and discoveries;
* backfill: no historical alerts, resumable;
* the API concurrency cap, retries and rate limit;
* Telegram retry, crash recovery and concurrent dispatchers;
* admin-only commands, pagination, pause/resume persistence;
* `/health`, secret redaction, migration ↔ model parity, a read-only guard;
* 2,000 wallets costing one request per poll.

---

## Security

* Read-only. Only TronGrid read endpoints are used (`/v1/...` queries,
  `gettransactioninfobyid`, and `triggerconstantcontract` for `symbol()`/`decimals()`).
  There is no signing, broadcasting, key handling or wallet control. A test enforces this.
* Every TRON address is validated with Base58Check before it is used or stored.
* Telegram commands are answered only for `TELEGRAM_ADMIN_CHAT_ID`.
* Keep `.env` private (`chmod 600 .env`). It is git-ignored and docker-ignored.
* The container runs as an unprivileged user, and the health port is bound to `127.0.0.1`.
