# TRON USDT Monitor

A Telegram bot that watches one TRON mainnet wallet and alerts you about **every USDT TRC-20
transfer between 1.000000 and 1.200000 USDT (inclusive), incoming or outgoing**. It ignores everything else.

| | |
|---|---|
| Wallet | `TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn` |
| Token | USDT TRC-20, contract `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`, 6 decimals |
| Range | `1.000000` ≤ amount ≤ `1.200000` USDT, integer-exact |
| Directions | INCOMING, OUTGOING (and SELF, wallet → wallet) |
| Alert time | Real block timestamp, the bot's detection time, and the latency between them |

> ⚠️ **About the contract address.** The spec named `TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7` as
> the USDT contract. That is **not** Tether's official USDT contract on TRON mainnet. The official
> one is **`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`** (the contract behind USDT on
> [Tronscan](https://tronscan.org/#/token20/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t)). The bot uses
> the official contract by default. On every start it also calls `symbol()` and `decimals()` on the
> configured contract and refuses to run unless they return `USDT` and `6`. A wrong
> `USDT_CONTRACT` stops the bot with a clear error. It never runs silently while watching the wrong token.

---

## 1. How transaction detection works

```
TRON API ──► parser (strict validation) ──► filter ──► dedup (DB UNIQUE tx_hash) ──► Telegram
                                             │
            contract == USDT, event == Transfer, wallet is from/to, 1.000000 ≤ amount ≤ 1.200000
```

### Mode `account` (default, for TronGrid)
1. Every `POLL_INTERVAL_SECONDS` (default 2s) the bot calls
   `GET /v1/accounts/{wallet}/transactions/trc20?contract_address=TR7NH…&min_timestamp=…`.
   This is TronGrid's index of TRC-20 `Transfer` events that involve the wallet in either
   direction, filtered by the API to the USDT contract. Unconfirmed-but-in-block
   transfers are included, so detection is as fast as possible.
2. Each record is strictly validated (tx id, Base58Check addresses, integer `value`, timestamp).
   The filter then checks contract, symbol, decimals, event type, wallet involvement and the amount range.
3. **On-chain verification** (`VERIFY_EVENT_LOG=true`): for each candidate the bot fetches
   `/wallet/gettransactioninfobyid` and decodes the raw TRC-20 event log itself:
   `Transfer(address,address,uint256)` with topic
   `ddf252ad…b3ef`, emitted by the USDT contract address, and the transaction receipt must be `SUCCESS`.
   The alert is only sent if the chain's own event log matches sender, recipient and amount.
   This step also supplies the block number.
4. A cursor (latest seen block timestamp) is saved in SQLite. Each poll re-checks an overlap window
   (`LOOKBACK_SECONDS`), so late-indexed transactions are never missed. After a restart or
   outage the bot resumes from the cursor and catches up on anything it missed.

### Mode `blocks` (any java-tron full node: QuickNode, GetBlock, Ankr, own node…)
The bot reads the chain head and then, for **every new block**, calls
`/wallet/gettransactioninfobyblocknum`. It decodes every log emitted by the USDT contract whose
`Transfer` topics contain the wallet. No index is involved; this is pure event decoding. The last
processed block number is persisted, and after downtime every missed block is scanned.

### Why the bot polls instead of streaming
TronGrid and standard java-tron HTTP nodes don't offer a public websocket or event-stream
subscription for TRC-20 transfers, so the bot polls. The loop is designed to be fast and cheap:
one small request every 2 s (≈43k requests/day, inside TronGrid's free 100k/day with an API key).
A TRON block is produced every 3 s, so a 2 s poll catches a transfer within one block interval.
Typical detection latency is about 2–6 s after the block timestamp.

### What is ignored
TRX transfers (no TRC-20 event), TRC-10 tokens, other TRC-20 tokens (including fake tokens named
"USDT" from other contracts), `Approval` and other events, TRC-721 transfers, failed/reverted
transactions, USDT < 1.000000 or > 1.200000, and transfers not involving the wallet.

### Precision
Amounts are integer base units (`1 USDT = 1_000_000`). `MIN_USDT`/`MAX_USDT` are parsed with
`Decimal` and converted exactly. Floating point is never used for the range check.
`1.200000` → match, `1.200001` → no match.

### Exact timestamps
"Blockchain Time" is the timestamp of the block that contains the transaction (`block_timestamp`
/ `blockTimeStamp` from the API, ms precision). It is **not** the time the bot saw it.
"Detected By Bot" is the bot's clock at detection. Both are logged with milliseconds:

```
matching_transaction_detected | ... block_time="2026-09-27 21:18:42.000 UTC" detected="2026-09-27 21:18:43.120 UTC" detection_latency_s=1.120
telegram_alert_sent           | ... detection_latency_s=1.120 alert_latency_s=1.410 telegram_send_s=0.290
```

Keep the VPS clock synced (`timedatectl set-ntp true`), or latency numbers will be off.

### No duplicate alerts
- `transactions.tx_hash` has a UNIQUE index. The bot checks for an existing row first, then inserts
  with `ON CONFLICT DO NOTHING`, and only a successful insert queues an alert.
- Alerts are stored as `pending` and set to `sent` only after Telegram confirms delivery. If
  Telegram is down, the alert retries with backoff until it goes through. After a crash, pending
  alerts are re-sent on the next start. Already-sent ones are never sent again.
- Everything lives in `data/monitor.db`, so dedup survives restarts and reboots.

### Historical transactions
With `BACKFILL_ENABLED=false` (default), the first start records the current chain time or block as
the monitoring point, and only newer transfers alert. With `BACKFILL_ENABLED=true`, the first start
also scans history (newest first, up to `BACKFILL_MAX_PAGES` × 200 transfers) and sends at most
`BACKFILL_LIMIT` matches, oldest first, labelled **HISTORICAL**. Backfill uses TronGrid's `/v1` API
and runs only once.

---

## 2. Telegram setup

1. In Telegram open **@BotFather**, send `/newbot` and follow the prompts. Copy the **bot token**.
2. Open a chat with your new bot and press **Start**, so the bot is allowed to message you.
3. Get your **chat ID** with either method:
   - message **@userinfobot**, which replies with your numeric id, or
   - open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser after messaging your bot,
     and read `"chat":{"id": …}`.
   - For a group: add the bot to the group, send a message, and use the negative group id from `getUpdates`.
4. Put both values in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_CHAT_ID`.

Only `TELEGRAM_ADMIN_CHAT_ID` can use commands or receive alerts. Other chats get
"⛔ Unauthorized", and the attempt is logged.

| Command | Shows |
|---|---|
| `/start` | Intro |
| `/status` | Online/degraded status, wallet, network, contract, range, detected count, last tx checked, last match, API latency, detection latency, uptime |
| `/wallet` | Monitored wallet, network, token, range, monitoring state |
| `/help` | Command list |

---

## 3. TRON API configuration

### TronGrid (recommended; the key you have is a TronGrid key)
```
TRON_API_URL=https://api.trongrid.io
TRON_API_KEY=<your TronGrid API key>
TRON_API_KEY_HEADER=TRON-PRO-API-KEY
MONITOR_MODE=account
```
The key is sent as the `TRON-PRO-API-KEY` header. Keys are created at https://www.trongrid.io/.

### Other providers (QuickNode, GetBlock, Ankr, NOWNodes, own java-tron node)
Most of these expose only the full-node API (`/wallet/...`) and not TronGrid's `/v1` index:
```
TRON_API_URL=https://your-endpoint.example/your-key-path   # full HTTP endpoint
TRON_API_KEY=                                              # or key + header name if the provider uses a header
TRON_API_KEY_HEADER=x-api-key
MONITOR_MODE=blocks
```
`BACKFILL_ENABLED=true` needs the `/v1` API (TronGrid). Without it, backfill is skipped with a warning.

### Speed vs. finality
`CONFIRMED_ONLY=false` (default) alerts as soon as the transaction is in a block, which is fastest.
`CONFIRMED_ONLY=true` uses solidified blocks only (irreversible, about 1 minute slower).

---

## 4. Installation and VPS deployment (Docker, recommended)

Requirements: a Linux VPS (Ubuntu/Debian), Docker Engine and the compose plugin.
```bash
curl -fsSL https://get.docker.com | sh          # installs Docker + compose plugin
sudo timedatectl set-ntp true                   # accurate clock for latency measurement
```

**1. Clone or upload the project**
```bash
git clone <your-repo-url> && cd <repo>/tron-usdt-monitor
# or: scp -r tron-usdt-monitor user@vps:/opt/ && cd /opt/tron-usdt-monitor
```
**2. Create .env**
```bash
cp .env.example .env
chmod 600 .env
nano .env
```
**3. Insert the TRON API credentials:** `TRON_API_URL`, `TRON_API_KEY`
**4. Insert the Telegram bot token:** `TELEGRAM_BOT_TOKEN`
**5. Insert the admin chat ID:** `TELEGRAM_ADMIN_CHAT_ID`

**6. Build the container**
```bash
docker compose build
```
**7. Start the bot**
```bash
docker compose up -d
```
You should receive "🟢 Monitor started" in Telegram. Send `/status` to check it.

**8. Check logs**
```bash
docker compose logs -f --tail=100
docker compose ps            # shows health: healthy once polling works
```
**9. Restart the bot**
```bash
docker compose restart
```
**10. Update the bot**
```bash
git pull                     # or upload the new files
docker compose up -d --build
```
Stop: `docker compose down`. The database in `./data/` is kept.
The container runs as a non-root user, restarts automatically (`restart: unless-stopped`, which also
covers VPS reboots once Docker is enabled with `systemctl enable docker`), and rotates its logs.

### Option B: systemd without Docker
```bash
sudo apt install -y python3 python3-venv
sudo useradd --system --home /opt/tron-usdt-monitor tronmon
sudo cp -r tron-usdt-monitor /opt/ && cd /opt/tron-usdt-monitor
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env && sudo chmod 600 .env
sudo mkdir -p data && sudo chown -R tronmon:tronmon /opt/tron-usdt-monitor
sudo cp deploy/tron-usdt-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tron-usdt-monitor
journalctl -u tron-usdt-monitor -f          # logs
sudo systemctl restart tron-usdt-monitor    # restart
```

### Run locally (development)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in
python -m app.main
pytest                 # run the test suite
```

---

## 5. Error handling

| Failure | Behaviour |
|---|---|
| TRON API timeout / 5xx / network drop | Up to `TRON_MAX_RETRIES` retries per request (exponential backoff and jitter). Then the poll loop backs off 1→2→4…60 s and keeps trying. `/status` shows DEGRADED |
| Rate limit (429 / "limit" responses) | Honours `Retry-After`, otherwise backs off |
| Malformed record or log | Skipped with a warning. It can never be accepted as a match |
| Telegram down or 429 | Alert stays `pending` and retries until delivered, honouring `retry_after` |
| Crash of a task | Supervisor restarts it. Docker/systemd restart the process |
| VPS reboot | Container/service autostarts, resumes from the saved cursor, and catches up on missed transfers |
| Wrong contract / not mainnet | Startup check: non-USDT contract → refuses to start, with a Telegram notice. Non-mainnet genesis → warning |

## 6. Security
- Secrets come only from `.env` / environment variables and are never hardcoded or committed (`.env` is gitignored).
- The log formatter redacts the bot token, API key and key-bearing URL parts from every line, including
  tracebacks. `httpx` URL logging is disabled because Telegram URLs contain the token.
- Telegram messages never include configuration values or secrets.
- Only the admin chat can run commands and receive alerts.

## 7. Configuration reference
See [.env.example](.env.example). Every option is documented there.

## 8. Project layout
```
app/
  main.py              entrypoint, wiring, supervision, graceful shutdown
  config.py            .env parsing and validation
  tron_client.py       TRON HTTP API client (retries, rate limits, latency)
  tron_monitor.py      detection engine: account/blocks modes, backfill, processor
  transaction_parser.py strict parsing: TronGrid records + raw Transfer event logs
  tron_address.py      Base58Check <-> hex (stdlib only)
  filters.py           exact amount handling + acceptance filter
  database.py          Repository interface + SQLite implementation
  telegram_bot.py      Telegram client, alert dispatcher, admin-only commands
  formatting.py        alert / status / wallet messages
  startup_checks.py    mainnet + on-chain USDT contract verification
  logger.py            structured (text/json) logging with secret redaction
  healthcheck.py       Docker HEALTHCHECK (heartbeat freshness)
tests/                 88 tests: filters, parser, duplicates, monitors, retries, Telegram, startup checks
```

### Moving to PostgreSQL later
All storage goes through the `app.database.Repository` interface. Implement it for PostgreSQL
(the same two tables; `amount_usdt` as `NUMERIC(20,6)`; `INSERT … ON CONFLICT (tx_hash) DO NOTHING`)
and return it from `create_repository()` for `postgresql://` URLs.

### Known limitation
`tx_hash` is UNIQUE, as specified. If one transaction contains **two** matching USDT transfers
involving the wallet (very rare, e.g. a batch payout contract), only the first one alerts.
