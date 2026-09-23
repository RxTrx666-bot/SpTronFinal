import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync, mkdtempSync, chmodSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { loadConfig, ConfigError } from '../src/config/config.js';
import { baseEnv, newAccount } from './helpers/setup.js';

test('valid configuration loads; amounts parsed exactly', () => {
  const { config, secrets } = loadConfig(baseEnv(newAccount(), { TRX_FUNDING_AMOUNT: '5', USDT_FUNDING_AMOUNT: '10.5' }));
  assert.equal(config.funding.trxAmountSun, 5_000_000n);
  assert.equal(config.funding.usdtAmountUnits, 10_500_000n);
  assert.equal(config.network.name, 'mainnet');
  assert.ok(Object.isFrozen(config));
  // secrets are not part of config
  assert.ok(!JSON.stringify(config, (k, v) => (typeof v === 'bigint' ? v.toString() : v)).includes(secrets.motherPrivateKey));
});

test('secrets are removed from the environment after loading', () => {
  const env = baseEnv(newAccount());
  loadConfig(env);
  assert.equal(env.MOTHER_PRIVATE_KEY, undefined);
  assert.equal(env.INTERNAL_API_KEY, undefined);
});

test('invalid configuration lists problems without leaking secrets', () => {
  const mother = newAccount();
  const env = baseEnv(mother, {
    TRON_NETWORK: 'ethereum',
    TRON_RPC_URL: 'http://node.example',
    USDT_CONTRACT_ADDRESS: '0xdAC17F958D2ee523a2206206994597C13D831ec7',
    TRX_FUNDING_AMOUNT: '-1',
    INTERNAL_API_KEY: 'short',
    API_HOST: '0.0.0.0',
  });
  let err;
  try {
    loadConfig(env);
  } catch (e) {
    err = e;
  }
  assert.ok(err instanceof ConfigError);
  const text = err.message;
  assert.match(text, /TRON_NETWORK must be one of: mainnet, nile/);
  assert.match(text, /TRON_RPC_URL must use https/);
  assert.match(text, /USDT_CONTRACT_ADDRESS is not a valid TRON address/);
  assert.match(text, /TRX_FUNDING_AMOUNT/);
  assert.match(text, /INTERNAL_API_KEY must be at least 32/);
  assert.match(text, /API_HOST is not loopback/);
  assert.ok(!text.includes(mother.privateKey));
  assert.ok(!text.includes('short'));
});

test('missing Mother key and credentials in RPC URL are refused', () => {
  const env = baseEnv(newAccount(), { TRON_RPC_URL: 'https://api.trongrid.io/?apikey=abc' });
  delete env.MOTHER_PRIVATE_KEY;
  assert.throws(() => loadConfig(env), (e) => /MOTHER_PRIVATE_KEY/.test(e.message) && /query parameters/.test(e.message));
});

test('amount above per-wallet cap is refused', () => {
  assert.throws(() => loadConfig(baseEnv(newAccount(), { TRX_FUNDING_AMOUNT: '500', MAX_TRX_PER_WALLET: '100' })), /exceeds MAX_TRX_PER_WALLET/);
});

test('secrets can be loaded from *_FILE with strict permissions', () => {
  const dir = mkdtempSync(join(tmpdir(), 'fundbot-'));
  const mother = newAccount();
  const f = join(dir, 'mother.key');
  writeFileSync(f, `${mother.privateKey}\n`);
  chmodSync(f, 0o600);
  const env = baseEnv(mother, { MOTHER_PRIVATE_KEY_FILE: f });
  delete env.MOTHER_PRIVATE_KEY;
  const { secrets } = loadConfig(env);
  assert.equal(secrets.motherPrivateKey, mother.privateKey);
  chmodSync(f, 0o644);
  const env2 = baseEnv(mother, { MOTHER_PRIVATE_KEY_FILE: f });
  delete env2.MOTHER_PRIVATE_KEY;
  assert.throws(() => loadConfig(env2), /chmod 600/);
});

test('encrypt policy requires a 32-byte key', () => {
  assert.throws(() => loadConfig(baseEnv(newAccount(), { TARGET_PRIVATE_KEY_POLICY: 'encrypt' })), /TARGET_KEY_ENCRYPTION_KEY/);
});
