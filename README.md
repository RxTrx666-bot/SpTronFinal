# SpTronFinal

**[`tron-usdt-pattern-monitor/`](tron-usdt-pattern-monitor/)** – a read-only, 24/7 TRON USDT TRC-20
monitor that learns TEST → LARGE transfer patterns per Sender → Recipient relationship,
builds an automatic watchlist and sends a Telegram RED alert when a known test transfer
repeats – before the large transfer arrives.

See [tron-usdt-pattern-monitor/README.md](tron-usdt-pattern-monitor/README.md) for setup,
deployment (Docker / systemd), simulation and tests.

**[`tron-usdt-monitor/`](tron-usdt-monitor/)** – a Telegram bot that monitors wallet
`TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn` and alerts on every outgoing USDT TRC-20 transfer (sent by the wallet)
between 1.000000 and 1.200000 USDT (exact integer range), with on-chain Transfer-event verification,
exact block timestamps, detection latency, duplicate-safe SQLite storage and admin-only commands.

See [tron-usdt-monitor/README.md](tron-usdt-monitor/README.md) for setup and VPS deployment.
