// WALLET FUNDING — the state machine that sends TRX / USDT from the Mother Wallet
// to a target address. (Wallet *identification* lives in intake.js.)
//
// Safety invariants
// -----------------
// 1. A transaction is persisted (txid + signed bytes + expiration) BEFORE it is
//    broadcast. A crash at any point therefore leaves a record of every
//    transaction that could possibly be on chain.
// 2. For a given (job, asset) a NEW transaction is only ever created when every
//    previous transaction for that pair is provably dead: either it executed and
//    failed on chain, or the latest solidified block is past its expiration and
//    the chain does not contain it. Until then only the SAME signed transaction
//    may be re-broadcast (same txid => can never execute twice).
// 3. The critical section "check balance -> build -> sign -> persist -> broadcast"
//    is serialised by a process-wide mutex, and only runs while this process
//    holds the single-instance DB lease.
// 4. Notification delivery is decoupled (outbox), so it cannot influence funding.

import { FundingError, RpcError, isTransient } from '../blockchain/errors.js';
import { JobStatus, TransferStatus, LIVE_TRANSFER_STATUSES } from '../database/models.js';
import { assertValidDestination, toHexAddress, ValidationError } from './validation.js';
import {
  remainingAssets,
  computeRequirements,
  computeReserved,
  assertSufficientBalance,
  assertDailyLimits,
  assertFeeLimitSufficient,
} from './planner.js';
import { sunToTrx, formatUnits } from '../blockchain/units.js';
import { formatFundedMessage, formatFailedMessage, formatSystemAlert } from '../telegram/alerts.js';
import { scrub, scrubError, maskAddress } from '../security/redact.js';
import { Mutex, sleep as realSleep } from '../util/mutex.js';

const TRANSIENT_BROADCAST_CODES = new Set([
  'SERVER_BUSY',
  'NO_CONNECTION',
  'NOT_ENOUGH_EFFECTIVE_CONNECTION',
  'BLOCK_UNSOLIDIFIED',
  'TAPOS_ERROR',
  'TRANSACTION_EXPIRATION_ERROR',
]);

const TERMINAL = new Set([JobStatus.COMPLETED, JobStatus.FAILED]);
const DAY_MS = 24 * 3600 * 1000;

export class FundingService {
  constructor({ repo, tron, usdt, signer, settings, config, logger, clock = () => Date.now(), sleep = realSleep, hasLease = () => true }) {
    this.repo = repo;
    this.tron = tron;
    this.usdt = usdt;
    this.signer = signer;
    this.settings = settings;
    this.config = config;
    this.logger = logger;
    this.now = clock;
    this.sleep = sleep;
    this.hasLease = hasLease;
    this.spendLock = new Mutex();
    this.stopping = false;
  }

  amountLabel(asset, amount) {
    return asset === 'TRX' ? `${sunToTrx(amount)} TRX` : `${formatUnits(amount, this.config.usdt.decimals)} USDT`;
  }

  /** Process one job as far as possible. Never throws. */
  async processJob(jobId) {
    const job = await this.repo.getJob(jobId);
    if (!job || TERMINAL.has(job.status)) return job;
    const log = this.logger.child({ job: job.id, address: job.address, mode: job.mode });
    try {
      await this.#run(job, log);
    } catch (err) {
      await this.#handleError(job, err, log);
    }
    return this.repo.getJob(jobId);
  }

