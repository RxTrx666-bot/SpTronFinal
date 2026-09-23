import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { scrub, scrubObject, registerSecret, registerTransientSecret, _resetSecretsForTests } from '../src/security/redact.js';
import { createLogger } from '../src/logger.js';
import { MotherSigner } from '../src/security/signer.js';
import { inspect } from 'node:util';
import { newAccount } from './helpers/setup.js';

beforeEach(() => _resetSecretsForTests());

test('registered secrets are redacted anywhere in a string', () => {
  const { privateKey } = newAccount();
  registerSecret(privateKey);
  const out = scrub(`boom: key=${privateKey} and again 0x${privateKey.toUpperCase()}`);
  assert.ok(!out.toLowerCase().includes(privateKey));
  assert.match(out, /\[REDACTED\]/);
});

test('transient target keys are redacted by hash without being retained', () => {
  const { privateKey } = newAccount();
  registerTransientSecret(privateKey);
  assert.ok(!scrub(`received ${privateKey}`).includes(privateKey));
});

test('transaction ids (also 64 hex) are NOT redacted', () => {
  const txid = 'a'.repeat(64);
  assert.equal(scrub(`tx ${txid}`), `tx ${txid}`);
});

test('secret-looking fields, bearer tokens, telegram tokens, URL passwords', () => {
  assert.equal(scrub('{"private_key":"abc123"}'), '{"private_key":[REDACTED]}');
  assert.equal(scrub('privateKey=deadbeef'), 'privateKey=[REDACTED]');
  assert.equal(scrub('Authorization: Bearer abc.def-ghi'), 'Authorization: Bearer [REDACTED]');
  assert.equal(scrub('https://api.telegram.org/bot123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-ZQ/sendMessage'), 'https://api.telegram.org/bot[REDACTED]/sendMessage');
  assert.equal(scrub('postgres://user:s3cr3t@db/x'), 'postgres://user:[REDACTED]@db/x');
});

test('scrubObject drops sensitive keys', () => {
  const o = scrubObject({ address: 'T1', private_key: 'xx', nested: { apiKey: 'y', ok: 1 }, signed_tx: '{}' });
  assert.deepEqual(o, { address: 'T1', private_key: '[REDACTED]', nested: { apiKey: '[REDACTED]', ok: 1 }, signed_tx: '[REDACTED]' });
});

test('logger never prints the Mother key, even via errors or objects', () => {
  const mother = newAccount();
  const signer = new MotherSigner(mother.privateKey);
  const lines = [];
  const log = createLogger({ level: 'debug', sink: (l) => lines.push(l) });
  log.info({ signer, err: new Error(`failed with ${mother.privateKey}`) }, `oops ${mother.privateKey}`);
  log.error({ privateKey: mother.privateKey }, 'x');
  const all = lines.join('\n');
  assert.ok(!all.includes(mother.privateKey), all);
  assert.ok(all.includes(mother.address));
  // util.inspect / JSON of the signer only show the address
  assert.ok(!inspect(signer).includes(mother.privateKey));
  assert.ok(!JSON.stringify(signer).includes(mother.privateKey));
  assert.equal(Object.keys(signer).includes('privateKey'), false);
});
