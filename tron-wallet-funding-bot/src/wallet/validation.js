// TRON address / key validation (offline, no network access).

import { utils } from 'tronweb';

const BASE58_TRON = /^T[1-9A-HJ-NP-Za-km-z]{33}$/;
const HEX64 = /^(0x)?[0-9a-fA-F]{64}$/;

export class ValidationError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ValidationError';
    this.code = code;
  }
}

/** True if the string looks like a raw secp256k1 private key. */
export function looksLikePrivateKey(value) {
  return typeof value === 'string' && HEX64.test(value.trim());
}

/**
 * Validate a TRON mainnet-format base58check address (T...).
 * Only the canonical base58 form is accepted as input, so hex strings,
 * private keys and addresses of other chains are rejected.
 */
export function isValidTronAddress(address) {
  if (typeof address !== 'string') return false;
  if (!BASE58_TRON.test(address)) return false;
  try {
    // Verifies base58check checksum and the 0x41 TRON prefix byte.
    return utils.crypto.isAddressValid(address);
  } catch {
    return false;
  }
}

/**
 * Validate a destination and throw a ValidationError with a safe (non-echoing)
 * message. Input values are never included in the message if they could be a
 * secret.
 */
export function assertValidDestination(address, { motherAddress, usdtContract } = {}) {
  if (typeof address !== 'string' || address.length === 0) {
    throw new ValidationError('INVALID_ADDRESS', 'Address is required');
  }
  const trimmed = address.trim();
  if (looksLikePrivateKey(trimmed)) {
    // Never echo the value: it is very likely a private key.
    throw new ValidationError('PRIVATE_KEY_AS_ADDRESS', 'Value looks like a private key, not a TRON address; refused');
  }
  if (trimmed !== address) {
    throw new ValidationError('INVALID_ADDRESS', 'Address must not contain surrounding whitespace');
  }
  if (!isValidTronAddress(address)) {
    throw new ValidationError('INVALID_ADDRESS', 'Invalid TRON address (expected base58check address starting with T)');
  }
  if (motherAddress && address === motherAddress) {
    throw new ValidationError('DESTINATION_IS_MOTHER', 'Destination is the Mother Wallet itself');
  }
  if (usdtContract && address === usdtContract) {
    throw new ValidationError('DESTINATION_IS_TOKEN_CONTRACT', 'Destination is the USDT contract');
  }
  return address;
}

export function toHexAddress(base58) {
  return utils.address.toHex(base58).toLowerCase();
}

export function fromHexAddress(hex) {
  return utils.address.fromHex(hex);
}

/** Derive the base58 address of a private key. Returns null if invalid. */
export function addressFromPrivateKey(privateKey) {
  if (!looksLikePrivateKey(privateKey)) return null;
  try {
    const addr = utils.address.fromPrivateKey(privateKey.trim().replace(/^0x/i, ''), true);
    return typeof addr === 'string' && isValidTronAddress(addr) ? addr : null;
  } catch {
    return null;
  }
}
