# TRON Wallet Funding Bot

A service that sends **TRX** and **USDT (TRC-20)** from a single **Mother Wallet** to
**Target Wallet** addresses that it receives from your internal systems. It runs on
**TRON mainnet only**. There is also a Nile testnet profile for testing. The code has no
path to Ethereum, BSC, Solana or any other chain.

```
Mother Wallet ──TRX──▶ Target Wallet A        Upstream bot ──POST /wallets──▶ Funding Bot
      │      ──USDT─▶ Target Wallet A                                          │
      │      ──TRX──▶ Target Wallet B        Telegram ◀── alerts / commands ───┤
      └──────  USDT─▶ Target Wallet B        Admin API ◀── balance / jobs ─────┘
```

## Key properties

| Concern | How it is handled |
|---|---|
| **Private keys** | The Mother key comes from env or a `*_FILE` secret. Only its derived address is ever printed. Transactions are built **and signed locally**; no key is ever sent to an RPC provider. Target wallet keys are **not needed** and are dropped by default. |
| **Wallet identification vs funding** | `wallet/intake.js` accepts addresses. `wallet/funding.js` sends funds, which needs only the Mother key. |
| **No duplicate funding** | Unique `(address, mode, round)` per job. Repeat funding needs `ALLOW_REPEAT_FUNDING=true` **and** `"repeat": true` in the request. |
| **No double-spend on retry** | Each transaction is written to the DB *before* broadcast. A new transaction for the same job and asset is created only once the previous one is **provably dead**: it failed on chain, or the solidified chain is past its expiration and it is not in the chain. Until then only the *identical* signed transaction is re-broadcast. |
| **Race conditions** | A mutex around *balance check → build → sign → persist → broadcast*. Balance checks count in-flight transactions. A DB lease allows only one instance. |
| **Malicious / buggy RPC** | Transactions are never built by the provider. The bytes are decoded again and compared field by field with the intent before signing. The genesis block is checked at startup. The USDT contract's address, decimals (6) and symbol are checked. |
| **Confirmation** | Waits for the transaction to be **solidified**, with `SUCCESS` receipts. For USDT, the Transfer event must match the expected recipient and amount. |
| **Alerts** | A Telegram outbox that is independent of the funding state. A Telegram outage can never repeat or block a transaction. |
| **Dry run** | `DRY_RUN=true` validates, builds and signs, but never broadcasts. |

## Project layout

```
src/
  index.js                 entry point (startup checks, graceful shutdown)
  app.js                   composition root (dependency wiring)
  config/config.js         env parsing + validation      config/networks.js  TRON network profiles
  security/secrets.js      env / *_FILE secret loading   security/redact.js  log/alert scrubbing
  security/signer.js       Mother Wallet signer (key held privately)
  security/crypto.js       AES-256-GCM (optional target key storage)
  blockchain/http.js       RPC transport: timeouts, retries, 429, throttling
  blockchain/provider.js   provider interface over the TRON HTTP API (swappable)
  blockchain/tx-builder.js local tx construction + byte-level verification
  blockchain/tron.js       chain operations, confirmation & expiry logic
  blockchain/usdt.js       USDT contract verification, balance, energy estimate
  blockchain/units.js      exact BigInt amount math (6 decimals)
  wallet/validation.js     TRON address validation
  wallet/intake.js         WALLET IDENTIFICATION (submissions, idempotency)
  wallet/funding.js        WALLET FUNDING (state machine)
  wallet/planner.js        pure funding calculations
  queue/queue.js           DB-backed queue, concurrency, single-instance lease
  database/database.js     Knex (SQLite default, PostgreSQL supported)
  database/models.js       repository + status enums
  admin/settings.js        runtime settings (pause, amounts) persisted in DB
  admin/admin-service.js   operations shared by Telegram + admin API
  telegram/client.js       Bot API client     telegram/bot.js     commands
  telegram/alerts.js       message formatting + notification dispatcher
  api/server.js            Fastify server (TLS, rate limit)
  api/routes.js            endpoints          api/authentication.js  bearer auth
migrations/                database schema (Knex migrations)
scripts/                   migrate.js, check.js (pre-flight), healthcheck.js
deploy/                    systemd unit, logrotate, nginx reverse proxy
docs/                      SECURITY, DEPLOYMENT, API, TELEGRAM
test/                      node:test suite + in-memory fake TRON chain
```

## Quick start (local, dry run)

Requirements: Node.js ≥ 20.12 (22 LTS recommended).

