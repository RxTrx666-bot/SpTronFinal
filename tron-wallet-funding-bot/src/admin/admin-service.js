// Operations shared by the Telegram commands and the admin HTTP API.

import { JobStatus, ACTIVE_JOB_STATUSES } from '../database/models.js';
import { sunToTrx, formatUnits } from '../blockchain/units.js';
import { scrubError } from '../security/redact.js';

export class AdminService {
  constructor({ repo, tron, usdt, signer, settings, funding, queue, config, startedAt = Date.now() }) {
    Object.assign(this, { repo, tron, usdt, signer, settings, funding, queue, config, startedAt });
  }

  get mode() {
    return this.config.dryRun ? 'dry_run' : 'live';
  }

  async balances() {
    const [trxSun, usdtUnits] = await Promise.all([this.tron.getTrxBalance(this.signer.address), this.usdt.balanceOf(this.signer.address)]);
    return {
      address: this.signer.address,
      trx: sunToTrx(trxSun),
      usdt: formatUnits(usdtUnits, this.config.usdt.decimals),
    };
  }

  async jobs(kind, limit = 20) {
    const map = {
      pending: ACTIVE_JOB_STATUSES,
      completed: [JobStatus.COMPLETED],
      failed: [JobStatus.FAILED],
      all: undefined,
    };
    const statuses = map[kind] ?? (Object.values(JobStatus).includes(kind) ? [kind] : undefined);
    return (await this.repo.listJobs({ statuses, mode: this.mode, limit })).map((j) => this.presentJob(j));
  }

  async job(idOrAddress) {
    const j = await this.repo.findJob(idOrAddress, this.mode);
    if (!j) return null;
    const transfers = await this.repo.transfersForJob(j.id);
    return {
      ...this.presentJob(j),
      transfers: transfers.map((t) => ({
        asset: t.asset,
        amount: t.asset === 'TRX' ? sunToTrx(t.amount) : formatUnits(t.amount, this.config.usdt.decimals),
        status: t.status,
        txid: t.txid,
        url: t.txid ? this.tron.txUrl(t.txid) : null,
        blockNumber: t.block_number ?? null,
        feeTrx: t.fee_sun ? sunToTrx(t.fee_sun) : null,
        error: t.error_code ? `${t.error_code}${t.error_message ? `: ${t.error_message}` : ''}` : null,
        createdAt: new Date(t.created_at).toISOString(),
      })),
    };
  }

  presentJob(j) {
    return {
      id: j.id,
      address: j.address,
      mode: j.mode,
      round: j.round,
      status: j.status,
      trxAmount: sunToTrx(j.trx_amount_sun),
      usdtAmount: formatUnits(j.usdt_amount_units, this.config.usdt.decimals),
      trxSent: j.trx_sent_sun ? sunToTrx(j.trx_sent_sun) : null,
      usdtSent: j.usdt_sent_units ? formatUnits(j.usdt_sent_units, this.config.usdt.decimals) : null,
      trxTxid: j.trx_txid ?? null,
      usdtTxid: j.usdt_txid ?? null,
      error: j.error_code ? { code: j.error_code, message: j.error_message } : null,
      retryCount: j.retry_count,
      receivedAt: new Date(j.received_at).toISOString(),
      fundedAt: j.funded_at ? new Date(j.funded_at).toISOString() : null,
    };
  }

  async retry(idOrAddress, by) {
    return this.presentJob(await this.funding.retryJob(idOrAddress, by));
  }

  async pause(by) {
    await this.settings.pause(`manual (${by})`, by);
    return this.settings.snapshot();
  }

  async resume(by) {
    await this.settings.resume(by);
    this.queue?.notify();
    return this.settings.snapshot();
  }

  async setAmount(asset, value, by) {
    return this.settings.setAmount(asset, value, by);
  }

  async health({ deep = false } = {}) {
    const out = {
      status: 'ok',
      network: this.config.network.name,
      motherAddress: this.signer.address,
      dryRun: this.config.dryRun,
      paused: this.settings.paused,
      uptimeSeconds: Math.round((Date.now() - this.startedAt) / 1000),
      queue: this.queue?.status() ?? null,
      checks: {},
    };
    try {
      await this.repo.ping();
      out.checks.database = 'ok';
    } catch (err) {
      out.checks.database = `error: ${scrubError(err).message}`;
      out.status = 'error';
    }
    if (deep) {
      try {
        const h = await this.tron.checkConnection();
        out.checks.rpc = `ok (head ${h.headBlock}, lag ${h.lagSeconds}s)`;
      } catch (err) {
        out.checks.rpc = `error: ${scrubError(err).message}`;
        out.status = out.status === 'error' ? 'error' : 'degraded';
      }
      out.jobs = await this.repo.countJobsByStatus(this.mode);
      out.notifications = { pending: await this.repo.countNotifications('PENDING'), failed: await this.repo.countNotifications('FAILED') };
    } else if (this.tron.lastHealth) {
      out.checks.rpc = this.tron.lastHealth.ok ? 'ok' : 'degraded';
    }
    out.checks.usdtContract = this.usdt.verified ? 'verified' : 'not verified';
    if (!this.usdt.verified && out.status === 'ok') out.status = 'degraded';
    if (this.queue && !this.queue.hasLease()) {
      out.checks.lease = 'standby (another instance holds the lease)';
      if (out.status === 'ok') out.status = 'degraded';
    }
    const last = this.queue?.lastTickAt;
    if (this.queue?.running && last && Date.now() - last > Math.max(60_000, this.config.queue.pollIntervalMs * 10)) {
      out.checks.worker = 'stalled';
      out.status = 'error';
    }
    return out;
  }
}