  // ------------------------------------------------------------------------
  async #run(job, log) {
    const dryRun = job.mode === 'dry_run';
    const plan = ['TRX', 'USDT'].filter((a) => this.#plannedAmount(job, a) > 0n);
    if (plan.length === 0) {
      throw new FundingError('NOTHING_TO_FUND', 'Both TRX and USDT amounts are 0');
    }

    if (job.status === JobStatus.RECEIVED || job.status === JobStatus.VALIDATING) {
      await this.repo.transitionJob(job.id, [JobStatus.RECEIVED, JobStatus.VALIDATING], JobStatus.QUEUED, {}, 'Validated');
      job.status = JobStatus.QUEUED;
    }

    // Phase 1: reconcile anything that may already be on chain (never creates txs).
    const state = await this.#reconcileAll(job, plan, log);

    // Phase 2: create the transactions that are still missing.
    const missing = remainingAssets(job, state);
    if (missing.length > 0) {
      if (this.settings.paused) {
        log.debug('Funding paused; job waiting');
        await this.repo.updateJob(job.id, { next_attempt_at: this.now() + this.config.queue.pollIntervalMs });
        return;
      }
      if (!this.hasLease()) throw new FundingError('NO_LEASE', 'This instance does not hold the funding lease', { transient: true });

      await this.#preflight(job, missing, log);

      for (const item of missing) {
        const status = item.asset === 'TRX' ? JobStatus.FUNDING_TRX : JobStatus.FUNDING_USDT;
        await this.repo.transitionJob(job.id, [JobStatus.QUEUED, JobStatus.RETRYING, JobStatus.FUNDING_TRX, JobStatus.FUNDING_USDT, JobStatus.CONFIRMING], status);
        const result = await this.spendLock.run(() => this.#sendTransfer(job, item, log));
        state[item.asset] = result;
      }
    }

    if (dryRun) {
      await this.#complete(job, log);
      return;
    }

    // Phase 3: wait for solidified confirmation.
    await this.repo.transitionJob(job.id, [JobStatus.FUNDING_TRX, JobStatus.FUNDING_USDT, JobStatus.QUEUED, JobStatus.RETRYING, JobStatus.CONFIRMING], JobStatus.CONFIRMING);
    const deadline = this.now() + this.config.tx.confirmationTimeoutSeconds * 1000;
    for (;;) {
      const s = await this.#reconcileAll(job, plan, log);
      if (plan.every((a) => s[a] === 'CONFIRMED')) {
        await this.#complete(job, log);
        return;
      }
      if (plan.some((a) => s[a] === 'NONE')) {
        // A transaction provably expired without being included. Safe to rebuild.
        throw new FundingError('TX_EXPIRED', 'Transaction expired before inclusion; a new transaction will be created', { transient: true });
      }
      if (this.now() >= deadline || this.stopping) {
        log.info('Confirmation still pending; will re-check');
        await this.repo.updateJob(job.id, { next_attempt_at: this.now() + Math.max(5000, this.config.tx.confirmationPollMs * 5) });
        return;
      }
      await this.sleep(this.config.tx.confirmationPollMs);
    }
  }

  #plannedAmount(job, asset) {
    return BigInt(asset === 'TRX' ? job.trx_amount_sun : job.usdt_amount_units);
  }

