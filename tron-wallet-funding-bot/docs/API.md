# HTTP API

Base URL: `https://funding.example.internal` (served by the HTTPS reverse proxy), or
`http://127.0.0.1:8080` locally.

Authentication is `Authorization: Bearer <key>` only. Credentials in URLs are refused
with `400 CREDENTIALS_IN_URL`.

| Key | May call |
|---|---|
| `INTERNAL_API_KEY` | `POST /wallets`, `POST /wallets/batch`, `GET /wallets/:address` |
| `ADMIN_API_KEY` | `/admin/*` (admin endpoints are off if this key is not set) |

Every error response looks like `{ "error": "CODE", "message": "..." }`. Request bodies
are never echoed back, even when the JSON is malformed.

Default rate limit: 60 requests per minute per IP (`API_RATE_LIMIT_MAX`).

## Submit a target wallet

`POST /wallets`

```json
{ "address": "TXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "source": "signup-bot" }
```

| Field | Required | Notes |
|---|---|---|
| `address` | yes | base58 TRON address (`T…`). Hex addresses, private keys and other chains' addresses are rejected. |
| `source` | no | A free label (max 64 characters, sanitized). |
| `repeat` | no | `true` asks for another funding round. It only takes effect when `ALLOW_REPEAT_FUNDING=true` and the previous round is `COMPLETED`. |
| `private_key` | no | **Not needed for funding.** Accepted only over HTTPS. See the private-key policy below. |

Responses:

- `201 {"status":"created","job":{...}}` means a new job was queued.
- `200 {"status":"duplicate","job":{...}}` means the address already has a job, and the existing job is returned. Nothing new is funded.
- `422 INVALID_ADDRESS | PRIVATE_KEY_AS_ADDRESS | DESTINATION_IS_MOTHER | PRIVATE_KEY_ADDRESS_MISMATCH | PRIVATE_KEY_NOT_ACCEPTED | FUNDING_DISABLED`
- `403 HTTPS_REQUIRED` means `private_key` was sent over plain HTTP.
- `400 MALFORMED_REQUEST`, `401 UNAUTHORIZED`, `429 RATE_LIMITED`

Private-key policy (`TARGET_PRIVATE_KEY_POLICY`):

- `discard` (default): the key is checked against `address` and then dropped. It is never stored or logged.
- `reject`: any request that contains `private_key` is refused.
- `encrypt`: the key is stored AES-256-GCM encrypted in `target_wallet_secrets`. Use this only if a later feature must sign **from** target wallets.

```bash
curl -s -X POST https://funding.example.internal/wallets \
  -H "Authorization: Bearer $INTERNAL_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"address":"TXXXXXXXXXXXXXXXXXXXXXXXXXXXX","source":"signup-bot"}'
```

Example response:

```json
{
  "status": "created",
  "job": {
    "id": "0b5b1f0e-3c2e-4c6e-9d0a-8f1f5b2f9c11",
    "address": "TXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "mode": "live",
    "round": 1,
    "status": "QUEUED",
    "trxAmount": "5",
    "usdtAmount": "10",
    "trxSent": null,
    "usdtSent": null,
    "trxTxid": null,
    "usdtTxid": null,
    "error": null,
    "retryCount": 0,
    "receivedAt": "2026-09-23T12:00:00.000Z",
    "fundedAt": null
  }
}
```

## Batch submit

`POST /wallets/batch` with `{ "wallets": [ {"address": "T..."}, ... ] }` (max
`API_MAX_BATCH_SIZE`, default 100). Returns `{ "results": [ {status: created|duplicate|rejected, ...} ] }`.

## Wallet status

`GET /wallets/TXXXXXXXXXXXXXXXXXXXXXXXXXXXX` returns the job and its transactions:

```json
{
  "id": "...", "status": "COMPLETED", "trxSent": "5", "usdtSent": "10",
  "trxTxid": "8bb5…", "usdtTxid": "2e4d…",
  "transfers": [
    { "asset": "TRX", "amount": "5", "status": "CONFIRMED", "txid": "8bb5…",
      "url": "https://tronscan.org/#/transaction/8bb5…", "blockNumber": 71234567, "feeTrx": "1.1" },
    { "asset": "USDT", "amount": "10", "status": "CONFIRMED", "txid": "2e4d…", "url": "…", "feeTrx": "13.4" }
  ]
}
```

## Health

- `GET /health` (no auth): `{"status":"ok"|"degraded"|"error"}`
- `GET /health/live` (no auth, not rate limited): `{"status":"ok"}`

## Admin endpoints (`ADMIN_API_KEY`)

| Method & path | Purpose |
|---|---|
| `GET /admin/health` | Detailed health: DB, RPC, USDT verification, lease, worker, job counts, notification backlog |
| `GET /admin/balance` | Mother Wallet TRX and USDT balance |
| `GET /admin/jobs?status=pending\|completed\|failed\|all\|<STATUS>&limit=50` | List jobs |
| `GET /admin/jobs/:idOrAddress` | Job detail with transaction IDs |
| `POST /admin/jobs/:idOrAddress/retry` | Retry a `FAILED` job (`409` otherwise) |
| `POST /admin/pause` / `POST /admin/resume` | Pause or resume funding |
| `GET /admin/settings` | Current amounts, caps, pause state |
| `PUT /admin/settings/funding` | `{"trx_amount":"5","usdt_amount":"10"}` (either field may be omitted) |

```bash
A="Authorization: Bearer $ADMIN_API_KEY"
curl -s https://funding.example.internal/admin/balance -H "$A"
curl -s "https://funding.example.internal/admin/jobs?status=failed" -H "$A"
curl -s -X POST https://funding.example.internal/admin/jobs/TXXXXXXXXXXXXXXXXXXXXXXXXXXXX/retry -H "$A"
curl -s -X POST https://funding.example.internal/admin/pause -H "$A"
curl -s -X PUT https://funding.example.internal/admin/settings/funding -H "$A" \
  -H 'content-type: application/json' -d '{"trx_amount":"3","usdt_amount":"15"}'
```

## Node.js client example (for the upstream bot)

```js
const res = await fetch('https://funding.example.internal/wallets', {
  method: 'POST',
  headers: {
    authorization: `Bearer ${process.env.FUNDING_BOT_KEY}`,
    'content-type': 'application/json',
  },
  body: JSON.stringify({ address }),
});
if (![200, 201].includes(res.status)) throw new Error(`funding bot: ${res.status}`);
const { status, job } = await res.json(); // status: created | duplicate
```

Submitting the same address again is safe: the bot returns the existing job.
