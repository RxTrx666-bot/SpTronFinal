// Exact decimal <-> integer base-unit conversion using BigInt.
// Never use floating point for token amounts.
//
// TRX: 1 TRX = 1_000_000 sun (6 decimals)
// USDT TRC-20: 6 decimals (verified against the contract at startup)

export const TRX_DECIMALS = 6;
export const SUN_PER_TRX = 1_000_000n;

export class AmountError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AmountError';
  }
}

/**
 * Parse a human decimal string ("5", "10.5", "0.000001") into base units.
 * Rejects negatives, exponents, more fractional digits than `decimals`,
 * and anything that is not a plain decimal literal.
 */
export function parseUnits(value, decimals) {
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new AmountError('Amount must be finite');
    value = String(value);
  }
  if (typeof value !== 'string') throw new AmountError('Amount must be a string or number');
  const s = value.trim();
  if (!/^\d+(\.\d+)?$/.test(s)) throw new AmountError(`Invalid amount "${s.slice(0, 32)}"`);
  const [whole, frac = ''] = s.split('.');
  if (frac.length > decimals) {
    throw new AmountError(`Amount has more than ${decimals} decimal places`);
  }
  const units = BigInt(whole) * 10n ** BigInt(decimals) + BigInt(frac.padEnd(decimals, '0') || '0');
  return units;
}

export function formatUnits(units, decimals) {
  const v = BigInt(units);
  const neg = v < 0n;
  const abs = neg ? -v : v;
  const base = 10n ** BigInt(decimals);
  const whole = abs / base;
  let frac = (abs % base).toString().padStart(decimals, '0').replace(/0+$/, '');
  return `${neg ? '-' : ''}${whole.toString()}${frac ? `.${frac}` : ''}`;
}

export const trxToSun = (v) => parseUnits(v, TRX_DECIMALS);
export const sunToTrx = (v) => formatUnits(v, TRX_DECIMALS);

/** Convert a BigInt to a JS number, refusing anything unsafe. */
export function toSafeNumber(units) {
  const v = BigInt(units);
  if (v < 0n || v > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new AmountError('Amount out of safe integer range');
  }
  return Number(v);
}
