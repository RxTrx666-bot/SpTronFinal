// DB-backed job queue with controlled concurrency.
//
// Jobs are durable rows in `funding_jobs`; the worker polls for due jobs and
// processes at most QUEUE_CONCURRENCY at a time. Even with concurrency > 1 the
// spending critical section is serialised inside FundingService.
//
// A DB lease guarantees that only ONE bot instance processes jobs at any time
// (protects against two processes spending the same Mother Wallet balance).

import { randomUUID } from 'node:crypto';
import { scrubError } from '../security/redact.js';

const LEASE_NAME = 'funding-worker';

export class FundingQueue {
  constructor({ repo, funding, config, logger }) {
    this.repo = repo;
    this.funding = funding;
    this.config = config;
    this.logger = logger;
    this.inFlight = new Map(); // jobId -> promise
    this.owner = randomUUID();
    this.leaseValidUntil = 0;
    this.timer = null;
    this.leaseTimer = null;
    this.sweepTimer = null;
    this.running = false;
    this.lastTickAt = null;
    this.ticking = false;
  }

  hasLease() {
    return Date.now() < this.leaseValidUntil;
  }

  async renewLease() {
    const ttl = this.config.queue.leaseTtlSeconds * 1000;
    const start = Date.now();
    try {
      const ok = await this.repo.acquireLease(LEASE_NAME, this.owner, ttl);
      // Consider the lease valid for a bit less than the TTL to be safe.
      this.leaseValidUntil = ok ? start + ttl - 5000 : 0;
      return ok;
    } catch (err) {
      this.leaseValidUntil = 0;
      this.logger.error({ error: scrubError(err).message }, 'Lease renewal failed');
      return false;
    }
  }

  async start() {
    this.running = true;
    const ok = await this.renewLease();
    if (!ok) {
      this.logger.warn('Another instance holds the funding lease; this instance will wait (standby)');
    }
    const ttl = this.config.queue.leaseTtlSeconds * 1000;
    this.leaseTimer = setInterval(() => this.renewLease(), Math.max(1000, Math.floor(ttl / 3)));
    this.timer = setInterval(() => this.tick(), this.config.queue.pollIntervalMs);
    this.sweepTimer = setInterval(() => this.hasLease() && this.funding.sweepOrphanTransfers().catch(() => {}), 60_000);
    for (const t of [this.leaseTimer, this.timer, this.sweepTimer]) t.unref?.();
    this.tick();
  }

  /** Wake the worker immediately (e.g. after a new submission). */
  notify() {
    if (this.running) setImmediate(() => this.tick());
  }

  async tick() {
    if (!this.running || this.ticking) return;
    this.ticking = true;
    try {
      this.lastTickAt = Date.now();
      if (!this.hasLease()) return;
      const free = this.config.queue.concurrency - this.inFlight.size;
      if (free <= 0) return;
      const jobs = await this.repo.dueJobs({ limit: free, excludeIds: [...this.inFlight.keys()] });
      for (const job of jobs) {
        const p = this.funding
          .processJob(job.id)
          .catch((err) => this.logger.error({ job: job.id, error: scrubError(err).message }, 'Unexpected worker error'))
          .finally(() => {
            this.inFlight.delete(job.id);
            if (this.running) setImmediate(() => this.tick());
          });
        this.inFlight.set(job.id, p);
      }
    } catch (err) {
      this.logger.error({ error: scrubError(err).message }, 'Queue tick failed');
    } finally {
      this.ticking = false;
    }
  }

  /** Stop taking new jobs and wait for in-flight ones (bounded). */
  async stop({ timeoutMs = 30000 } = {}) {
    this.running = false;
    this.funding.stopping = true;
    clearInterval(this.timer);
    clearInterval(this.sweepTimer);
    const all = Promise.allSettled([...this.inFlight.values()]);
    await Promise.race([all, new Promise((r) => setTimeout(r, timeoutMs).unref?.())]);
    clearInterval(this.leaseTimer);
    await this.repo.releaseLease(LEASE_NAME, this.owner).catch(() => {});
    this.leaseValidUntil = 0;
  }

  status() {
    return {
      running: this.running,
      hasLease: this.hasLease(),
      inFlight: this.inFlight.size,
      concurrency: this.config.queue.concurrency,
      lastTickAt: this.lastTickAt ? new Date(this.lastTickAt).toISOString() : null,
    };
  }
}
