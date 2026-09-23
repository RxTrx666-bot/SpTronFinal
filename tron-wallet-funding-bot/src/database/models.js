// Repository layer. All SQL lives here; the rest of the app uses these methods.

import { randomUUID } from 'node:crypto';
import { scrub } from '../security/redact.js';

export const JobStatus = Object.freeze({
  RECEIVED: 'RECEIVED',
  VALIDATING: 'VALIDATING',
  QUEUED: 'QUEUED',
  FUNDING_TRX: 'FUNDING_TRX',
  FUNDING_USDT: 'FUNDING_USDT',
  CONFIRMING: 'CONFIRMING',
  COMPLETED: 'COMPLETED',
  FAILED: 'FAILED',
  RETRYING: 'RETRYING',
});

/** Statuses the worker picks up (active, not terminal). */
export const ACTIVE_JOB_STATUSES = [
  JobStatus.RECEIVED, // only if the process crashed right after creating the job
  JobStatus.VALIDATING,
  JobStatus.QUEUED,
  JobStatus.RETRYING,
  JobStatus.FUNDING_TRX,
  JobStatus.FUNDING_USDT,
  JobStatus.CONFIRMING,
];

export const TransferStatus = Object.freeze({
  SIGNED: 'SIGNED', // persisted before broadcast; broadcast outcome unknown
  BROADCAST: 'BROADCAST', // accepted by the node
  REJECTED: 'REJECTED', // node rejected it; watched until provably expired
  CONFIRMED: 'CONFIRMED', // solidified and successful
  FAILED: 'FAILED', // solidified but execution failed
  EXPIRED: 'EXPIRED', // provably never included
  DRY_RUN: 'DRY_RUN',
});

/** Transfers that may still end up on chain. */
export const LIVE_TRANSFER_STATUSES = [TransferStatus.SIGNED, TransferStatus.BROADCAST, TransferStatus.REJECTED];

export class DuplicateJobError extends Error {
  constructor() {
    super('A funding job already exists for this wallet');
    this.name = 'DuplicateJobError';
    this.code = 'DUPLICATE_WALLET';
  }
}

const num = (v) => (v === null || v === undefined ? v : Number(v));

function mapJob(r) {
  if (!r) return null;
  return {
    ...r,
    round: num(r.round),
    retry_count: num(r.retry_count),
    next_attempt_at: num(r.next_attempt_at),
    received_at: num(r.received_at),
    updated_at: num(r.updated_at),
    funded_at: num(r.funded_at),
  };
}

function mapTransfer(r) {
  if (!r) return null;
  return {
    ...r,
    expiration_at: num(r.expiration_at),
    block_number: num(r.block_number),
    broadcast_attempts: num(r.broadcast_attempts),
    created_at: num(r.created_at),
    updated_at: num(r.updated_at),
    confirmed_at: num(r.confirmed_at),
  };
}

function isUniqueViolation(err) {
  return err?.code === 'SQLITE_CONSTRAINT_UNIQUE' || err?.code === '23505' || /UNIQUE constraint failed/.test(err?.message ?? '');
}

export class Repository {
  constructor(knex, { clock = () => Date.now() } = {}) {
    this.knex = knex;
    this.now = clock;
  }

  async ping() {
    await this.knex.raw('select 1');
    return true;
  }

  // ---------------------------------------------------------------- wallets --
  async upsertWallet({ address, source }, trx = this.knex) {
    const now = this.now();
    const existing = await trx('wallets').where({ address }).first();
    if (existing) {
      await trx('wallets')
        .where({ address })
        .update({ submission_count: trx.raw('submission_count + 1'), last_received_at: now });
      return { ...existing, submission_count: num(existing.submission_count) + 1, isNew: false };
    }
    try {
      await trx('wallets').insert({ address, source: source ?? null, submission_count: 1, first_received_at: now, last_received_at: now });
    } catch (err) {
      if (!isUniqueViolation(err)) throw err;
      return this.upsertWallet({ address, source }, trx);
    }
    return { address, source, submission_count: 1, isNew: true };
  }

  async storeWalletSecret(address, enc) {
    await this.knex.transaction(async (trx) => {
      await trx('target_wallet_secrets').where({ address }).delete();
      await trx('target_wallet_secrets').insert({
        address,
        ciphertext: enc.ciphertext,
        iv: enc.iv,
        tag: enc.tag,
        key_id: enc.keyId,
        created_at: this.now(),
      });
      await trx('wallets').where({ address }).update({ has_encrypted_key: true });
    });
  }

