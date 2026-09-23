import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseUnits, formatUnits, trxToSun, sunToTrx } from '../src/blockchain/units.js';
import {
  computeRequirements,
  computeReserved,
  assertSufficientBalance,
  assertDailyLimits,
  remainingAssets,
  assertFeeLimitSufficient,
} from '../src/wallet/planner.js';

const fees = { usdtFeeLimitSun: 30_000_000n, trxFeeBufferSun: 2_000_000n, minMotherTrxReserveSun: 0n };

test('USDT uses 6 decimals exactly (no floating point)', () => {
  assert.equal(parseUnits('10', 6), 10_000_000n);
  assert.equal(parseUnits('0.000001', 6), 1n);
  assert.equal(parseUnits('123456789.123456', 6), 123456789123456n);
  assert.equal(formatUnits(10_000_000n, 6), '10');
  assert.equal(formatUnits(1_500_000n, 6), '1.5');
  assert.equal(trxToSun('5'), 5_000_000n);
  assert.equal(sunToTrx(5_000_001n), '5.000001');
  // classic float trap: 0.1 + 0.2
  assert.equal(parseUnits('0.1', 6) + parseUnits('0.2', 6), parseUnits('0.3', 6));
});

test('rejects malformed amounts', () => {
  for (const v of ['-1', '1e6', '1.0000001', 'abc', '', '1,5', ' 1 2']) {
    assert.throws(() => parseUnits(v, 6), undefined, v);
  }
});

test('funding plan: TRX only, USDT only, both', () => {
  const job = (trx, usdt) => ({ trx_amount_sun: String(trx), usdt_amount_units: String(usdt) });
  const none = { TRX: 'NONE', USDT: 'NONE' };
  assert.deepEqual(remainingAssets(job(5_000_000, 0), none).map((i) => i.asset), ['TRX']);
  assert.deepEqual(remainingAssets(job(0, 10_000_000), none).map((i) => i.asset), ['USDT']);
  assert.deepEqual(remainingAssets(job(5_000_000, 10_000_000), none).map((i) => i.asset), ['TRX', 'USDT']);
  // already confirmed TRX is not sent again
  assert.deepEqual(remainingAssets(job(5_000_000, 10_000_000), { TRX: 'CONFIRMED', USDT: 'NONE' }).map((i) => i.asset), ['USDT']);
  assert.deepEqual(remainingAssets(job(5_000_000, 10_000_000), { TRX: 'LIVE', USDT: 'LIVE' }), []);
});

test('requirements include fees and reserve', () => {
  const both = computeRequirements([{ asset: 'TRX', amount: 5_000_000n }, { asset: 'USDT', amount: 10_000_000n }], fees);
  assert.equal(both.trxSun, 5_000_000n + 30_000_000n + 2_000_000n);
  assert.equal(both.usdtUnits, 10_000_000n);
  const usdtOnly = computeRequirements([{ asset: 'USDT', amount: 10_000_000n }], fees);
  assert.equal(usdtOnly.trxSun, 32_000_000n);
  assert.deepEqual(computeRequirements([], fees), { trxSun: 0n, usdtUnits: 0n });
});

test('insufficient balance detection accounts for in-flight reservations', () => {
  const required = { trxSun: 37_000_000n, usdtUnits: 10_000_000n };
  const reserved = computeReserved({ inflight: { TRX: 5_000_000n, USDT: 10_000_000n }, inflightUsdtCount: 1, usdtFeeLimitSun: 30_000_000n });
  assert.deepEqual(reserved, { trxSun: 35_000_000n, usdtUnits: 10_000_000n });
  // Enough without reservations, not enough with them
  const balances = { trxSun: 50_000_000n, usdtUnits: 15_000_000n };
  assert.doesNotThrow(() => assertSufficientBalance({ balances, reserved: { trxSun: 0n, usdtUnits: 0n }, required }));
  assert.throws(() => assertSufficientBalance({ balances, reserved, required }), { code: 'INSUFFICIENT_USDT' });
  assert.throws(
    () => assertSufficientBalance({ balances: { trxSun: 1_000_000n, usdtUnits: 100_000_000n }, reserved: { trxSun: 0n, usdtUnits: 0n }, required }),
    { code: 'INSUFFICIENT_TRX' },
  );
});

test('daily limits', () => {
  const spent24h = { TRX: 90_000_000n, USDT: 0n };
  assert.throws(() => assertDailyLimits({ spent24h, items: [{ asset: 'TRX', amount: 20_000_000n }], dailyTrxLimitSun: 100_000_000n, dailyUsdtLimitUnits: 0n }), {
    code: 'DAILY_LIMIT_TRX',
  });
  assert.doesNotThrow(() => assertDailyLimits({ spent24h, items: [{ asset: 'TRX', amount: 20_000_000n }], dailyTrxLimitSun: 0n, dailyUsdtLimitUnits: 0n }));
});

test('fee limit check against energy estimate', () => {
  assert.doesNotThrow(() => assertFeeLimitSufficient({ energyUsed: 65_000, energyFeeSun: 100n, feeLimitSun: 30_000_000n }));
  assert.throws(() => assertFeeLimitSufficient({ energyUsed: 1_000_000, energyFeeSun: 100n, feeLimitSun: 30_000_000n }), { code: 'FEE_LIMIT_TOO_LOW' });
});
