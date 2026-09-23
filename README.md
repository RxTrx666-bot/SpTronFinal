# SpTronFinal

**[`tron-usdt-pattern-monitor/`](tron-usdt-pattern-monitor/)**: a read-only, 24/7 monitor for
TRON USDT TRC-20 transfers. For each Sender → Recipient relationship it learns TEST → LARGE
transfer patterns, builds a watchlist automatically, and sends a Telegram RED alert when a
known test transfer repeats, before the large transfer arrives.

See [tron-usdt-pattern-monitor/README.md](tron-usdt-pattern-monitor/README.md) for setup,
deployment (Docker / systemd), simulation and tests.

**[`tron-wallet-funding-bot/`](tron-wallet-funding-bot/)**: a TRON mainnet wallet funding
service. It receives target wallet addresses from internal systems (HTTP API) and funds
them with TRX and/or USDT TRC-20 from a Mother Wallet. Transactions are signed locally.
It provides idempotency and crash-safe re-broadcast, a DB-backed queue, Telegram alerts
and commands, an admin API, and a dry-run mode.

See [tron-wallet-funding-bot/README.md](tron-wallet-funding-bot/README.md) and its `docs/`.
