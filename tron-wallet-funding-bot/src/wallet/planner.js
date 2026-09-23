// Pure funding calculations (no I/O) — easy to unit test.

import { FundingError } from '../blockchain/errors.js';
import { sunToTrx, formatUnits } from '../blockchain/units.js';

/** Which assets a job still needs, given the transfers already confirmed/live. */
export function remainingAssets(job, assetState) {
  const out = [];
  if (BigInt(job.trx_amount_sun) > 0n && assetState.TRX === 'NONE') out.push({ asset: 'TRX', amount: BigInt(job.trx_amount_sun) });
  if (BigInt(job.usdt_amount_units) > 0n && assetState.USDT === 'NONE') out.push({ asset: 'USDT', amount: BigInt(job.usdt_amount_units) });
  return out;
}

/**
 * Mother Wallet requirements for sending `items` ([{asset, amount}]).
 * TRX needed = TRX amount + worst-case USDT fee (fee_limit) + bandwidth/account
 * activation buffer + configured minimum reserve.
 */
export function computeRequirements(items, { usdtFeeLimitSun, trxFeeBufferSun, minMotherTrxReserveSun }) {
  let trx = 0n;
  let usdt = 0n;
  for (const it of items) {
    if (it.asset === 'TRX') trx += BigInt(it.amount);
    if (it.asset === 'USDT') {
      usdt += BigInt(it.amount);
      trx += BigInt(usdtFeeLimitSun);
    }
  }
  if (items.length > 0) trx += BigInt(trxFeeBufferSun) + BigInt(minMotherTrxReserveSun);
  return { trxSun: trx, usdtUnits: usdt };
}

/**
 * Amounts already committed by signed/broadcast transactions that the node
 * balance may not reflect yet (conservative: may be counted twice briefly,
 * which only ever errs on the side of NOT sending).
 */
export function computeReserved({ inflight, inflightUsdtCount, usdtFeeLimitSun }) {
  return {
    trxSun: BigInt(inflight.TRX ?? 0n) + BigInt(inflightUsdtCount) * BigInt(usdtFeeLimitSun),
    usdtUnits: BigInt(inflight.USDT ?? 0n),
  };
}

/** Throw FundingError if the Mother Wallet cannot cover the requirement. */
export function assertSufficientBalance({ balances, reserved, required, usdtDecimals = 6 }) {
  const availTrx = balances.trxSun - reserved.trxSun;
  const availUsdt = balances.usdtUnits - reserved.usdtUnits;
  if (required.usdtUnits > 0n && availUsdt < required.usdtUnits) {
    throw new FundingError(
      'INSUFFICIENT_USDT',
      `Insufficient Mother Wallet USDT balance (available ${formatUnits(availUsdt < 0n ? 0n : availUsdt, usdtDecimals)} USDT, required ${formatUnits(required.usdtUnits, usdtDecimals)} USDT)`,
      { pause: true },
    );
  }
  if (availTrx < required.trxSun) {
    throw new FundingError(
      'INSUFFICIENT_TRX',
      `Insufficient Mother Wallet TRX balance (available ${sunToTrx(availTrx < 0n ? 0n : availTrx)} TRX, required ${sunToTrx(required.trxSun)} TRX incl. fees/reserve)`,
      { pause: true },
    );
  }
  return { availTrx, availUsdt };
}

/** Enforce rolling 24h spending limits (0 = unlimited). */
export function assertDailyLimits({ spent24h, items, dailyTrxLimitSun, dailyUsdtLimitUnits, usdtDecimals = 6 }) {
  const add = { TRX: 0n, USDT: 0n };
  for (const it of items) add[it.asset] += BigInt(it.amount);
  if (dailyTrxLimitSun > 0n && spent24h.TRX + add.TRX > dailyTrxLimitSun) {
    throw new FundingError('DAILY_LIMIT_TRX', `Daily TRX limit reached (${sunToTrx(spent24h.TRX)}/${sunToTrx(dailyTrxLimitSun)} TRX in last 24h)`, { pause: true });
  }
  if (dailyUsdtLimitUnits > 0n && spent24h.USDT + add.USDT > dailyUsdtLimitUnits) {
    throw new FundingError(
      'DAILY_LIMIT_USDT',
      `Daily USDT limit reached (${formatUnits(spent24h.USDT, usdtDecimals)}/${formatUnits(dailyUsdtLimitUnits, usdtDecimals)} USDT in last 24h)`,
      { pause: true },
    );
  }
}

/** Energy estimate check: refuse if the USDT transfer would likely exceed fee_limit. */
export function assertFeeLimitSufficient({ energyUsed, energyFeeSun, feeLimitSun, safetyFactorPct = 120n }) {
  const est = (BigInt(energyUsed) * BigInt(energyFeeSun) * safetyFactorPct) / 100n;
  if (est > BigInt(feeLimitSun)) {
    throw new FundingError(
      'FEE_LIMIT_TOO_LOW',
      `Estimated USDT transfer cost ${sunToTrx(est)} TRX exceeds USDT_FEE_LIMIT_TRX ${sunToTrx(feeLimitSun)} TRX`,
    );
  }
  return est;
}
