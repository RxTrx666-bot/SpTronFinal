# tron-wallet-outflow-alert

A read-only Telegram bot that watches **one TRON mainnet wallet** for **outgoing USDT (TRC-20)**
transfers of specific amounts and posts an alert for each one. It never creates, signs or
broadcasts a transaction, and it has no setting for a private key.

| | |
|---|---|
| Wallet | `TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn` |
| Token | USDT `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`, 6 decimals |
| Filter | outgoing only, amounts `1.01, 10, 20, 30` ± `0.000100` USDT (inclusive) |
| Source | TronGrid (`TRON-PRO-API-KEY` header) |

## How it works

```
every POLL_INTERVAL_SECONDS (2s)
  1. GET /v1/accounts/{wallet}/transactions/trc20?contract_address=USDT&only_from=true
         &min_timestamp=cursor-LOOKBACK&order_by=block_timestamp,asc   (paged by fingerprint)
     pre-filter: token == USDT, from == wallet, amount in filter  -> SQLite `candidates`
     cursor advances to the newest block_timestamp of each completed page
  2. POST /wallet/gettransactioninfobyid {value: txid}   (or /walletsolidity in solidified mode)
     require receipt.result == SUCCESS
     decode every log: topic0 == ddf252ad… (Transfer(address,address,uint256)),
     log.address == USDT contract, topic1 (from) == wallet, data (uint256) in filter
     -> one transaction: write `transfers` + `outbox` rows + `verified_txs`, drop candidate
  3. delivery loop sends the outbox to each TELEGRAM_CHAT_ID; a row is deleted only after
     Telegram accepts it (retry with backoff, honours 429 retry_after)
```

On startup it calls `triggerconstantcontract` for `symbol()` and `decimals()` and refuses to
start unless they return `USDT` and `6`. It also checks the bot token with `getMe`.

Why nothing is missed or duplicated:

- **Cursor + lookback.** The cursor is saved in SQLite. Each scan re-reads `LOOKBACK_SECONDS`
  before it, which covers transfers that TronGrid indexes late. After downtime, the next scan
  continues from the saved cursor, paging up to `MAX_PAGES_PER_POLL` pages per poll.
- **The cursor only moves after a page is fully processed.** If TronGrid fails mid-scan, the
  next poll repeats it.
- **The receipt and event log are the source of truth.** The listing amount is only a
  pre-filter. The alert uses the amount decoded from the log. A failed receipt, or a
  `Transfer` emitted by another contract, gets rejected.
- **Transactions not yet in a block** (`{}` from `gettransactioninfobyid`) are retried with
  backoff for up to `VERIFY_MAX_AGE_SECONDS`.
- **Exactly-once bookkeeping.** `verified_txs` stops a txid from being verified twice. The
  `(txid, log_index, chat_id)` unique key stops a duplicate alert. Alerts sit in the outbox
  until they are delivered, so they survive restarts and Telegram outages.
- **One database, one wallet.** The database records the wallet and contract it belongs to,
  and refuses a different configuration.

Amounts are integers in base units throughout. Config strings are parsed with `Decimal`, and
a value with more than 6 decimals is rejected. Floats are never used for token amounts.

## Setup

```bash
cp .env.example .env           # set TRON_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m outflow_alert check  # token + Telegram checks only
python -m outflow_alert        # run
```

Add the bot to the target chat or channel first (as an admin for a channel). Exit codes:
`2` config error, `3` startup check failed (wrong token metadata, bad bot token, TronGrid
unreachable), `4` the database belongs to another wallet.

### Docker

```bash
docker compose up -d --build
docker compose logs -f
```

State is kept in the `outflow-data` volume. The container runs as a non-root user with a
read-only root filesystem. Its health check fails if the poll loop stops writing its heartbeat.

### systemd

Install to `/opt/tron-wallet-outflow-alert` with a `.venv`, then
`cp systemd/tron-outflow-alert.service /etc/systemd/system/ && systemctl enable --now tron-outflow-alert`.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `TRON_API_KEY` | – | required |
| `TELEGRAM_BOT_TOKEN` | – | required |
| `TELEGRAM_CHAT_ID` | – | required; comma-separated for several chats |
| `WALLET_ADDRESS` | `TWkvff…cdn` | |
| `USDT_CONTRACT` | `TR7NHq…Lj6t` | checked against `symbol()`/`decimals()` at startup |
| `AMOUNTS` | `1.01,10,20,30` | USDT, at most 6 decimals |
| `AMOUNT_TOLERANCE` | `0.000100` | inclusive (±100 base units) |
| `POLL_INTERVAL_SECONDS` | `2` | min 0.5 |
| `LOOKBACK_SECONDS` | `120` | overlap re-scanned on every poll |
| `INITIAL_BACKFILL_SECONDS` | `0` | first start only (the lookback still applies) |
| `CONFIRMATION_MODE` | `fast` | `fast` = in a block with receipt SUCCESS; `solidified` = irreversible (about 1 min later) |
| `VERIFY_MAX_AGE_SECONDS` | `3600` | give up on candidates that never appear in a block |
| `MAX_PAGES_PER_POLL` / `PAGE_LIMIT` | `25` / `200` | catch-up speed after downtime |
| `DB_PATH` | `data/outflow_alert.db` | SQLite (WAL) |
| `HEARTBEAT_FILE` | empty | used by the Docker health check |
| `LOG_LEVEL` | `INFO` | the bot token and API key are never logged |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests run against in-memory fakes of TronGrid and Telegram (`tests/fakes.py`). They cover
tolerance edges, incoming, failed and foreign-contract transfers, listing and log mismatches,
pending transactions, pagination, restart, Telegram and TronGrid outages, several chats, the
startup token check, and a guard that the client cannot reach any write endpoint.
