import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  isValidTronAddress,
  assertValidDestination,
  looksLikePrivateKey,
  addressFromPrivateKey,
  ValidationError,
} from '../src/wallet/validation.js';
import { newAccount, USDT_MAINNET } from './helpers/setup.js';

test('accepts valid base58 TRON addresses', () => {
  const a = newAccount();
  assert.equal(isValidTronAddress(a.address), true);
  assert.equal(isValidTronAddress(USDT_MAINNET), true);
});

test('rejects invalid / foreign addresses', () => {
  const bad = [
    '',
    'T',
    'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6x', // bad checksum
    '0x742d35Cc6634C0532925a3b844Bc454e4438f44e', // Ethereum / BSC
    '41a614f803b6fd780986a42c78ec9c7f77e6ded13c', // TRON hex form (not accepted as input)
    'So11111111111111111111111111111111111111112', // Solana
    'bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq', // Bitcoin
    ' TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t',
    null,
    12345,
  ];
  for (const b of bad) assert.equal(isValidTronAddress(b), false, String(b));
});

test('a private key is never accepted as a destination and is not echoed', () => {
  const { privateKey } = newAccount();
  assert.equal(looksLikePrivateKey(privateKey), true);
  assert.throws(
    () => assertValidDestination(privateKey),
    (err) => err instanceof ValidationError && err.code === 'PRIVATE_KEY_AS_ADDRESS' && !err.message.includes(privateKey),
  );
  assert.throws(() => assertValidDestination(`0x${privateKey}`), { code: 'PRIVATE_KEY_AS_ADDRESS' });
});

test('destination may not be the Mother Wallet or the USDT contract', () => {
  const mother = newAccount();
  assert.throws(() => assertValidDestination(mother.address, { motherAddress: mother.address }), { code: 'DESTINATION_IS_MOTHER' });
  assert.throws(() => assertValidDestination(USDT_MAINNET, { usdtContract: USDT_MAINNET }), { code: 'DESTINATION_IS_TOKEN_CONTRACT' });
});

test('derives the address of a private key', () => {
  const a = newAccount();
  assert.equal(addressFromPrivateKey(a.privateKey), a.address);
  assert.equal(addressFromPrivateKey('zz'), null);
});
