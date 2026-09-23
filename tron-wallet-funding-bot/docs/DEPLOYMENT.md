# Deployment (VPS)

Two supported setups: **systemd** (recommended on a single VPS) or **Docker Compose**.
Both keep running after you close SSH, restart automatically, and survive reboots.

## 0. Before you begin

- Use a dedicated VPS or user. Only the bot and the reverse proxy should run there.
- Keep only an **operating float** in the Mother Wallet, not your whole treasury.
- Get a TronGrid API key (or run your own java-tron node).
- Look up the official USDT TRC-20 contract on https://tronscan.org and verify it.
- Plan the order: dry run on mainnet (`DRY_RUN=true`), then real transfers on Nile, then live mainnet.

## 1. systemd install

```bash
# Node.js 22 LTS (NodeSource or distro package)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs build-essential python3

# service user and directories
sudo useradd --system --home /opt/tron-funding-bot --shell /usr/sbin/nologin fundbot
sudo mkdir -p /opt/tron-funding-bot /etc/tron-funding-bot/secrets /var/lib/tron-funding-bot /var/log/tron-funding-bot
sudo chown fundbot:fundbot /var/lib/tron-funding-bot /var/log/tron-funding-bot
sudo chmod 700 /var/lib/tron-funding-bot /etc/tron-funding-bot/secrets

# code (copy the tron-wallet-funding-bot directory; e.g. git clone then cp)
sudo cp -r tron-wallet-funding-bot/* /opt/tron-funding-bot/
cd /opt/tron-funding-bot && sudo npm ci --omit=dev
sudo chown -R root:root /opt/tron-funding-bot     # code is read-only for the service
```

### Configuration (non-secret)

```bash
sudo cp /opt/tron-funding-bot/.env.example /etc/tron-funding-bot/config.env
sudo chmod 640 /etc/tron-funding-bot/config.env && sudo chown root:fundbot /etc/tron-funding-bot/config.env
sudo nano /etc/tron-funding-bot/config.env
```

Set at least:

```env
TRON_NETWORK=mainnet
TRON_RPC_URL=https://api.trongrid.io
USDT_CONTRACT_ADDRESS=<verified official USDT TRC-20 contract>
TRX_FUNDING_AMOUNT=5
USDT_FUNDING_AMOUNT=10
DRY_RUN=true
DATABASE_URL=sqlite:/var/lib/tron-funding-bot/funding.db
API_HOST=127.0.0.1
API_PORT=8080
TRUST_PROXY=127.0.0.1
TELEGRAM_CHAT_ID=<id>
LOG_FORMAT=json
```

Leave every secret variable **empty** in this file. Secrets are supplied as files, below.

### Secrets

```bash
cd /etc/tron-funding-bot/secrets
sudo sh -c 'umask 077; read -rs -p "Mother private key: " K; echo; printf %s "$K" > mother_private_key'
sudo sh -c 'umask 077; openssl rand -hex 32 > internal_api_key'
sudo sh -c 'umask 077; openssl rand -hex 32 > admin_api_key'
sudo sh -c 'umask 077; read -rs -p "Telegram bot token: " K; echo; printf %s "$K" > telegram_bot_token'
sudo sh -c 'umask 077; read -rs -p "TronGrid API key: " K; echo; printf %s "$K" > tron_api_key'
sudo chmod 600 * && sudo chown root:root *
```

Using `read -s` keeps the key out of your shell history. The unit passes these files to
the service with `LoadCredential=`, so only the service can read them and they never
appear in `systemctl show` or the process environment.

### Database setup

SQLite needs no server. Migrations run automatically at startup. To run them by hand:

```bash
sudo -u fundbot env DATABASE_URL=sqlite:/var/lib/tron-funding-bot/funding.db node /opt/tron-funding-bot/scripts/migrate.js
```

**PostgreSQL** (optional): create a database and a user that owns only that database,
then set `DATABASE_URL=postgres://fundbot:<password>@127.0.0.1:5432/fundbot`. The `pg`
driver is an optional dependency and is installed by `npm ci`. Store the whole URL as a
secret if it contains a password. Run the Knex migrations the same way.

### Pre-flight check, then start

```bash
sudo cp /opt/tron-funding-bot/deploy/systemd/tron-funding-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
# one-off pre-flight check with the same credentials
sudo systemd-run --pty --wait --collect -p User=fundbot \
  -p EnvironmentFile=/etc/tron-funding-bot/config.env \
  -p LoadCredential=mother_private_key:/etc/tron-funding-bot/secrets/mother_private_key \
  -p LoadCredential=tron_api_key:/etc/tron-funding-bot/secrets/tron_api_key \
  -p LoadCredential=internal_api_key:/etc/tron-funding-bot/secrets/internal_api_key \
  -p WorkingDirectory=/var/lib/tron-funding-bot \
  sh -c 'MOTHER_PRIVATE_KEY_FILE=$CREDENTIALS_DIRECTORY/mother_private_key TRON_API_KEY_FILE=$CREDENTIALS_DIRECTORY/tron_api_key INTERNAL_API_KEY_FILE=$CREDENTIALS_DIRECTORY/internal_api_key node /opt/tron-funding-bot/scripts/check.js'

sudo systemctl enable --now tron-funding-bot
sudo systemctl status tron-funding-bot
journalctl -u tron-funding-bot -f
```

