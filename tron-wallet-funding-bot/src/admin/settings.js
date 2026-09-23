// Runtime settings that operators can change without a restart (via Telegram or
// the admin API). Persisted in the `settings` table so they survive restarts.
// Environment values are the defaults; per-wallet caps from the environment can
// never be exceeded at runtime.

import { parseUnits, TRX_DECIMALS, sunToTrx, formatUnits } from '../blockchain/units.js';

export class SettingsError extends Error {
  constructor(message) {
    super(message);
    this.name = 'SettingsError';
    this.code = 'INVALID_SETTING';
  }
}

export class RuntimeSettings {
  constructor({ repo, config, logger }) {
    this.repo = repo;
    this.config = config;
    this.logger = logger;
    this.state = {
      paused: config.startPaused,
      pauseReason: config.startPaused ? 'START_PAUSED=true' : null,
      trxAmountSun: config.funding.trxAmountSun,
      usdtAmountUnits: config.funding.usdtAmountUnits,
    };
  }

  async load() {
    const paused = await this.repo.getSetting('paused');
    if (paused !== undefined && !this.config.startPaused) this.state.paused = paused === 'true';
    this.state.pauseReason = (await this.repo.getSetting('pause_reason')) ?? this.state.pauseReason;
    const trx = await this.repo.getSetting('trx_amount_sun');
    const usdt = await this.repo.getSetting('usdt_amount_units');
    if (trx !== undefined) this.state.trxAmountSun = BigInt(trx);
    if (usdt !== undefined) this.state.usdtAmountUnits = BigInt(usdt);
    // Env caps always win, even over persisted values.
    if (this.state.trxAmountSun > this.config.funding.maxTrxPerWalletSun) this.state.trxAmountSun = this.config.funding.maxTrxPerWalletSun;
    if (this.state.usdtAmountUnits > this.config.funding.maxUsdtPerWalletUnits) this.state.usdtAmountUnits = this.config.funding.maxUsdtPerWalletUnits;
    return this.snapshot();
  }

  get paused() {
    return this.state.paused;
  }

  fundingAmounts() {
    return { trxAmountSun: this.state.trxAmountSun, usdtAmountUnits: this.state.usdtAmountUnits };
  }

  snapshot() {
    return {
      paused: this.state.paused,
      pauseReason: this.state.pauseReason,
      trxAmount: sunToTrx(this.state.trxAmountSun),
      usdtAmount: formatUnits(this.state.usdtAmountUnits, this.config.usdt.decimals),
      maxTrxPerWallet: sunToTrx(this.config.funding.maxTrxPerWalletSun),
      maxUsdtPerWallet: formatUnits(this.config.funding.maxUsdtPerWalletUnits, this.config.usdt.decimals),
      dryRun: this.config.dryRun,
    };
  }

  async pause(reason, by = 'system') {
    this.state.paused = true;
    this.state.pauseReason = String(reason).slice(0, 200);
    await this.repo.setSetting('paused', 'true', by);
    await this.repo.setSetting('pause_reason', this.state.pauseReason, by);
    this.logger?.warn({ by, reason: this.state.pauseReason }, 'Funding paused');
  }

  async resume(by = 'system') {
    this.state.paused = false;
    this.state.pauseReason = null;
    await this.repo.setSetting('paused', 'false', by);
    await this.repo.setSetting('pause_reason', '', by);
    this.logger?.info({ by }, 'Funding resumed');
  }

  async setAmount(asset, value, by = 'system') {
    let units;
    try {
      units = parseUnits(String(value), asset === 'TRX' ? TRX_DECIMALS : this.config.usdt.decimals);
    } catch (err) {
      throw new SettingsError(err.message);
    }
    if (asset === 'TRX') {
      if (units > this.config.funding.maxTrxPerWalletSun) throw new SettingsError(`TRX amount exceeds MAX_TRX_PER_WALLET (${sunToTrx(this.config.funding.maxTrxPerWalletSun)})`);
      this.state.trxAmountSun = units;
      await this.repo.setSetting('trx_amount_sun', units.toString(), by);
    } else if (asset === 'USDT') {
      if (units > this.config.funding.maxUsdtPerWalletUnits) {
        throw new SettingsError(`USDT amount exceeds MAX_USDT_PER_WALLET (${formatUnits(this.config.funding.maxUsdtPerWalletUnits, this.config.usdt.decimals)})`);
      }
      this.state.usdtAmountUnits = units;
      await this.repo.setSetting('usdt_amount_units', units.toString(), by);
    } else {
      throw new SettingsError('Unknown asset');
    }
    this.logger?.info({ by, asset, amount: String(value) }, 'Funding amount changed');
    return this.snapshot();
  }
}
