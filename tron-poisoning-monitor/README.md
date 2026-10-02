# TRON Address-Poisoning Monitor

A **read-only** bot that detects successful address-poisoning attacks on TRON and alerts on
Telegram when a wallet **sends funds to a look-alike of an address it used before**. That payment
is the moment an address-poisoning attack succeeds.

It works in two layers at the same time:

* **Network-wide (default, `NETWORK_WIDE=true`)**: every USDT transfer on TRON is checked. The
  bot remembers who paid whom over the last `NETWORK_MEMORY_DAYS` (default 7) and alerts when
  *any* wallet pays a look-alike of an address it recently paid. No `/add` is needed.
* **Watched wallets (`/add`)**: for wallets you care about most, the bot also loads their
  complete history (not just the last few days), keeps it forever, and alerts at any amount.

```
Victim → TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c   (12 payments over 5 months)
Victim → TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c   25,000 USDT  ← new recipient, same "TLegit…Wr2c" look
         🚨 SUCCESSFUL ADDRESS POISONING DETECTED  (confidence 98/100, ~20 ms after the block was read)
```

The bot **never** holds, requests or handles private keys, and never builds, signs or broadcasts
transactions. It only calls read endpoints, and a test (`test_codebase_is_read_only`) fails the
build if signing or broadcast code is ever added.

---

## Contents

