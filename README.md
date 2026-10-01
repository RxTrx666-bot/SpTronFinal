# SpTronFinal

Two independent, read-only TRON USDT TRC-20 monitors:

* **[`tron-poisoning-monitor/`](tron-poisoning-monitor/)**: address-poisoning detection and
  successful-attack alerts. It learns each monitored wallet's historical recipients and raises a
  Telegram 🚨 alert the moment the wallet **sends funds to a look-alike** of an address it used
  before. It also scores confidence from multiple signals, traces the funds over several hops,
  and generates an evidence packet, an investigator report, JSON and an X draft (manual approval).
  See [tron-poisoning-monitor/README.md](tron-poisoning-monitor/README.md).

* **[`tron-usdt-pattern-monitor/`](tron-usdt-pattern-monitor/)**: learns TEST → LARGE transfer
  patterns per Sender → Recipient relationship, builds an automatic watchlist and sends a Telegram
  RED alert when a known test transfer repeats, before the large transfer arrives.
  See [tron-usdt-pattern-monitor/README.md](tron-usdt-pattern-monitor/README.md).
