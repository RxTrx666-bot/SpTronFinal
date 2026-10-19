# SpTronFinal

**[`tron-usdt-pattern-monitor/`](tron-usdt-pattern-monitor/)** – a read-only, 24/7 TRON USDT TRC-20
monitor that learns TEST → LARGE transfer patterns per Sender → Recipient relationship,
builds an automatic watchlist and sends a Telegram RED alert when a known test transfer
repeats – before the large transfer arrives.

See [tron-usdt-pattern-monitor/README.md](tron-usdt-pattern-monitor/README.md) for setup,
deployment (Docker / systemd), simulation and tests.

**[`tron-wallet-outflow-alert/`](tron-wallet-outflow-alert/)** – a read-only Telegram bot that watches one
TRON wallet for outgoing USDT TRC-20 transfers of specific amounts (default 1.01 / 10 / 20 / 30 ± 0.0001),
verifies each one against the on-chain receipt and Transfer event log, and posts an alert.
See [tron-wallet-outflow-alert/README.md](tron-wallet-outflow-alert/README.md).