  /**
   * For each planned asset return 'CONFIRMED' | 'LIVE' | 'NONE' | 'DRY_RUN'.
   * Throws FundingError if a transaction failed on chain just now.
   */
  async #reconcileAll(job, plan, log) {
    const transfers = await this.repo.transfersForJob(job.id);
    const state = {};
    for (const asset of plan) {
      const mine = transfers.filter((t) => t.asset === asset);
      if (mine.some((t) => t.status === TransferStatus.CONFIRMED)) {
        state[asset] = 'CONFIRMED';
        continue;
      }
      if (job.mode === 'dry_run' && mine.some((t) => t.status === TransferStatus.DRY_RUN)) {
        state[asset] = 'CONFIRMED';
        continue;
      }
      const live = mine.filter((t) => LIVE_TRANSFER_STATUSES.includes(t.status));
      let result = 'NONE';
      for (const t of live) {
        const r = await this.#reconcileTransfer(job, t, log);
        if (r === 'CONFIRMED') {
          result = 'CONFIRMED';
          break;
        }
        if (r === 'LIVE') result = 'LIVE';
      }
      state[asset] = result;
    }
    return state;
  }

  /** Reconcile one possibly-on-chain transfer. Returns 'CONFIRMED' | 'LIVE' | 'NONE'. */
  async #reconcileTransfer(job, t, log) {
    const ctx = {
      kind: t.asset,
      contractHex: this.usdt?.contractHex,
      fromHex: this.signer.hexAddress,
      toHex: toHexAddress(t.to_address),
      amount: BigInt(t.amount),
    };
    const look = await this.tron.lookupTransaction(t.txid, ctx);
    if (look.state === 'CONFIRMED') {
      await this.repo.updateTransfer(t.id, {
        status: TransferStatus.CONFIRMED,
        block_number: look.blockNumber,
        fee_sun: look.feeSun,
        confirmed_at: this.now(),
        error_code: null,
        error_message: null,
      });
      await this.#recordTxOnJob(job, t.asset, t.txid, t.amount);
      log.info({ asset: t.asset, txid: t.txid, block: look.blockNumber }, `${t.asset} transaction confirmed`);
      return 'CONFIRMED';
    }
    if (look.state === 'FAILED') {
      await this.repo.updateTransfer(t.id, {
        status: TransferStatus.FAILED,
        block_number: look.blockNumber,
        fee_sun: look.feeSun,
        error_code: 'ONCHAIN_FAILURE',
        error_message: look.reason,
      });
      log.error({ asset: t.asset, txid: t.txid, reason: look.reason }, `${t.asset} transaction failed on chain`);
      throw new FundingError('TX_FAILED_ONCHAIN', `${t.asset} transaction ${t.txid} failed on chain: ${look.reason}`);
    }
    if (look.state === 'PENDING') {
      if (t.status !== TransferStatus.BROADCAST) {
        await this.repo.updateTransfer(t.id, { status: TransferStatus.BROADCAST });
      }
      return 'LIVE';
    }
    // NOT_FOUND. Expiry check FIRST, then look again: once the solidified head is
    // past expiration, any block that could contain the tx is already solidified.
    if (t.expiration_at && (await this.tron.isDefinitelyExpired(t.expiration_at))) {
      const again = await this.tron.lookupTransaction(t.txid, ctx);
      if (again.state === 'NOT_FOUND') {
        await this.repo.updateTransfer(t.id, { status: TransferStatus.EXPIRED, error_code: 'EXPIRED', error_message: 'Not included before expiration' });
        log.warn({ asset: t.asset, txid: t.txid }, `${t.asset} transaction expired without inclusion (safe to rebuild)`);
        return 'NONE';
      }
      return this.#reconcileTransfer(job, t, log);
    }
    // Not found and not expired: re-broadcast the SAME signed transaction
    // (idempotent: identical txid), unless paused or broadcast very recently.
    const recently = this.now() - (t.updated_at ?? 0) < 10_000 && t.broadcast_attempts > 0;
    if (!this.settings.paused && !recently && t.signed_tx && this.hasLease()) {
      await this.#broadcast(job, t, JSON.parse(t.signed_tx), log, { rebroadcast: true });
    }
    return 'LIVE';
  }

  async #recordTxOnJob(job, asset, txid, amount) {
    const patch = asset === 'TRX' ? { trx_txid: txid, trx_sent_sun: String(amount) } : { usdt_txid: txid, usdt_sent_units: String(amount) };
    await this.repo.updateJob(job.id, patch);
    Object.assign(job, patch);
  }

  /** On-chain and balance checks before any new transaction is created. */
  async #preflight(job, missing, log) {
    const f = this.config.funding;
    // Destination checks (again, independent of intake).
    try {
      assertValidDestination(job.address, { motherAddress: this.signer.address, usdtContract: this.config.usdt.contract });
    } catch (err) {
      if (err instanceof ValidationError) throw new FundingError(err.code, err.message);
      throw err;
    }
    if (!f.allowContractDestinations && (await this.tron.isContract(job.address))) {
      throw new FundingError('DESTINATION_IS_CONTRACT', 'Destination is a smart contract address (set ALLOW_CONTRACT_DESTINATIONS=true to allow)');
    }
    const needsUsdt = missing.some((m) => m.asset === 'USDT');
    if (needsUsdt && !this.usdt.verified) {
      await this.usdt.verify({ network: this.config.network, expectedSymbol: this.config.usdt.expectedSymbol, allowNonstandard: this.config.usdt.allowNonstandardContract, logger: log });
    }
    const balances = await this.#motherBalances(needsUsdt);
    const reserved = await this.#reserved();
    const required = computeRequirements(missing, f);
    assertSufficientBalance({ balances, reserved, required, usdtDecimals: this.config.usdt.decimals });
    if (job.mode === 'live') {
      const spent24h = await this.repo.sumTransfers({
        statuses: [TransferStatus.SIGNED, TransferStatus.BROADCAST, TransferStatus.REJECTED, TransferStatus.CONFIRMED],
        since: this.now() - DAY_MS,
      });
      assertDailyLimits({ spent24h, items: missing, dailyTrxLimitSun: f.dailyTrxLimitSun, dailyUsdtLimitUnits: f.dailyUsdtLimitUnits });
    }
    log.info(
      { trxBalance: sunToTrx(balances.trxSun), usdtBalance: needsUsdt ? formatUnits(balances.usdtUnits, 6) : undefined },
      'Mother Wallet balance check passed',
    );
  }

  async #motherBalances(needsUsdt) {
    const trxSun = await this.tron.getTrxBalance(this.signer.address);
    const usdtUnits = needsUsdt ? await this.usdt.balanceOf(this.signer.address) : 0n;
    return { trxSun, usdtUnits };
  }

  async #reserved() {
    const inflight = await this.repo.sumTransfers({ statuses: [TransferStatus.SIGNED, TransferStatus.BROADCAST] });
    const inflightUsdtCount = await this.repo.countLiveUsdtTransfers();
    return computeReserved({ inflight, inflightUsdtCount, usdtFeeLimitSun: this.config.funding.usdtFeeLimitSun });
  }

  /** Critical section (runs under spendLock). Returns 'LIVE' | 'CONFIRMED'. */
  async #sendTransfer(job, item, log) {
    const { asset, amount } = item;
    const dryRun = job.mode === 'dry_run';
    const f = this.config.funding;

    // Re-check everything inside the lock: another job may have spent in between.
    if (this.settings.paused) throw new FundingError('PAUSED', 'Funding was paused', { transient: true });
    if (!this.hasLease()) throw new FundingError('NO_LEASE', 'Lost the funding lease', { transient: true });
    const existing = (await this.repo.transfersForJob(job.id)).filter(
      (t) => t.asset === asset && [...LIVE_TRANSFER_STATUSES, TransferStatus.CONFIRMED].includes(t.status),
    );
    if (existing.length > 0) {
      // Invariant 2: never create a second live tx for the same (job, asset).
      return existing.some((t) => t.status === TransferStatus.CONFIRMED) ? 'CONFIRMED' : 'LIVE';
    }
    const balances = await this.#motherBalances(asset === 'USDT');
    const reserved = await this.#reserved();
    assertSufficientBalance({ balances, reserved, required: computeRequirements([item], f), usdtDecimals: this.config.usdt.decimals });

    log.info({ asset, amount: this.amountLabel(asset, amount), to: job.address }, `Sending ${asset}`);

    let prepared;
    if (asset === 'TRX') {
      prepared = await this.tron.prepareTrxTransfer({ from: this.signer.address, to: job.address, amountSun: amount });
    } else {
      const est = await this.usdt.estimateTransfer(job.address, amount);
      if (!est.ok) throw new FundingError('USDT_SIMULATION_FAILED', `USDT transfer simulation failed: ${est.message}`);
      const energyFeeSun = await this.tron.provider.getEnergyFeeSun();
      assertFeeLimitSufficient({ energyUsed: est.energyUsed, energyFeeSun, feeLimitSun: f.usdtFeeLimitSun });
      prepared = await this.tron.prepareTrc20Transfer({
        from: this.signer.address,
        contract: this.config.usdt.contract,
        to: job.address,
        amountUnits: amount,
        feeLimitSun: f.usdtFeeLimitSun,
      });
    }
    const signed = this.signer.sign(prepared.tx);

    if (dryRun) {
      log.info({ asset, txid: signed.txID }, `[DRY RUN] Would send ${this.amountLabel(asset, amount)} to ${job.address}`);
      await this.repo.insertTransfer({ jobId: job.id, asset, toAddress: job.address, amount, status: TransferStatus.DRY_RUN });
      const patch = asset === 'TRX' ? { trx_sent_sun: String(amount) } : { usdt_sent_units: String(amount) };
      await this.repo.updateJob(job.id, patch);
      Object.assign(job, patch);
      return 'CONFIRMED';
    }

    // Invariant 1: persist before broadcast.
    const transfer = await this.repo.insertTransfer({
      jobId: job.id,
      asset,
      toAddress: job.address,
      amount,
      status: TransferStatus.SIGNED,
      txid: signed.txID,
      signedTx: signed,
      expirationAt: prepared.expirationMs,
    });
    const txField = asset === 'TRX' ? { trx_txid: signed.txID } : { usdt_txid: signed.txID };
    await this.repo.updateJob(job.id, txField);
    Object.assign(job, txField);

    await this.#broadcast(job, transfer, signed, log, { rebroadcast: false });
    return 'LIVE';
  }

  async #broadcast(job, transfer, signed, log, { rebroadcast }) {
    const attempts = (transfer.broadcast_attempts ?? 0) + 1;
    let res;
    try {
      res = await this.tron.broadcast(signed);
    } catch (err) {
      // Outcome unknown (timeout / connection drop). The transaction may or may
      // not have reached the network. Leave it SIGNED; reconciliation will look
      // it up on chain and re-broadcast the same bytes or wait for expiry.
      await this.repo.updateTransfer(transfer.id, { broadcast_attempts: attempts, error_code: err.code ?? 'BROADCAST_UNKNOWN', error_message: scrubError(err).message });
      log.warn({ asset: transfer.asset, txid: transfer.txid, error: scrubError(err).message }, 'Broadcast outcome unknown; will verify on chain before any retry');
      return;
    }
    if (res.accepted || res.duplicate) {
      await this.repo.updateTransfer(transfer.id, { status: TransferStatus.BROADCAST, broadcast_attempts: attempts, error_code: null, error_message: null }, { fromStatuses: LIVE_TRANSFER_STATUSES });
      log.info({ asset: transfer.asset, txid: transfer.txid, rebroadcast }, `${transfer.asset} transaction broadcast`);
      return;
    }
    await this.repo.updateTransfer(transfer.id, { status: TransferStatus.REJECTED, broadcast_attempts: attempts, error_code: res.code, error_message: res.message }, { fromStatuses: LIVE_TRANSFER_STATUSES });
    if (rebroadcast) {
      // Keep waiting for provable expiry; never create a new tx from here.
      log.warn({ asset: transfer.asset, txid: transfer.txid, code: res.code }, `${transfer.asset} re-broadcast rejected by node; waiting for expiry check`);
      return;
    }
    log.error({ asset: transfer.asset, txid: transfer.txid, code: res.code, message: res.message }, `${transfer.asset} broadcast rejected by node`);
    throw new FundingError(`BROADCAST_${res.code}`, `${transfer.asset} transaction rejected by node: ${res.code}${res.message ? ` (${res.message})` : ''}`, {
      transient: TRANSIENT_BROADCAST_CODES.has(res.code),
    });
  }

  async #complete(job, log) {
    const fresh = await this.repo.getJob(job.id);
    const ok = await this.repo.transitionJob(
      job.id,
      [JobStatus.CONFIRMING, JobStatus.FUNDING_TRX, JobStatus.FUNDING_USDT, JobStatus.QUEUED, JobStatus.RETRYING],
      JobStatus.COMPLETED,
      { funded_at: this.now(), error_code: null, error_message: null, next_attempt_at: 0 },
      job.mode === 'dry_run' ? 'Dry run completed (nothing broadcast)' : 'Funding completed',
    );
    if (!ok) return;
    const done = { ...fresh, ...job, funded_at: this.now() };
    log.info(
      { trx: done.trx_sent_sun ? sunToTrx(done.trx_sent_sun) : '0', usdt: done.usdt_sent_units ? formatUnits(done.usdt_sent_units, 6) : '0', trxTx: done.trx_txid, usdtTx: done.usdt_txid },
      job.mode === 'dry_run' ? '[DRY RUN] Funding simulation completed' : 'Funding completed',
    );
    await this.#notify(job.id, 'funded', formatFundedMessage({ job: done, network: this.config.network, dryRun: job.mode === 'dry_run', usdtDecimals: this.config.usdt.decimals }));
  }

  async #handleError(job, err, log) {
    const code = err.code ?? (err instanceof RpcError ? err.code : 'INTERNAL_ERROR');
    const message = scrub(err.message ?? String(err));
    const fresh = (await this.repo.getJob(job.id).catch(() => null)) ?? job;
    const transfers = await this.repo.transfersForJob(job.id).catch(() => []);
    const hasLive = transfers.some((t) => LIVE_TRANSFER_STATUSES.includes(t.status));
    const transient = isTransient(err);
    const retryCount = (fresh.retry_count ?? 0) + (transient ? 1 : 0);

    if (transient && (hasLive || retryCount <= this.config.queue.maxAutoRetries || code === 'PAUSED' || code === 'NO_LEASE')) {
      // Transactions possibly in flight are NEVER abandoned by marking FAILED.
      const delay = Math.min(10 * 60_000, this.config.queue.retryBaseDelayMs * 2 ** Math.min(retryCount - 1, 8));
      const target = hasLive ? fresh.status : JobStatus.RETRYING;
      await this.repo.transitionJob(job.id, [fresh.status], target, {
        retry_count: retryCount,
        next_attempt_at: this.now() + delay,
        error_code: code,
        error_message: message,
      }, `Transient error: ${message}`);
      log.warn({ code, error: message, retryCount, retryInMs: delay }, 'Transient funding error; will retry');
      return;
    }

    const failed = await this.repo.transitionJob(job.id, [fresh.status], JobStatus.FAILED, { error_code: code, error_message: message, next_attempt_at: 0 }, message);
    if (!failed) return;
    log.error({ code, error: message }, 'Funding failed');
    await this.#notify(
      job.id,
      `failed:${this.now()}`,
      formatFailedMessage({ job: fresh, network: this.config.network, reason: message, code, dryRun: job.mode === 'dry_run' }),
    );
    if (err instanceof FundingError && err.pause && this.config.funding.autoPauseOnInsufficientBalance && !this.settings.paused) {
      await this.settings.pause(`auto: ${code}`, 'system');
      await this.#notify(null, 'auto_pause', formatSystemAlert('FUNDING AUTO-PAUSED', `${message}\n\nTop up / fix the issue, then /resume and /retry failed jobs.`));
    }
  }

  async #notify(jobId, kind, text) {
    try {
      await this.repo.enqueueNotification({ jobId, kind, text });
    } catch (err) {
      this.logger.warn({ error: scrubError(err).message }, 'Unable to queue notification');
    }
  }

  /**
   * Transfers can still be live when their job has already reached a terminal
   * state (e.g. USDT failed on chain while the TRX tx was still unconfirmed).
   * Track them to their final state so balances/reservations stay correct.
   * Never broadcasts anything.
   */
  async sweepOrphanTransfers() {
    const live = await this.repo.liveTransfers();
    for (const t of live) {
      const job = await this.repo.getJob(t.job_id);
      if (!job || !TERMINAL.has(job.status)) continue;
      try {
        const ctx = { kind: t.asset, contractHex: this.usdt?.contractHex, fromHex: this.signer.hexAddress, toHex: toHexAddress(t.to_address), amount: BigInt(t.amount) };
        const look = await this.tron.lookupTransaction(t.txid, ctx);
        if (look.state === 'CONFIRMED') {
          await this.repo.updateTransfer(t.id, { status: TransferStatus.CONFIRMED, block_number: look.blockNumber, fee_sun: look.feeSun, confirmed_at: this.now() });
          await this.#recordTxOnJob(job, t.asset, t.txid, t.amount);
          this.logger.warn({ job: job.id, asset: t.asset, txid: t.txid }, 'Late confirmation of a transaction belonging to a finished job');
        } else if (look.state === 'FAILED') {
          await this.repo.updateTransfer(t.id, { status: TransferStatus.FAILED, block_number: look.blockNumber, fee_sun: look.feeSun, error_code: 'ONCHAIN_FAILURE', error_message: look.reason });
        } else if (look.state === 'NOT_FOUND' && t.expiration_at && (await this.tron.isDefinitelyExpired(t.expiration_at))) {
          const again = await this.tron.lookupTransaction(t.txid, ctx);
          if (again.state === 'NOT_FOUND') await this.repo.updateTransfer(t.id, { status: TransferStatus.EXPIRED, error_code: 'EXPIRED' });
        }
      } catch (err) {
        this.logger.warn({ transfer: t.id, error: scrubError(err).message }, 'Orphan transfer check failed');
      }
    }
  }

  // ----------------------------------------------------------- admin helpers --
  async retryJob(idOrAddress, by = 'admin') {
    const job = await this.repo.findJob(idOrAddress, this.config.dryRun ? 'dry_run' : 'live');
    if (!job) throw new FundingError('NOT_FOUND', 'Job not found');
    if (job.status !== JobStatus.FAILED && job.status !== JobStatus.RETRYING) {
      throw new FundingError('NOT_RETRYABLE', `Job is ${job.status}; only FAILED or RETRYING jobs can be retried`);
    }
    await this.repo.transitionJob(job.id, [JobStatus.FAILED, JobStatus.RETRYING], JobStatus.RETRYING, { next_attempt_at: 0, retry_count: 0 }, `Manual retry by ${by}`);
    this.logger.info({ job: job.id, address: maskAddress(job.address), by }, 'Job queued for retry');
    return this.repo.getJob(job.id);
  }
}
