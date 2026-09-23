// WALLET IDENTIFICATION — receiving target wallets from upstream systems.
//
// Funding an address only needs the Mother Wallet's key. Target wallet private
// keys are therefore NOT needed and by default are validated (to prove the
// upstream sent a consistent pair) and immediately discarded. They are never
// logged, never returned, and only persisted (AES-256-GCM encrypted) when
// TARGET_PRIVATE_KEY_POLICY=encrypt is explicitly configured.

import { assertValidDestination, addressFromPrivateKey, looksLikePrivateKey, ValidationError } from './validation.js';
import { registerTransientSecret, maskAddress } from '../security/redact.js';
import { DuplicateJobError, JobStatus } from '../database/models.js';

export class IntakeService {
  constructor({ repo, settings, config, signer, cipher, logger, onNewJob = () => {} }) {
    this.repo = repo;
    this.settings = settings;
    this.config = config;
    this.signer = signer;
    this.cipher = cipher; // FieldCipher, only when policy=encrypt
    this.logger = logger;
    this.onNewJob = onNewJob;
  }

  get mode() {
    return this.config.dryRun ? 'dry_run' : 'live';
  }

  /**
   * Submit a target wallet.
   * @returns {{ status: 'created'|'duplicate', job }}
   * @throws ValidationError
   */
  async submit({ address, privateKey, source, repeat = false }) {
    // The private key reference is dropped at the end of this function; it is
    // only kept by hash (for log scrubbing) unless the encrypt policy is on.
    if (privateKey !== undefined && privateKey !== null) {
      registerTransientSecret(String(privateKey));
    }
    if (typeof source === 'string') source = source.replace(/[^\w.:-]/g, '').slice(0, 64) || null;

    assertValidDestination(address, { motherAddress: this.signer.address, usdtContract: this.config.usdt.contract });

    let storeKey = false;
    if (privateKey !== undefined && privateKey !== null) {
      if (this.config.targetKeyPolicy === 'reject') {
        throw new ValidationError('PRIVATE_KEY_NOT_ACCEPTED', 'Private keys are not accepted; send the address only');
      }
      if (typeof privateKey !== 'string' || !looksLikePrivateKey(privateKey)) {
        throw new ValidationError('INVALID_PRIVATE_KEY', 'private_key is malformed');
      }
      if (addressFromPrivateKey(privateKey) !== address) {
        throw new ValidationError('PRIVATE_KEY_ADDRESS_MISMATCH', 'private_key does not correspond to address');
      }
      storeKey = this.config.targetKeyPolicy === 'encrypt';
    }

    this.logger.info({ address, source }, 'New wallet received');
    await this.repo.upsertWallet({ address, source });
    if (storeKey) {
      const enc = this.cipher.encrypt(privateKey.trim().replace(/^0x/i, '').toLowerCase(), address);
      await this.repo.storeWalletSecret(address, enc);
      this.logger.info({ address, keyId: enc.keyId }, 'Target wallet key stored encrypted (TARGET_PRIVATE_KEY_POLICY=encrypt)');
    }
    privateKey = undefined; // eslint-disable-line no-param-reassign

    // Idempotency: one job per address unless repeat funding is enabled AND requested.
    const existing = await this.repo.latestJob(address, this.mode);
    let round = 1;
    if (existing) {
      const canRepeat = repeat === true && this.config.funding.allowRepeatFunding && existing.status === JobStatus.COMPLETED;
      if (!canRepeat) {
        this.logger.info({ address, job: existing.id, status: existing.status }, 'Duplicate wallet submission ignored (already has a funding job)');
        return { status: 'duplicate', job: existing };
      }
      round = existing.round + 1;
    }

    const { trxAmountSun, usdtAmountUnits } = this.settings.fundingAmounts();
    if (trxAmountSun === 0n && usdtAmountUnits === 0n) {
      throw new ValidationError('FUNDING_DISABLED', 'Both TRX and USDT funding amounts are 0');
    }
    this.logger.info({ address }, 'Wallet validated');

    let job;
    try {
      job = await this.repo.createJob({ address, mode: this.mode, round, trxAmountSun, usdtAmountUnits, source });
    } catch (err) {
      if (err instanceof DuplicateJobError) {
        // Lost a race with a concurrent identical submission: return the winner.
        return { status: 'duplicate', job: await this.repo.latestJob(address, this.mode) };
      }
      throw err;
    }
    await this.repo.transitionJob(job.id, JobStatus.RECEIVED, JobStatus.VALIDATING, {}, 'Offline validation passed');
    await this.repo.transitionJob(job.id, JobStatus.VALIDATING, JobStatus.QUEUED, {}, 'Queued for funding');
    this.logger.info({ address: maskAddress(address), job: job.id, round }, 'Funding job created');
    this.onNewJob(job.id);
    return { status: 'created', job: await this.repo.getJob(job.id) };
  }
}
