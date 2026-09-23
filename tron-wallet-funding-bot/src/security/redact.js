// Central secret scrubbing.
//
// Every string that leaves the process (logs, Telegram messages, HTTP error
// bodies, DB error columns) is passed through scrub(). Two mechanisms:
//
//  1. Exact secrets (Mother key, API keys, Telegram token, DB password, ...)
//     registered at startup are replaced wherever they appear as substrings.
//  2. Target-wallet private keys received through the API are NOT kept in
//     memory. Only their SHA-256 hash is remembered (for a limited time) so
//     that any 64-hex token that hashes to a known secret is redacted.
//
// On top of that, a few structural patterns are always redacted (fields named
// like private keys, bearer tokens, Telegram bot tokens in URLs).

import { createHash } from 'node:crypto';

const REDACTED = '[REDACTED]';
const exactSecrets = new Set();
const hashedSecrets = new Map(); // sha256(hex lowercase) -> expiresAt (ms, Infinity = forever)

const HEX64 = /\b(?:0x)?([0-9a-fA-F]{64})\b/g;
const KEY_FIELD =
  /("?(?:private[_-]?key|privateKey|priv[_-]?key|secret|password|passphrase|api[_-]?key|token|mnemonic|seed)"?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;}&]+)/gi;
const BEARER = /(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi;
const TG_TOKEN = /(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}/g;
const URL_CREDENTIALS = /\b([a-z][a-z0-9+.-]*:\/\/)([^\s:/@]+):([^\s@/]+)@/gi;

function sha256Hex(value) {
  return createHash('sha256').update(value).digest('hex');
}

/** Register a secret value that must never appear in any output. */
export function registerSecret(value) {
  if (typeof value !== 'string') return;
  const v = value.trim();
  if (v.length < 6) return; // too short to be meaningful; avoid redacting common words
  exactSecrets.add(v);
  const hex = v.replace(/^0x/i, '');
  if (/^[0-9a-fA-F]{64}$/.test(hex)) hashedSecrets.set(sha256Hex(hex.toLowerCase()), Infinity);
}

/**
 * Remember a transient secret (e.g. a target wallet private key seen in a
 * request) by hash only, so it can be scrubbed without being retained.
 */
export function registerTransientSecret(value, ttlMs = 24 * 3600 * 1000) {
  if (typeof value !== 'string') return;
  const hex = value.trim().replace(/^0x/i, '');
  if (!/^[0-9a-fA-F]{64}$/.test(hex)) return;
  hashedSecrets.set(sha256Hex(hex.toLowerCase()), Date.now() + ttlMs);
  pruneHashed();
}

function pruneHashed() {
  if (hashedSecrets.size < 10000) return;
  const now = Date.now();
  for (const [k, exp] of hashedSecrets) if (exp < now) hashedSecrets.delete(k);
}

/** Test helper. */
export function _resetSecretsForTests() {
  exactSecrets.clear();
  hashedSecrets.clear();
}

export function scrub(input) {
  if (input === null || input === undefined) return input;
  let s = typeof input === 'string' ? input : String(input);
  for (const secret of exactSecrets) {
    if (s.includes(secret)) s = s.split(secret).join(REDACTED);
  }
  if (hashedSecrets.size > 0) {
    const now = Date.now();
    s = s.replace(HEX64, (match, hex) => {
      const exp = hashedSecrets.get(sha256Hex(hex.toLowerCase()));
      return exp !== undefined && exp >= now ? REDACTED : match;
    });
  }
  s = s.replace(KEY_FIELD, (_m, prefix) => `${prefix}${REDACTED}`);
  s = s.replace(BEARER, (_m, prefix) => `${prefix}${REDACTED}`);
  s = s.replace(TG_TOKEN, REDACTED);
  s = s.replace(URL_CREDENTIALS, (_m, scheme, user) => `${scheme}${user}:${REDACTED}@`);
  return s;
}

const SENSITIVE_KEY = /^(private[_-]?key|privatekey|priv[_-]?key|secret|password|passphrase|api[_-]?key|apikey|token|authorization|mnemonic|seed|signed_tx|signature)$/i;

/** Deep-copy an object, dropping sensitive keys and scrubbing strings. */
export function scrubObject(value, depth = 0) {
  if (depth > 6) return '[Truncated]';
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return scrub(value);
  if (typeof value === 'bigint') return value.toString();
  if (typeof value !== 'object') return value;
  if (value instanceof Error) return scrubError(value);
  if (Array.isArray(value)) return value.slice(0, 50).map((v) => scrubObject(v, depth + 1));
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = SENSITIVE_KEY.test(k) ? REDACTED : scrubObject(v, depth + 1);
  }
  return out;
}

/** Serialise an error safely (no stack in production output unless asked). */
export function scrubError(err, { withStack = false } = {}) {
  if (!err) return err;
  const out = {
    name: err.name,
    message: scrub(err.message ?? String(err)),
  };
  if (err.code) out.code = scrub(String(err.code));
  if (withStack && err.stack) out.stack = scrub(err.stack);
  return out;
}

export function maskAddress(address) {
  if (typeof address !== 'string' || address.length < 12) return address;
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}
