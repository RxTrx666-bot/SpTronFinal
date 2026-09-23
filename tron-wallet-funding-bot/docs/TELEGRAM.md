# Telegram alerts and commands

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token into
   `TELEGRAM_BOT_TOKEN` (preferably through `TELEGRAM_BOT_TOKEN_FILE`).
2. Add the bot to your private ops group, or message it directly. Get the chat id, for
   example from `https://api.telegram.org/bot<token>/getUpdates` run on your own machine,
   and set `TELEGRAM_CHAT_ID`. Separate several ids with commas.
3. Recommended: set `TELEGRAM_ADMIN_USER_IDS` to the numeric user ids that may run
   commands. Without it, anyone in an authorized chat can run commands.

Messages from chats or users that are not authorized are ignored and logged. Commands
that arrived while the bot was offline are **skipped** at startup, so an old `/retry` or
`/pause` is never replayed.

## Alerts

Alerts are written to an outbox table and delivered separately, with retry and
exponential backoff. If Telegram is down, the funding state is unaffected and the
alert is delivered later.

```
🚨 WALLET FUNDED

Network:
TRON

Wallet:
TXXXXXXXXXXXXXXXXXXXXXXXXXXXX

TRX funded:
5 TRX

USDT funded:
10 USDT

TRX Transaction:
<txid linked to Tronscan>

USDT Transaction:
<txid linked to Tronscan>

Status:
✅ SUCCESS
```

```
❌ FUNDING FAILED

Network:
TRON

Wallet:
TXXXXXXXXXXXXXXXXXXXXXXXXXXXX

Reason:
Insufficient Mother Wallet USDT balance (available 5 USDT, required 10 USDT)

Code: INSUFFICIENT_USDT

Job: 0b5b1f0e-…
```

Other alerts: `FUNDING AUTO-PAUSED` (Mother Wallet empty or daily limit reached),
`FUNDING BOT STARTED`, and `🧪 DRY RUN — WALLET WOULD BE FUNDED` in dry-run mode.

No alert ever contains a private key, an API key or the bot token. All text goes through
the secret scrubber.

## Commands

| Command | Description |
|---|---|
| `/help` | List the commands |
| `/status` | Health: network, Mother address, pause state, DB, RPC, USDT verification, lease, job counts |
| `/balance` | Mother Wallet TRX and USDT balance |
| `/pending` | Jobs in progress or queued (`RECEIVED` … `CONFIRMING`, `RETRYING`) |
| `/completed` | Recently completed jobs |
| `/failed` | Failed jobs with error codes |
| `/job <id\|address>` | Job detail with each transaction, its status and a Tronscan link |
| `/tx <id\|address>` | Same as `/job` |
| `/retry <id\|address>` | Retry a failed job. Assets that are already confirmed are not sent again. |
| `/pause` | Stop creating new transactions. Transactions already broadcast are still tracked to the end. |
| `/resume` | Resume funding |
| `/set_trx <amount>` | Set the TRX amount per wallet for new jobs (capped by `MAX_TRX_PER_WALLET`) |
| `/set_usdt <amount>` | Set the USDT amount per wallet for new jobs (capped by `MAX_USDT_PER_WALLET`). `0` disables it. |
| `/config` | Show amounts, caps, pause, dry-run and repeat-funding settings |

Set `TELEGRAM_COMMANDS_ENABLED=false` to use Telegram for alerts only.