  // ------------------------------------------------------------------- jobs --
  async latestJob(address, mode) {
    return mapJob(await this.knex('funding_jobs').where({ address, mode }).orderBy('round', 'desc').first());
  }

  async createJob({ address, mode, round = 1, trxAmountSun, usdtAmountUnits, source }) {
    const now = this.now();
    const job = {
      id: randomUUID(),
      address,
      mode,
      round,
      status: JobStatus.RECEIVED,
      trx_amount_sun: BigInt(trxAmountSun).toString(),
      usdt_amount_units: BigInt(usdtAmountUnits).toString(),
      retry_count: 0,
      next_attempt_at: 0,
      source: source ?? null,
      received_at: now,
      updated_at: now,
    };
    try {
      await this.knex.transaction(async (trx) => {
        await trx('funding_jobs').insert(job);
        await trx('job_events').insert({ job_id: job.id, from_status: null, to_status: JobStatus.RECEIVED, message: 'Job created', created_at: now });
      });
    } catch (err) {
      if (isUniqueViolation(err)) throw new DuplicateJobError();
      throw err;
    }
    return mapJob(job);
  }

  async getJob(id) {
    return mapJob(await this.knex('funding_jobs').where({ id }).first());
  }

  /** Find by job id or by address (latest job for that address in `mode`). */
  async findJob(idOrAddress, mode) {
    if (/^T[1-9A-HJ-NP-Za-km-z]{33}$/.test(idOrAddress)) return this.latestJob(idOrAddress, mode);
    return this.getJob(idOrAddress);
  }

  async listJobs({ statuses, mode, limit = 20, offset = 0 } = {}) {
    const q = this.knex('funding_jobs').orderBy('updated_at', 'desc').limit(Math.min(limit, 500)).offset(offset);
    if (statuses?.length) q.whereIn('status', statuses);
    if (mode) q.where({ mode });
    return (await q).map(mapJob);
  }

  async countJobsByStatus(mode) {
    const q = this.knex('funding_jobs').select('status').count({ n: '*' }).groupBy('status');
    if (mode) q.where({ mode });
    const rows = await q;
    return Object.fromEntries(rows.map((r) => [r.status, Number(r.n)]));
  }

  async dueJobs({ limit, excludeIds = [] }) {
    const q = this.knex('funding_jobs')
      .whereIn('status', ACTIVE_JOB_STATUSES)
      .where('next_attempt_at', '<=', this.now())
      .orderBy('received_at', 'asc')
      .limit(limit);
    if (excludeIds.length) q.whereNotIn('id', excludeIds);
    return (await q).map(mapJob);
  }

  /**
   * Compare-and-set status transition. Returns true if the job was in one of
   * `from` and has been moved to `to`. Records an audit event.
   */
  async transitionJob(id, from, to, patch = {}, message = null) {
    const now = this.now();
    const fromList = Array.isArray(from) ? from : [from];
    return this.knex.transaction(async (trx) => {
      const current = await trx('funding_jobs').where({ id }).first();
      if (!current || !fromList.includes(current.status)) return false;
      const n = await trx('funding_jobs')
        .where({ id, status: current.status })
        .update({ ...sanitizePatch(patch), status: to, updated_at: now });
      if (n !== 1) return false;
      if (current.status !== to || message) {
        await trx('job_events').insert({ job_id: id, from_status: current.status, to_status: to, message: message ? scrub(message).slice(0, 1000) : null, created_at: now });
      }
      return true;
    });
  }

  async updateJob(id, patch) {
    await this.knex('funding_jobs').where({ id }).update({ ...sanitizePatch(patch), updated_at: this.now() });
  }

  async jobEvents(jobId) {
    return this.knex('job_events').where({ job_id: jobId }).orderBy('id', 'asc');
  }

  // -------------------------------------------------------------- transfers --
  async insertTransfer(t) {
    const now = this.now();
    const row = {
      job_id: t.jobId,
      asset: t.asset,
      to_address: t.toAddress,
      amount: BigInt(t.amount).toString(),
      status: t.status,
      txid: t.txid ?? null,
      signed_tx: t.signedTx ? JSON.stringify(t.signedTx) : null,
      expiration_at: t.expirationAt ?? null,
      broadcast_attempts: 0,
      created_at: now,
      updated_at: now,
    };
    const [id] = await this.knex('transfers').insert(row).returning('id');
    return mapTransfer({ ...row, id: typeof id === 'object' ? id.id : id });
  }

