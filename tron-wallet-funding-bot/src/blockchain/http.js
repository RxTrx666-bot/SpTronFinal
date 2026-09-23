// HTTP transport for TRON full/solidity node APIs (java-tron HTTP API, TronGrid,
// or any compatible provider).
//
// - per-request timeout
// - bounded retries with exponential backoff + jitter for READ calls only
// - HTTP 429 handling honouring Retry-After
// - optional client-side minimum interval between requests (rate limiting)
// - the API key is sent as a header only, never in URLs, never logged
//
// Broadcasts are sent with retry disabled: re-sending is decided by the funding
// pipeline after checking the chain, never blindly by the transport.

import { RpcError } from './errors.js';
import { scrub } from '../security/redact.js';
import { fetchWithTimeout } from '../util/mutex.js';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class RpcClient {
  constructor({ url, solidityUrl, apiKey, apiKeyHeader = 'TRON-PRO-API-KEY', timeoutMs = 10000, maxRetries = 3, minIntervalMs = 0, fetchImpl = globalThis.fetch, logger }) {
    this.url = url;
    this.solidityUrl = solidityUrl || url;
    this.timeoutMs = timeoutMs;
    this.maxRetries = maxRetries;
    this.minIntervalMs = minIntervalMs;
    this.fetch = fetchImpl;
    this.logger = logger;
    this.#headers = { 'content-type': 'application/json', accept: 'application/json' };
    if (apiKey) this.#headers[apiKeyHeader] = apiKey;
    this.#nextSlot = 0;
  }

  #headers;
  #nextSlot;

  async #throttle() {
    if (!this.minIntervalMs) return;
    const now = Date.now();
    const wait = Math.max(0, this.#nextSlot - now);
    this.#nextSlot = Math.max(now, this.#nextSlot) + this.minIntervalMs;
    if (wait) await sleep(wait);
  }

  /**
   * POST a JSON body to the node API.
   * @param {string} path e.g. '/wallet/getnowblock'
   * @param {object} body
   * @param {{ solidity?: boolean, retry?: boolean }} opts
   */
  async post(path, body = {}, { solidity = false, retry = true } = {}) {
    const base = solidity ? this.solidityUrl : this.url;
    const attempts = retry ? this.maxRetries + 1 : 1;
    let lastErr;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        return await this.#once(base, path, body);
      } catch (err) {
        lastErr = err;
        const canRetry = retry && err instanceof RpcError && err.transient && attempt < attempts;
        if (!canRetry) break;
        const backoff = err.retryAfterMs ?? Math.min(8000, 250 * 2 ** (attempt - 1)) + Math.floor(Math.random() * 200);
        this.logger?.warn({ path, attempt, kind: err.kind, backoffMs: backoff }, 'RPC call failed, retrying');
        await sleep(backoff);
      }
    }
    throw lastErr;
  }

  async #once(base, path, body) {
    await this.#throttle();
    let res;
    try {
      res = await fetchWithTimeout(
        this.fetch,
        `${base}${path}`,
        { method: 'POST', headers: this.#headers, body: JSON.stringify(body) },
        this.timeoutMs,
      );
    } catch (err) {
      if (err?.name === 'TimeoutError' || err?.name === 'AbortError') {
        throw new RpcError('timeout', `RPC timeout after ${this.timeoutMs}ms on ${path}`);
      }
      throw new RpcError('network', `RPC network error on ${path}: ${scrub(err?.cause?.code ?? err?.message ?? 'unknown')}`);
    }
    if (res.status === 429) {
      const ra = Number(res.headers.get('retry-after'));
      throw new RpcError('rate_limit', `RPC rate limited on ${path}`, {
        status: 429,
        retryAfterMs: Number.isFinite(ra) && ra > 0 ? Math.min(ra * 1000, 30000) : 2000,
      });
    }
    if (res.status >= 500) {
      throw new RpcError('http', `RPC HTTP ${res.status} on ${path}`, { status: res.status });
    }
    if (res.status === 401 || res.status === 403) {
      throw new RpcError('http', `RPC HTTP ${res.status} on ${path} (check TRON_API_KEY)`, { status: res.status, transient: false });
    }
    if (!res.ok) {
      throw new RpcError('http', `RPC HTTP ${res.status} on ${path}`, { status: res.status, transient: false });
    }
    let text;
    try {
      text = await res.text();
    } catch {
      throw new RpcError('network', `RPC response read failed on ${path}`);
    }
    try {
      return text ? JSON.parse(text) : {};
    } catch {
      throw new RpcError('bad_response', `RPC returned non-JSON response on ${path}`);
    }
  }
}