```bash
cd tron-wallet-funding-bot
npm ci
cp .env.example .env && chmod 600 .env
# edit .env: MOTHER_PRIVATE_KEY, TRON_RPC_URL, TRON_API_KEY, USDT_CONTRACT_ADDRESS,
#            INTERNAL_API_KEY, ADMIN_API_KEY (openssl rand -hex 32), keep DRY_RUN=true
npm run migrate
npm run check      # verifies network, USDT contract, prints Mother address + balances
npm start
```

At startup only the address is shown:

```
Mother wallet:
TXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

Submit a wallet:

```bash
curl -s -X POST http://127.0.0.1:8080/wallets \
  -H "Authorization: Bearer $INTERNAL_API_KEY" -H 'content-type: application/json' \
  -d '{"address":"TXXXXXXXXXXXXXXXXXXXXXXXXXXXX"}'
```

In dry run the log shows:

```
[INFO] [DRY RUN] Would send 5 TRX to TXXXXXXXX...
[INFO] [DRY RUN] Would send 10 USDT to TXXXXXXXX...
```

Set `DRY_RUN=false` only after the dry run and a testnet run look right.

## Funding configuration

| Variable | Meaning |
|---|---|
| `TRX_FUNDING_AMOUNT` / `USDT_FUNDING_AMOUNT` | Amount per wallet. Set either one to `0` to disable that asset. |
| `MAX_TRX_PER_WALLET` / `MAX_USDT_PER_WALLET` | Hard caps. Runtime changes can never go above them. |
| `DAILY_TRX_LIMIT` / `DAILY_USDT_LIMIT` | Rolling 24h spend limits (`0` means unlimited). |
| `USDT_FEE_LIMIT_TRX` | Maximum TRX burned per USDT transfer (the `fee_limit`). The energy estimate is checked against it before sending. |
| `TRX_FEE_BUFFER_TRX`, `MIN_MOTHER_TRX_RESERVE` | TRX kept back for bandwidth and account activation, and a floor that is never spent. |
| `ALLOW_REPEAT_FUNDING` | Allows funding an address again. The request must also send `"repeat": true`. |

The amounts can be changed at runtime with `/set_trx` / `/set_usdt` or
`PUT /admin/settings/funding`. The new values are saved in the DB. Each job records the
amounts in force when it was created. The full list of settings is in [`.env.example`](.env.example).

### Mother Wallet balance rule

Before anything is sent, the bot checks that the Mother Wallet can cover **every asset
the job still needs**, plus fees:

`TRX needed = TRX amount + USDT fee_limit (if USDT is sent) + fee buffer + reserve`

TRX and USDT already committed by in-flight transactions are subtracted first. If the
balance is short, the job is marked `FAILED` before anything is broadcast, an alert is
sent, and funding is **auto-paused**. Top up, `/resume`, then `/retry`.

## Job states

```
RECEIVED → VALIDATING → QUEUED → FUNDING_TRX → FUNDING_USDT → CONFIRMING → COMPLETED
                                     │  transient error  ▲
                                     └──▶ RETRYING ──────┘      permanent error → FAILED
```

Transactions have their own states: `SIGNED` (persisted, broadcast outcome unknown),
`BROADCAST`, `REJECTED`, `CONFIRMED`, `FAILED` (failed on chain), `EXPIRED` (provably
never included) and `DRY_RUN`.

## Testing

```bash
npm test
```

The suite (74 tests) runs against an **in-memory fake TRON chain**
(`test/helpers/fake-chain.js`). The fake chain checks signatures and `raw_data_hex` the
way a node does. It executes transfers, enforces expiration and models solidification.
No test uses mainnet or real funds. Covered: address validation, duplicate detection and
idempotency (including concurrent submissions), funding calculations, insufficient
balance, API authentication, transaction state handling, broadcast timeouts (accepted or
lost), crash recovery, expiry followed by a rebuild, on-chain reverts, retry and backoff,
dry run, private-key redaction, Telegram failures, the overspend race, fake USDT
contracts, and the wrong-network check.

### Testnet (Nile)

```env
TRON_NETWORK=nile
TRON_RPC_URL=https://nile.trongrid.io
USDT_CONTRACT_ADDRESS=<Nile USDT test token, verify on nile.tronscan.org>
USDT_ALLOW_NONSTANDARD_CONTRACT=true   # only if your test token differs from the reference
```

Get test TRX and USDT from the Nile faucet, then run with `DRY_RUN=false` on Nile before
you go to mainnet.

## Further documentation

- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): VPS install, systemd, Docker, HTTPS, logs, monitoring, backups
- [docs/SECURITY.md](docs/SECURITY.md): threat model, key handling, the review checklist
- [docs/API.md](docs/API.md): HTTP API with example requests
- [docs/TELEGRAM.md](docs/TELEGRAM.md): alerts and commands