  async updateTransfer(id, patch, { fromStatuses } = {}) {
    const q = this.knex('transfers').where({ id });
    if (fromStatuses) q.whereIn('status', fromStatuses);
    return q.update({ ...sanitizePatch(patch), updated_at: this.now() });
  }

  async transfersForJob(jobId) {
    return (await this.knex('transfers').where({ job_id: jobId }).orderBy('id', 'asc')).map(mapTransfer);
  }

  async liveTransfers() {
    return (await this.knex('transfers').whereIn('status', LIVE_TRANSFER_STATUSES)).map(mapTransfer);
  }

  /** Sum of amounts per asset for transfers matching statuses (and created since). */
  async sumTransfers({ statuses, since }) {
    const q = this.knex('transfers')
      .join('funding_jobs', 'transfers.job_id', 'funding_jobs.id')
      .where('funding_jobs.mode', 'live')
      .whereIn('transfers.status', statuses)
      .select('transfers.asset', 'transfers.amount');
    if (since) q.where('transfers.created_at', '>=', since);
    const totals = { TRX: 0n, USDT: 0n };
    for (const r of await q) totals[r.asset] = (totals[r.asset] ?? 0n) + BigInt(r.amount);
    return totals;
  }

  async countLiveUsdtTransfers() {
    const r = await this.knex('transfers')
      .join('funding_jobs', 'transfers.job_id', 'funding_jobs.id')
      .where('funding_jobs.mode', 'live')
      .where('transfers.asset', 'USDT')
      .whereIn('transfers.status', [TransferStatus.SIGNED, TransferStatus.BROADCAST])
      .count({ n: '*' })
      .first();
    return Number(r?.n ?? 0);
  }

  // ---------------------------------------------------------- notifications --
  async enqueueNotification({ jobId = null, kind, text }) {
    try {
      await this.knex('notifications').insert({
        job_id: jobId,
        kind,
        text: scrub(text),
        status: 'PENDING',
        attempts: 0,
        next_attempt_at: 0,
        created_at: this.now(),
      });
      return true;
    } catch (err) {
      if (isUniqueViolation(err)) return false; // already queued: never notify twice
      throw err;
    }
  }

  async dueNotifications(limit = 10) {
    return this.knex('notifications')
      .where({ status: 'PENDING' })
      .where('next_attempt_at', '<=', this.now())
      .orderBy('id', 'asc')
      .limit(limit);
  }

  async markNotification(id, patch) {
    await this.knex('notifications').where({ id }).update(sanitizePatch(patch));
  }

  async countNotifications(status) {
    const r = await this.knex('notifications').where({ status }).count({ n: '*' }).first();
    return Number(r?.n ?? 0);
  }

  // --------------------------------------------------------------- settings --
  async getSetting(key) {
    const r = await this.knex('settings').where({ key }).first();
    return r ? r.value : undefined;
  }

  async setSetting(key, value, updatedBy = 'system') {
    const row = { key, value: String(value), updated_by: String(updatedBy).slice(0, 64), updated_at: this.now() };
    await this.knex('settings').insert(row).onConflict('key').merge();
  }

  // ------------------------------------------------------------------ lease --
  /** Acquire or renew a named lease. Returns true if `owner` holds it. */
  async acquireLease(name, owner, ttlMs) {
    const now = this.now();
    return this.knex.transaction(async (trx) => {
      const cur = await trx('leases').where({ name }).first();
      if (!cur) {
        try {
          await trx('leases').insert({ name, owner, expires_at: now + ttlMs });
          return true;
        } catch (err) {
          if (isUniqueViolation(err)) return false;
          throw err;
        }
      }
      if (cur.owner === owner || Number(cur.expires_at) < now) {
        const n = await trx('leases')
          .where({ name, owner: cur.owner, expires_at: cur.expires_at })
          .update({ owner, expires_at: now + ttlMs });
        return n === 1;
      }
      return false;
    });
  }

  async releaseLease(name, owner) {
    await this.knex('leases').where({ name, owner }).delete();
  }
}

function sanitizePatch(patch) {
  const out = {};
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) continue;
    if (typeof v === 'bigint') out[k] = v.toString();
    else if (k === 'error_message' || k === 'last_error') out[k] = v === null ? null : scrub(String(v)).slice(0, 1000);
    else out[k] = v;
  }
  return out;
}
