# Security

## Threat model (summary)

| Asset | Threat | Mitigation |
|---|---|---|
| Mother private key | Leak through logs, alerts, errors, env dumps, git, provider | Loaded from a `*_FILE` or env var, then removed from `process.env`. Held only in a private class field of `MotherSigner`. Signing is local. The scrubber redacts the key everywhere. `.gitignore` covers `.env`, `secrets/` and `*.key`. `toJSON` / `inspect` show the address only. |
| Mother funds | Duplicate funding, double spend on retry, races, a malicious RPC, a wrong network or token | Idempotent jobs; persist-before-broadcast; re-broadcast of the identical tx only; a new tx only after provable expiry; a spend mutex and in-flight reservation; a single-instance DB lease; local tx construction with byte-level verification; genesis and USDT contract checks; per-wallet caps and daily limits; auto-pause. |
| Target wallet keys | Unnecessary collection | Not needed for funding. By default they are validated and dropped (`discard`). `reject` refuses them. `encrypt` stores AES-256-GCM only when a later feature truly needs it. Accepted over HTTPS only. |
| Control plane | Unauthorized funding or pausing | Separate internal and admin bearer keys (at least 32 characters, compared in constant time). Rate limiting. Telegram commands only from allow-listed chats and users. Credentials are never accepted in URLs. |

## How private keys are handled

- **Mother key**: `MOTHER_PRIVATE_KEY_FILE`, preferably a systemd credential or Docker
  secret. The file must be `chmod 600`; group- or world-readable files are refused.
  After loading, the variable is deleted from the environment and the only reference
  lives inside the signer. Startup prints the derived **address** only.
- **Signing**: transactions are built locally from a recent block reference. The
  serialized protobuf is decoded again and compared with the intent: owner, recipient,
  amount, contract, calldata, `fee_limit`, no TRX value attached, no memo, and a sane
  expiration. `txID == sha256(raw_data_hex)` is checked before signing, and the
  signature is checked by recovering the address. Only the signed transaction is sent to
  the node.
- **Target keys**: by hash only, for up to 24h, so that they can be redacted if they
  ever show up in text. Never persisted unless `TARGET_PRIVATE_KEY_POLICY=encrypt`.
  JavaScript cannot wipe string memory; the design keeps the lifetime and number of
  references to a minimum.
- **Destinations**: only base58 `T…` addresses are accepted. A 64-hex value (a private
  key) is refused with a message that does **not** echo it. The Mother address, the USDT
  contract and (by default) any smart contract are refused as destinations.

## What is never logged or sent

Private keys, API keys, the Telegram token, database passwords, request bodies, query
strings and signed transactions. Every log line, alert and stored error message goes
through `security/redact.js`, which removes:

- secrets registered at load time (exact match, anywhere in the text)
- 64-hex strings whose hash matches a known key
- fields named like `private_key`, `password`, `token` or `api_key`
- `Bearer …` values, Telegram bot tokens, and passwords inside URLs

JSON parse errors are replaced by a generic message, because Node's native error text
quotes the raw body.

## Network and RPC safety

- `TRON_RPC_URL` must be `https://`. Credentials or query strings in RPC URLs are refused. The API key is sent as a header only.
- The genesis block ID is compared with the expected network at startup. A mismatch refuses to run.
- Reads retry with backoff, and HTTP 429 is honoured. **Broadcast is never retried by
  the transport.** The funding state machine decides, after checking the chain, whether
  to re-broadcast the *same* signed bytes.
- A transaction counts as final only once it is **solidified**. For USDT, the receipt
  must be `SUCCESS` and a `Transfer(from=Mother, to=target, amount)` event must be present.

## Operational controls

- `DRY_RUN=true` for rehearsals. `START_PAUSED=true` to start without sending anything.
- `MAX_*_PER_WALLET` caps that runtime settings can never exceed. `DAILY_*_LIMIT`
  circuit breakers.
- `AUTO_PAUSE_ON_INSUFFICIENT_BALANCE`.
- Keep only a float in the Mother Wallet and refill it from cold storage.

## Deployment hardening

- The service runs as an unprivileged user under a hardened systemd sandbox
  (`ProtectSystem=strict`, `NoNewPrivileges`, no capabilities, `UMask=0077`). The
  container runs non-root with a read-only root filesystem and all capabilities dropped.
- The DB directory is `0700` and the SQLite files are `0600`. Use a PostgreSQL user that
  owns only its own database.
- The API listens on loopback behind a TLS reverse proxy that allows internal networks
  only, and logs no query strings.
- Rotate `INTERNAL_API_KEY` and `ADMIN_API_KEY` by replacing the secret files and
  restarting. If the Mother key may have leaked, move the funds to a new key at once.

## Security review checklist (performed)

| Item | Result |
|---|---|
| Private key exposure (logs, Telegram, DB, errors, API responses, env) | Covered by the scrubber, the signer design and tests (`redaction.test.js`, `funding.test.js` scans every table and every log line). |
| Duplicate transaction risk | Unique job key; persist-before-broadcast; invariant that a new tx needs a provably dead predecessor (tests: accepted-timeout, lost-timeout, crash-before-broadcast, expiry-rebuild, node reject then retry). |
| Race conditions | Spend mutex and reservation of in-flight amounts; single-instance lease; concurrent duplicate submissions resolved by a unique index (tests: 5 concurrent jobs against a balance for 2; 3 concurrent duplicate submissions). |
| Transaction retry problems | The transport never retries broadcasts; reconciliation checks the chain first; expiry is judged on solidified chain time with a safety margin, never on the local clock. |
| Insufficient balance | Checked for all remaining assets before the first broadcast, and again inside the spend lock; auto-pause. |
| USDT decimals | Amounts are BigInt only; the contract must report 6 decimals; calldata amount encoding is tested. |
| Confirmation | Solidified info plus receipt plus Transfer event match; on-chain failure is detected; late confirmations of orphaned transfers are swept. |
| API authentication | Constant-time bearer check before the body is parsed; key separation; rate limit; HTTPS-only private keys; credentials in URLs refused (tests in `api.test.js`). |
| Telegram | Allow-listed chats and users; backlog skipped at startup; failures isolated from funding (tested). |

## Known limitations

- The genesis block IDs and the reference USDT contract in `src/config/networks.js`
  are the widely published values. Run `npm run check` against your node and compare
  with Tronscan. If they ever differ, startup fails closed; override with
  `EXPECTED_GENESIS_BLOCK_ID` only after verifying.
- Balance reservation is deliberately conservative. Shortly after a broadcast, an amount
  can be counted both as reserved and as already deducted, which can briefly under-report
  the available balance. This only ever errs towards **not** sending.
- A single Mother Wallet and a single active instance, by design.
