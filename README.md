# SpTronFinal

**[`tron-usdt-wallet-monitor/`](tron-usdt-wallet-monitor/)**: a read-only TRON USDT TRC-20 monitor.
It auto-discovers every wallet that a root wallet (Wallet A) sends USDT to, then sends a Telegram
alert when any discovered wallet receives ≥ 500 USDT from any sender. It runs on PostgreSQL and
Docker Compose. See [tron-usdt-wallet-monitor/README.md](tron-usdt-wallet-monitor/README.md).

**[`tron-usdt-pattern-monitor/`](tron-usdt-pattern-monitor/)** – a read-only, 24/7 TRON USDT TRC-20
monitor that learns TEST → LARGE transfer patterns per Sender → Recipient relationship,
builds an automatic watchlist and sends a Telegram RED alert when a known test transfer
repeats – before the large transfer arrives.

See [tron-usdt-pattern-monitor/README.md](tron-usdt-pattern-monitor/README.md) for setup,
deployment (Docker / systemd), simulation and tests.