1. [Important: the USDT contract address](#1-important-the-usdt-contract-address)
2. [How detection works](#2-how-detection-works)
3. [Project structure](#3-project-structure)
4. [Configuration (.env)](#4-configuration-env)
5. [Deploy on a VPS (Docker)](#5-deploy-on-a-vps-docker)
6. [Telegram: setup, first wallet, commands, buttons](#6-telegram)
7. [Example successful-poisoning alert](#7-example-successful-poisoning-alert)
8. [Simulation mode](#8-simulation-mode)
9. [Tests](#9-tests)
10. [Speed and latency](#10-speed-and-latency)
11. [Scaling, restart safety, deduplication](#11-scaling-restart-safety-deduplication)
12. [Database](#12-database)
13. [Security](#13-security)
14. [Limitations (including the TRON API)](#14-limitations)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Important: the USDT contract address

The specification gave `TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7` as the USDT contract. **That is not
Tether USDT.** The official USDT TRC-20 contract on TRON mainnet is
**`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`** (6 decimals), and that is the default here. Monitoring
the other contract would miss every real USDT payment.

Tokens are configurable: `TOKENS=SYMBOL:CONTRACT:DECIMALS[,…]`. The architecture is token-generic
(aggregates, incidents and traces store the contract), but only USDT is enabled by default.
`python -m app.main check` warns if the configured "USDT" contract is not the official one.

---

## 2. How detection works

### Event classes

| Event | Meaning | Telegram |
|---|---|---|
| `POISONING_ATTEMPT` | Look-alike activity, but the victim did **not** send funds: incoming dust from a look-alike, or a zero-value `transferFrom(victim → look-alike, 0)` spoof | recorded; optional (`NOTIFY_ATTEMPTS`) |
| `POISONING_CANDIDATE` | Victim **sent funds** to a look-alike; evidence is below `CONFIDENCE_THRESHOLD` and is investigated further (may be upgraded) | optional (`NOTIFY_CANDIDATES`) |
| `SUCCESSFUL_POISONING_EVENT` | Victim **sent funds** to a look-alike and the combined evidence crosses the threshold | **🚨 immediate alert** |

A tiny poisoning transfer is **supporting evidence, never a requirement**. The primary trigger
is the victim's own payment, so attacks are caught even when the dust was sent months before
monitoring started, or in a counterfeit token.

### Pipeline

```
                    every block (~3 s)                            per monitored wallet
 TRON API ──► BlockMonitor ──► filter: token ∈ TOKENS and ──► Ingestor: INSERT … ON CONFLICT DO NOTHING
            (getblockbynum +    from/to ∈ monitored set              (idempotency key = tx hash + transfer)
             txinfo by block)   (in-memory, O(1))                         │ new rows only
                                                                          ▼
     ┌──────────────── one DB transaction (exactly-once) ──────────────────────────────┐
     │ claim tx (PENDING→) · recipient known? · indexed look-alike candidate lookup   │
     │ · similarity engine · local evidence (dust seen, other victims, labels)        │
     │ · risk engine → event + evidence + similarity rows + Telegram outbox + jobs    │
     │ · update victim→recipient aggregates                                            │
     └─────────────────────────────────────────────────────────────────────────────────┘
          │ wake                               │ wake
          ▼                                    ▼
     AlertWorker → Telegram 🚨           JobWorker: INVESTIGATE (suspicious address history,
     (retries, never drops)                         dust campaign, funding, account age, label → re-score)
                                                    TRACE (5 hops, forwarding evidence, re-traces)
```

The alert does **not** wait for investigation or tracing. Those run afterwards and post
"🧾 EVIDENCE UPDATE" and "🔎 FUND TRACE" follow-ups. A candidate whose investigation pushes it
over the threshold is upgraded and the 🚨 alert is sent at that point.

### Historical counterparty database

When a wallet is added (`/add`), its **complete USDT history** is fetched page by page (as far back
as the API allows, or `HISTORY_DAYS`) and stored. Every outgoing payment updates
`historical_recipients` exactly once:

`transaction_count, total_amount, first_seen, last_seen, largest_amount, smallest_amount, average_amount`
(+ folded prefix/suffix keys for indexed look-alike lookup, and a `flagged_suspicious` marker).

After the scan, a **retrospective analysis** replays the history chronologically with a running
aggregate, so past poisonings are found as well. They are reported in one "📚 HISTORY SCAN
COMPLETE" summary rather than as individual alerts. Live monitoring is active immediately after
`/add`. A payment made while the scan is still running is re-checked against the full history
when the scan finishes, and is alerted if it qualifies.

### Address similarity (`app/services/similarity.py`)

Addresses are first normalised (`app/utils/address.py`): Base58Check is decoded and
checksum-verified, and hex (`41…`, `0x…`, 32-byte ABI topics) is converted to Base58. Formatting
differences can therefore never produce or hide a match, and stored addresses are never changed.
Base58 is case-sensitive, so case is never altered.

| Metric | Description |
|---|---|
| `prefix_match_length` / `suffix_match_length` | identical characters at start/end. The leading `T` is identical for every TRON address and is **excluded** |
| fuzzy prefix/suffix | also counts case differences and confusable characters (`1/i 5/s 2/z 8/B 9/g`) as half-matches |
| `prefix_similarity` / `suffix_similarity` | effective edge match ÷ visible window (`SIMILARITY_PREFIX_WINDOW`, default 5) |
| `overall_similarity` | normalised Levenshtein similarity of the 33-character body |
| `positional_similarity` | share of equal characters at equal positions |
| `coincidence_log10` | log10 chance that a random address shares these edges (58⁻ⁿ); 5+4 chars ≈ 10⁻¹⁶ |
| `similarity_score` | weighted combination (weights configurable) |

A match requires **both edges** (`MIN_PREFIX_MATCH` and `MIN_SUFFIX_MATCH`, default 3 + 4 after the
leading T, i.e. the first 4 characters a wallet displays and the last 4), or one
very long edge (`SINGLE_EDGE_MIN_MATCH`, default 7), **and** `similarity_score ≥ MIN_SIMILARITY_SCORE`.
A shared 4-character prefix alone, or a shared suffix alone, never matches.

### Risk / confidence engine (`app/services/risk_engine.py`)

The score is additive and capped to 0-100. The full breakdown is stored with every incident and
printed in the report. Defaults are listed below; override any of them with
`RISK_WEIGHTS='{"recipient_new": 25}'`.

| Signal | Points |
|---|---|
| Legitimate recipient used ≥2 / ≥5 / ≥10 times | +10 / +4 / +3 |
| Victim → legitimate total ≥ `LEGIT_SUBSTANTIAL_TOTAL_USDT` (10k) | +5 |
| Legitimate recipient used within `LEGIT_RECENT_DAYS` (180) | +5 |
| Suspicious recipient never paid before | +20 |
| Test payment to real address → look-alike dust within `RAPID_DUST_MINUTES` → victim pays look-alike within `RAPID_PAYMENT_HOURS` | +30 |
| Repeat payment to an address already flagged (repeat loss) | +25 |
| Both edges match | +30 |
| One long edge matches | +10 |
| Similarity score ≥ `VERY_HIGH_SIMILARITY` (0.85) | +10 |
| Amount ≥ 1,000 / ≥ 10,000 USDT | +5 / +3 |
| Amount consistent with earlier payments to the legitimate recipient | +5 |
| Dust or zero-value transfer between the suspicious address and the victim | +15 |
| Suspicious address dusted ≥ `MULTI_VICTIM_MIN` wallets | +10 |
| Other monitored wallets also paid the suspicious address | +10 |
| Suspicious address was fresh (≤ 3 prior transfers) | +5 |
| Funded by ≥ `MANY_SENDERS_MIN` different wallets | +5 |
| Forwarded ≥ 50% within `FORWARD_WINDOW_MINUTES` | +5 |
| **Negative:** victim already paid the suspicious address | −40 |
| **Negative:** it is an established counterparty (≥3 payments / large volume) | −25 |
| **Negative:** public exchange/service label | −25 |
| **Negative:** transfer not signed by the victim (spender / `transferFrom`) | −30 |
| **Negative:** weak legitimate relationship (one small payment) | −10 |
| **Negative (history only):** victim kept paying the address afterwards | −30 |

The score alone cannot produce a SUCCESSFUL event when the amount is below
`MIN_VICTIM_AMOUNT_USDT`, when the "legitimate" recipient is not established
(`MIN_LEGIT_TX_COUNT` / `MIN_LEGIT_TOTAL_USDT`), or when the victim did not sign the transfer.
Those cases stay CANDIDATE. Wording is always "possible" / "high-confidence"; the bot never says
"confirmed scam".

---

### The attack pattern the bot follows

1. **Planting:** a look-alike address appears in the victim's history through one of:
   a **fake "USDT" token** transfer, a **tiny TRX** transfer, **tiny real USDT**, or a **zero-value
   USDT `transferFrom`**. The bot records every such "contact" on the whole network (kept 7 days).
2. **Copying:** the victim later pays that look-alike real USDT. Because the payee touched the
   victim's history before, the bot fetches the victim's **real payment history from TronGrid**
   (it does not depend on how long the bot has been running), finds the address the fake imitates,
   and runs the full scoring → 🚨 alert.
3. **Timing:** the most common real sequence is *victim makes a small test payment to the real
   address → the look-alike dusts the victim within seconds/minutes → the victim copies the
   look-alike for the main payment*. That sequence is scored explicitly (+30, `rapid_poisoning_sequence`,
   `RAPID_DUST_MINUTES` / `RAPID_PAYMENT_HOURS`), so a single test payment to the real address is
   enough. Below-threshold cases are still sent as 🟠 POSSIBLE (`NOTIFY_CANDIDATES=true`).
4. The look-alike rule matches what wallets display: by default the **first 4 characters including
   the `T`** (`MIN_PREFIX_MATCH=3` after the T) **and the last 4** (`MIN_SUFFIX_MATCH=4`).

### Network-wide mode in detail (`app/services/network_scanner.py`)

Per block (≈3 s, measured ≈95 ms of processing for 150 transfers on PostgreSQL):

1. One indexed query loads, for every sender in the block, its remembered recipients that share a
   folded 3-character prefix or suffix with the address being paid (and, for dust, with the dust
   sender).
2. The similarity engine checks those few candidates in memory.
3. Look-alike payments go through the full detector: incident, confidence score, 🚨 alert,
   investigation and fund trace.
4. Dust and zero-value transfers from look-alikes are stored as evidence. A later payment to that
   look-alike then scores "poisoning transaction observed: YES" (+15).
5. All other payments are bulk-added to the payment memory, in the same DB transaction that
   advances the block cursor, so restarts never double count.

| Setting | Default | Meaning |
|---|---|---|
| `NETWORK_WIDE` | `true` | check every USDT transfer (block mode only) |
| `NETWORK_MEMORY_DAYS` | `7` | how long payments of non-watched wallets are remembered (pruned hourly) |
| `NETWORK_MIN_ALERT_USDT` | `100` | network-wide alerts only from this amount; smaller cases are still recorded |

Disk: about 825 bytes per remembered sender→recipient pair (measured, including indexes), so
expect a few GB for 7 days of TRON USDT traffic. Check with `df -h` and lower
`NETWORK_MEMORY_DAYS` on small disks. The memory starts empty, so in the first days a victim's
earlier payments may not be known yet. Wallets added with `/add` don't have this limitation.

## 3. Project structure

```
tron-poisoning-monitor/
├── app/
│   ├── main.py                    # Application wiring + CLI (run / migrate / check / simulate / health)
│   ├── config.py                  # all settings, thresholds and risk weights (env / .env)
│   ├── database.py                # engine, SQL migration runner, DB retry helpers
│   ├── domain.py                  # TokenTransfer (+ idempotency key), enums
│   ├── repository.py              # idempotent inserts, aggregates, outbox, jobs
│   ├── models/__init__.py         # SQLAlchemy models (13 tables)
│   ├── services/
│   │   ├── tron_service.py        # read-only TronGrid client: blocks, history, tx, account, labels
│   │   ├── transaction_monitor.py # BlockMonitor (default), AccountMonitor, Ingestor, gap fill
│   │   ├── history_service.py     # initial history scan (resumable) + summary
│   │   ├── similarity.py          # address similarity engine
│   │   ├── risk_engine.py         # confidence scoring
│   │   ├── poisoning_detector.py  # exactly-once detection, incidents, retrospective analysis
│   │   ├── network_scanner.py     # network-wide detection over every USDT transfer + memory pruning
│   │   ├── investigator.py        # on-chain evidence discovery + re-scoring / upgrades
│   │   ├── fund_tracer.py         # multi-hop follow-the-money
│   │   ├── labels.py              # operator label file / cache / TronScan public tags
│   │   ├── notifier.py            # incident → Telegram outbox rows
│   │   ├── report_service.py      # alert, evidence packet, investigator report, JSON, X draft
│   │   ├── telegram_service.py    # Bot API, admin commands, buttons
│   │   ├── admin_service.py       # add/remove/pause/resume wallets
│   │   └── x_service.py           # optional X API v2 posting (manual approval only)
│   ├── workers/
│   │   ├── alert_worker.py        # outbox delivery with retries
│   │   ├── job_worker.py          # HISTORY / INVESTIGATE / TRACE jobs
│   │   └── maintenance.py         # confirmations, pending recovery, system_logs, heartbeat
│   ├── simulation/                # in-memory TRON chain, scenarios, end-to-end runner
│   └── utils/                     # address, amounts (integer only), logging (redaction), clock, rate limiter
├── migrations/001_initial_schema.sql · 002_network_wide.sql · 003_network_contacts.sql
├── data/address_labels.json       # operator-curated labels (optional)
├── docs/example-output/           # outputs of the final simulation
├── tests/                         # 85 tests
├── Dockerfile · docker-compose.yml · .env.example
├── requirements.txt · requirements-dev.txt · pyproject.toml · pytest.ini
```

---

## 4. Configuration (.env)

```bash
cp .env.example .env
nano .env
```

Minimum to fill in:

| Variable | Value |
|---|---|
| `TRON_API_URL` | your provider's HTTP API (TronGrid-compatible), e.g. `https://api.trongrid.io` |
| `TRON_API_KEY` | your API key |
| `TRON_API_KEY_HEADER` | header name for the key (`TRON-PRO-API-KEY` for TronGrid) |
| `TRON_RATE_LIMIT_RPS` | your plan's request rate minus a margin (default 10) |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_ADMIN_CHAT_ID` | your numeric Telegram user id (comma-separate several) |
| `POSTGRES_PASSWORD` | a long random password |

Everything else has production defaults; see `.env.example` (thresholds, risk weights, trace
depth, X credentials). Every threshold is an environment variable, so tuning never requires
code changes. Restart after editing: `docker compose up -d`.

---

## 5. Deploy on a VPS (Docker)

Tested design target: any 1 vCPU / 1-2 GB RAM Linux VPS.

```bash
# 1. Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# 2. Code
git clone <this repository> && cd SpTronFinal/tron-poisoning-monitor

# 3. Configuration
cp .env.example .env && nano .env            # API key, Telegram token + your user id, DB password

# 4. Optional: verify API access and the token contract (read-only)
docker compose run --rm monitor python -m app.main check

# 5. Start (PostgreSQL + monitor; migrations run automatically)
docker compose up -d

# 6. Logs / health
docker compose logs -f monitor
docker compose ps                            # "healthy" once blocks are being processed
```

* Restart policy `unless-stopped`: the stack comes back after a VPS reboot (enable Docker:
  `systemctl enable docker`).
* Update: `git pull && docker compose up -d --build`.
* Backups: `docker compose exec db pg_dump -U tron tron_poison > backup.sql`.
* Reports written via **📄 FULL REPORT** are also saved to `./output/cases/<CASE_ID>/`.
* Redis is intentionally not used. The alert outbox, background jobs and pending analysis live in
  PostgreSQL next to the incidents, so a restart cannot lose or duplicate them.

Without Docker: Python 3.11+, PostgreSQL 14+, then `pip install -r requirements.txt`, set
`DATABASE_URL=postgresql+asyncpg://…`, and run `python -m app.main`. Run a single instance per
database.

---

## 6. Telegram

### Setup

1. Create a bot with **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Get your numeric user id (e.g. message **@userinfobot**) → `TELEGRAM_ADMIN_CHAT_ID`.
3. `docker compose up -d`, open your bot, and send `/start`.

Only ids in `TELEGRAM_ADMIN_CHAT_ID` can use commands or buttons. Anyone else gets
"⛔ not authorized" (at most once per hour) and is recorded in `telegram_users`. If you put a
**group** id there, every member of that group is an administrator.

### Add the first wallet

```
/add TXyz…your wallet address… Treasury
```

* Base58 (`T…`) and hex (`41…`) are accepted; input is checksum-validated.
* Live monitoring starts immediately; the historical scan runs in the background and ends with a
  "📚 HISTORY SCAN COMPLETE" message listing the top recipients and any historical incidents.

### Commands

| Command | Action |
|---|---|
| `/start`, `/help` | introduction / command list |
| `/add <ADDRESS> [label]` | monitor a wallet |
| `/remove <ADDRESS>` | stop monitoring (history is kept) |
| `/list` | monitored wallets, history status, recipient counts |
| `/pause` · `/resume` | pause / resume **alert delivery** globally (monitoring continues; alerts queue and are delivered on resume) |
| `/pause <ADDRESS>` · `/resume <ADDRESS>` | silence / re-enable alerts for one wallet (data is still collected) |
| `/status` | head/processed block, lag, network-wide scan counters, API usage per day, queues, incident counts, alert latency |
| `/cases`, `/case <CASE_ID>` | recent incidents / one incident with buttons |
| `/recipients <ADDRESS>` | historical recipient database of a wallet |

### Alert buttons

`[📋 COPY CASE]` sends the evidence packet as a copyable block ·
`[🔎 TRACE FUNDS]` re-runs the trace now ·
`[📄 FULL REPORT]` sends the investigator report (`.md`) + case file (`.json`) ·
`[🐦 PREPARE X POST]` sends a draft with `[📤 POST TO X]` (only if `X_ENABLED=true`) and `[CANCEL]`.

**Nothing is ever posted to X automatically.** Posting requires `X_ENABLED=true`, all four OAuth
1.0a user-context credentials, and an administrator pressing **📤 POST TO X**. Drafts are checked
for over-claiming wording ("confirmed scam", "scammer", "100%", …) and for the 280-character limit.

---

## 7. Example successful-poisoning alert

Produced by the final simulation (`docs/example-output/telegram_alert.txt`). In Telegram the
links are clickable and the four buttons appear below the message.

```
🚨 SUCCESSFUL ADDRESS POISONING DETECTED

Case: TRON-POISON-20261001-000002
Network: TRON
Token: USDT TRC-20

Victim:
TVictimxw466QvBptjwLHfEf3ekBSMZkyw

Victim sent:
25,000 USDT

Legitimate historical recipient:
TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c

Suspicious recipient:
TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c

Recipient similarity: 77%  (prefix 5 / suffix 4 chars after T)
Historical legitimate recipient: 12 previous transactions (287,950 USDT)
Suspicious recipient history: 0 previous transaction(s) from victim
Confidence: 98/100
Block: 77520000 (unconfirmed)
Block time: 2026-10-01 13:02:36 UTC

Transaction: 45c7f2bbdbfd5473…            → https://tronscan.org/#/transaction/<hash>
Victim: TVictimx…SMZkyw                   → https://tronscan.org/#/address/<address>
Legitimate recipient: TLegit9d…TSWr2c
Suspicious recipient: TLegitVx…BQWr2c

Detection time: 2026-10-01 13:02:36.412 UTC
Latency: block→detected 2 ms, detected→analysed 22 ms

Evidence:
• Victim repeatedly used legitimate recipient (12 previous payments)
• New recipient strongly resembles legitimate recipient
• Victim sent funds to the look-alike recipient
• Suspicious recipient had prior dust interaction: YES

Status:
⚠️ POSSIBLE SUCCESSFUL ADDRESS-POISONING ATTACK

[📋 COPY CASE] [🔎 TRACE FUNDS] [📄 FULL REPORT] [🐦 PREPARE X POST]
```

Follow-ups from the same run: an evidence update raising confidence to 100/100 (dust campaign
against 7 wallets, 99% forwarded after 45 s), then the fund trace
`Suspicious → hop 1 → hop 2 → hop 3 → Binance-Hot (simulated public tag) (possible exchange/service attribution)`.
See `docs/example-output/` for the evidence packet, investigator report, JSON case file, trace
and X draft.

---

## 8. Simulation mode

```bash
python -m app.main simulate                   # or: SIMULATION_MODE=true python -m app.main
docker compose run --rm monitor python -m app.main simulate
```

The simulation uses an in-memory TRON chain with time-based block numbers, solidification lag,
TronGrid-style pagination, missing block numbers in the history endpoint, transaction signers and
injectable outages. It never touches the real blockchain. Scenario:

1. Victim `TVictim…` pays `TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c` 12 times over 150 days.
2. The attacker sends 0.000001 USDT from look-alike `TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c` to the
   victim and six other wallets.
3. The wallet is added through the same code path as `/add`; the history scan and retrospective run.
4. Live: victim → look-alike **25,000 USDT** → `SUCCESSFUL_POISONING_EVENT` → Telegram alert.
5. Funds move over 4 hops to a labelled exchange deposit address → investigation + trace.
6. COPY CASE / FULL REPORT / PREPARE X POST are pressed.

Output goes to `output/simulation/`: `telegram_alert.txt`, `evidence_packet.txt`,
`investigator_report.md`, `case.json`, `x_post.txt`, `fund_trace.txt`, `latency.json`,
`summary.json`. Add `--telegram` to also deliver the simulated messages to your real Telegram
chat (needs the token and admin id in `.env`).

The look-alike addresses are real, checksum-valid Base58Check strings found with a small vanity
search over encodings, the same technique attackers use with GPUs. No private keys exist for them.

---

## 9. Tests

```bash
pip install -r requirements-dev.txt
pytest                                                    # SQLite
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/tron_test pytest   # real PostgreSQL + SQL migrations
```

Results at delivery: **85 passed on PostgreSQL 16** (each test starts from an empty schema built
by `migrations/*.sql`, plus a check that the migrations match the ORM models) and **84 passed /
1 skipped on SQLite** (the skipped test is the PostgreSQL-only schema check). `ruff check` is clean.

| # | Required scenario | Test |
|---|---|---|
| 1 | Normal repeated recipient | `test_normal_repeated_recipient_updates_stats_without_alert` |
| 2 | New unrelated recipient | `test_new_unrelated_recipient_is_not_alerted` |
| 3 | Similar recipient but legitimate | `test_similar_recipient_that_victim_already_uses_is_not_alerted` |
| 4/5 | Prefix-only / suffix-only similarity | `test_prefix_only_and_suffix_only_similarity_do_not_alert`, `test_prefix_only_and_suffix_only_do_not_match` |
| 6 | Strong prefix + suffix | `test_strong_prefix_and_suffix_match` |
| 7 | Victim sends to suspicious | `test_victim_payment_to_lookalike_is_successful_event_with_alert` |
| 8/9 | Dust exists / does not exist | `test_dust_is_supporting_evidence_not_a_requirement` |
| 10 | Duplicate transaction | `test_duplicate_transaction_never_duplicates_incident_or_alert` |
| 11 | API outage | `test_api_outage_does_not_crash_or_skip_blocks`, `test_trongrid_client_retries_backoff_and_errors` |
| 12 | Telegram outage | `test_telegram_outage_alert_is_retried_and_delivered_once` |
| 13 | Database restart / outage | `test_database_outage_is_retried_without_losing_the_transfer`, `test_analysis_crash_is_recovered_from_pending_state` |
| 14 | Bot restart | `test_restart_resumes_cursor_recovers_pending_and_never_duplicates`, `test_history_scan_resumes_after_interruption` |
| 15 | Large amount | `test_large_amount_exact_integer_handling` (2,000,000,000.123456 USDT, exact) |
| 16 | Very small amount | `test_very_small_amount_is_candidate_not_successful` |
| 17 | Multiple victims, same suspicious address | `test_multiple_victims_same_suspicious_address` |
| 18 | Suspicious address forwarding funds | `test_forwarding_is_traced_and_recorded` |
| 19 | Multiple look-alike recipients | `test_multiple_lookalike_recipients` |
| 20 | Different token ignored | `test_different_token_is_ignored` |

Additional tests cover zero-value `transferFrom` spoofs, third-party-signed transfers, historical
(retrospective) detection, a payment made during the history scan, candidate → successful
upgrades, per-wallet and global pause, the full concurrent run loop, Telegram authorization and
commands, all buttons, X manual approval, report wording (no over-claiming, facts vs analysis),
read-only enforcement, secret redaction, the rate-limiter priority, block/log decoding and
address/amount handling.

---

## 10. Speed and latency

Every incident stores `detected_at`, `analysis_started_at`, `analysis_completed_at` and
`alert_sent_at`, plus `chain_latency_ms` (block timestamp → transfer seen),
`detection_latency_ms` (seen → analysis complete) and `alert_latency_ms` (seen → Telegram
accepted the message). `/status` shows the median and maximum alert latency.

What to expect in production:

| Stage | Typical | Determined by |
|---|---|---|
| block produced → block readable via API | ~0.5-3 s | TRON block time (3 s), provider propagation, head polls timed to the 3 s block schedule |
| block fetched → analysis complete | ~10-50 ms | local DB work (measured 22 ms in the simulation) |
| analysis → Telegram accepted | ~100-400 ms | Telegram API round-trip from your VPS |

So a realistic end-to-end figure is **~1-4 s after the block is produced**. The simulation's
sub-100 ms figures exclude network round-trips and are labelled as such in `latency.json`. The
alert is sent on the first, unconfirmed sighting. A "✅ confirmed" / "↩️ not confirmed" follow-up
is sent if the transaction is later dropped (solidification takes ~19 blocks ≈ 1 min).

---

## 11. Scaling, restart safety, deduplication

* **Constant-cost monitoring**: block mode reads each block once (2 requests per 3 s) and filters
  in memory against a hash set of monitored wallets. 10 or 10,000+ wallets cost the same number of
  API calls. `MONITOR_MODE=account` (per-wallet polling) exists for small setups only.
* **No full-history comparisons**: a new recipient is compared only with that victim's recipients
  sharing a folded 3-character prefix **or** suffix key, via composite indexes
  (`victim, token, prefix_key` / `suffix_key`). Established counterparties (≥3 payments) are not
  re-analysed at all.
* **Rate limiting**: one token bucket for all TRON calls with priorities
  (live > detection > investigation > history), exponential backoff with jitter, `Retry-After`,
  timeouts, pooled keep-alive connections. API failures never crash the process.
* **Idempotency**: each transfer has a source-independent key
  `sha256(tx_hash | token | from | to | amount | occurrence)`, with `tx_hash` indexed. Block
  scans, history pages, gap fills and replays all agree on it. Incidents are unique per
  `(transaction, victim)`, alerts per `dedup_key`, evidence per `(event, evidence_key)`, traces per
  `(event, run, transfer)`.
* **Exactly-once analysis**: the transfer is claimed with a conditional UPDATE inside the same DB
  transaction that writes the incident, its alert rows, its jobs and the aggregate update. A crash
  rolls everything back, and `RecoveryWorker` / startup recovery re-analyse `PENDING` transfers.
* **Restart**: the block cursor is persisted after every block, and processing resumes at the next
  block. Gaps longer than `MAX_BLOCK_CATCHUP` blocks (default 1 hour) are filled through
  per-wallet history queries. History scans resume from their saved cursor. Jobs left `RUNNING`
  are reset. The Telegram update offset is persisted.
* **Delivery guarantee**: alerts are at-least-once with dedup. The only theoretical duplicate is a
  crash in the milliseconds between Telegram accepting a message and the row being marked `SENT`.

---

## 12. Database

PostgreSQL in production (`migrations/001_initial_schema.sql`, applied automatically and recorded
in `schema_migrations`). Amounts are `NUMERIC(78,0)` integer base units; floating point is never
used for money.

| Table | Purpose |
|---|---|
| `watched_wallets` | monitored wallets, status, history-scan progress/cursor |
| `historical_recipients` | per victim→recipient aggregates (count, total, first/last, largest/smallest/average) |
| `transactions` | every transfer involving a monitored wallet (idempotent), analysis state, confirmation |
| `address_similarity_matches` | all similarity measurements that matched |
| `poisoning_events` | incidents (case id, class, confidence, breakdown, latency metrics) |
| `poisoning_evidence` | facts and analytical findings per incident |
| `fund_traces` | trace hops per incident and trace run |
| `alerts` | Telegram outbox (dedup key, retries, delivery time) |
| `jobs` | durable background work |
| `telegram_users` | everyone who interacted; unauthorized attempts |
| `system_logs` | WARNING+ logs and audit events (wallet added/removed, pause, incidents) |
| `monitor_state` | block cursor, Telegram offset, global pause |
| `x_post_drafts` · `address_labels` | X drafts and their status · label cache |

Case ids look like `TRON-POISON-20261001-000123` (UTC date + globally unique sequence).

---

## 13. Security

* Read-only by construction: no key material, no signing or broadcast code (enforced by a test).
* Secrets only come from the environment, and every log line (including tracebacks and
  third-party library logs, such as Telegram URLs that embed the bot token) is redacted.
* All user input is validated: addresses are checksum-verified, labels are sanitised and
  length-limited, callback data is parsed strictly, and HTML is escaped in messages.
* Only administrator ids can modify monitored wallets or press buttons.
* The container runs as an unprivileged user. PostgreSQL is not exposed outside the compose network.

---

## 14. Limitations

**TRON API (TronGrid-compatible HTTP)**

* **No WebSocket/event subscription** on TronGrid. The fastest option is block polling; TRON's
  native event plugins (Kafka/ZMQ/MongoDB) require running your own full node. The data source is
  an interface (`TronDataSource`), so a node-plugin source can be added without touching detection.
* The history endpoint (`/v1/accounts/{address}/transactions/trc20`) returns at most 200 rows per
  page, carries **no block number and no signer**, and its depth/retention depends on the
  provider. Very active wallets are capped by `HISTORY_MAX_TRANSFERS` (the summary says so and
  `poisoning_tx_observed` becomes `UNKNOWN` rather than `NO`). Block numbers and signers of
  incident transactions are resolved separately.
* The block endpoints return the **unconfirmed** head. Alerts are sent before solidification
  (~1 min) and a follow-up is sent if the transaction is dropped. A transfer that appears only in
  a replacement block after a rare fork could be missed; transfers that are dropped are detected.
* Free API keys have tight rate limits. History scans and traces are throttled behind live
  monitoring, so adding many large wallets at once takes time.
* This sandbox could not reach `api.trongrid.io` (outbound proxy returned 403), so the client was
  verified against TronGrid's documented response formats (unit tests with recorded-shape
  payloads and HTTP mocks), not against live mainnet. Run `python -m app.main check` on your VPS
  first; it reads the head block and decodes live transfers.

**Analytical**

* Blockchain data does not prove control, identity or intent. Every output says so, and
  attributions are "possible exchange/service attribution" from public labels only.
* A legitimate new address that happens to look like an old one produces the same pattern. The
  model weighs this (history, signer, amount, later reuse), but the result remains a confidence
  score.
* Only the configured token is analysed. Dust in counterfeit tokens or TRX is not seen (it is not
  required for detection), and the tracer follows the token, not swaps or TRX.
* Fund tracing uses a FIFO / largest-outflow approximation over `TRACE_HOPS` hops; mixers,
  swaps and exchange-internal movements break or blur the trail.
* Run one monitor instance per database. Row locks make concurrent instances safe for
  correctness, but they would duplicate API usage.

---

## 15. Troubleshooting

| Symptom | Fix |
|---|---|
| `TRON_API_UNAVAILABLE … 401/403` | Wrong `TRON_API_KEY` / `TRON_API_KEY_HEADER`, or the VPS blocks outbound HTTPS. Run `python -m app.main check`. |
| `TRON_API_RETRY … 429` | Lower `TRON_RATE_LIMIT_RPS` or upgrade the API plan. |
| `/status` lag keeps growing | API too slow or rate-limited; raise the rate limit or `BLOCK_PREFETCH`. Gaps beyond `MAX_BLOCK_CATCHUP` are filled per wallet. |
| No Telegram messages | Check the token, that you sent `/start` to the bot, and that `TELEGRAM_ADMIN_CHAT_ID` is your numeric id. Undelivered alerts stay queued (`/status` → queued alerts). |
| "not authorized" | Your id is not in `TELEGRAM_ADMIN_CHAT_ID`. |
| Container `unhealthy` | No successful block poll for 3 minutes; see `docker compose logs monitor`. |