`Restart=always` restarts the bot after a crash, and the unit starts at boot. Exit code
78 (invalid configuration) is **not** restarted in a loop: fix the configuration and run
`systemctl restart`.

### Going live

1. With `DRY_RUN=true`, submit one real target address and check the `[DRY RUN] Would send …` log lines and the dry-run Telegram alert.
2. Point a second installation at **Nile** with test funds and `DRY_RUN=false`, then check the transactions on nile.tronscan.org.
3. On mainnet, set `DRY_RUN=false`, keep `DAILY_*_LIMIT` conservative, and `systemctl restart tron-funding-bot`.

Jobs created in dry run are kept separate (`mode=dry_run`). They do **not** block live funding of the same address.

### Upgrades

```bash
sudo systemctl stop tron-funding-bot     # waits for in-flight work (up to 45s)
cp /var/lib/tron-funding-bot/funding.db /var/lib/tron-funding-bot/funding.db.bak-$(date +%F)
# replace code, npm ci --omit=dev
sudo systemctl start tron-funding-bot    # runs migrations, reconciles in-flight transactions
```

After a crash or `kill -9`, the new process waits until the previous single-instance
lease expires (`INSTANCE_LEASE_TTL_SECONDS`, 30s by default). It then **reconciles**
every transaction that was signed or broadcast, using the chain state, before creating
anything new.

## 2. HTTPS

The API binds to `127.0.0.1`. Put nginx (see `deploy/nginx/tron-funding-bot.conf`) or
Caddy in front of it for TLS, and allow only your internal networks. Set `TRUST_PROXY`
to the proxy address so the bot can tell that a request arrived over HTTPS. A
`private_key` field is refused on plain HTTP.

Alternatively, serve TLS directly with `TLS_CERT_FILE` / `TLS_KEY_FILE`. The bot refuses
to start with a non-loopback `API_HOST` and no TLS, unless you set `ALLOW_INSECURE_HTTP=true`.

Firewall: `ufw default deny incoming; ufw allow OpenSSH; ufw allow from <upstream-ip> to any port 443`.

## 3. Docker Compose

```bash
cd tron-wallet-funding-bot
mkdir -p secrets && chmod 700 secrets
# create secrets/mother_private_key, internal_api_key, admin_api_key, telegram_bot_token, tron_api_key
chmod 600 secrets/* && sudo chown 1000:1000 secrets/*    # container runs as uid 1000 (node)
cp .env.example .env && chmod 600 .env   # non-secret settings only; leave secret values empty
docker compose up -d --build
docker compose logs -f fundbot
docker compose exec fundbot node scripts/check.js
```

`restart: unless-stopped` together with the Docker daemon starting at boot keeps the bot
running. The image has a `HEALTHCHECK`. Container logs are rotated by the `json-file`
options (20 MB × 10). The SQLite database is on the `fundbot-data` volume.

## 4. Logs and log rotation

- systemd: logs go to journald. Set a limit in `/etc/systemd/journald.conf`, for example `SystemMaxUse=1G`.
- File logging: set `LOG_FILE=/var/log/tron-funding-bot/bot.log` and install `deploy/logrotate/tron-funding-bot` to `/etc/logrotate.d/`.
- `LOG_FORMAT=json` gives one JSON object per line, which suits Loki, ELK or Datadog.

## 5. Health checks and monitoring

| Probe | Use |
|---|---|
| `GET /health/live` | Liveness (process up) |
| `GET /health` | Readiness: `ok` / `degraded` / `error` |
| `GET /admin/health` (admin key) | Detailed: DB, RPC lag, USDT verification, lease, worker, job counts, notification backlog |
| `node scripts/healthcheck.js` | Exit 0 or 1, for Docker, cron or Monit |

Suggested alerts: `/health` is not `ok` for more than 2 minutes; `FAILED` jobs above 0;
`CONFIRMING` jobs older than 10 minutes; the `notifications.failed` count rising; the
Mother balance below a threshold (poll `/admin/balance`). Also alert when
`FUNDING AUTO-PAUSED` or `FUNDING BOT STARTED` appears unexpectedly (a restart).

A simple cron-based monitor:

```cron
*/2 * * * * curl -fsS http://127.0.0.1:8080/health | grep -q '"ok"' || /usr/local/bin/notify-ops "fundbot unhealthy"
```

## 6. Backups

`sqlite3 /var/lib/tron-funding-bot/funding.db ".backup '/backup/funding-$(date +%F).db'"`,
run daily. The database holds the idempotency records: restoring an old backup can make
the bot forget that it already funded an address. After a restore, check recent
transactions on Tronscan (`/tx` for each address) before you resume.
