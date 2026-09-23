import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeApp, newAccount } from './helpers/setup.js';

const INTERNAL = `Bearer ${'i'.repeat(40)}`;
const ADMIN = `Bearer ${'a'.repeat(40)}`;

async function api(env = {}) {
  const ctx = await makeApp({ env });
  return ctx;
}

test('rejects unauthenticated and wrongly authenticated requests', async () => {
  const ctx = await api();
  const s = ctx.app.server;
  const body = { address: newAccount().address };
  assert.equal((await s.inject({ method: 'POST', url: '/wallets', payload: body })).statusCode, 401);
  assert.equal((await s.inject({ method: 'POST', url: '/wallets', payload: body, headers: { authorization: 'Bearer wrong' } })).statusCode, 401);
  assert.equal((await s.inject({ method: 'POST', url: '/wallets', payload: body, headers: { authorization: 'Basic abc' } })).statusCode, 401);
  // admin key cannot submit wallets, internal key cannot use admin endpoints (least privilege)
  assert.equal((await s.inject({ method: 'POST', url: '/wallets', payload: body, headers: { authorization: ADMIN } })).statusCode, 401);
  assert.equal((await s.inject({ method: 'POST', url: '/admin/pause', headers: { authorization: INTERNAL } })).statusCode, 401);
  // no DB side effects from rejected requests
  assert.equal((await ctx.app.knex('wallets').count({ n: '*' }).first()).n, 0);
  await ctx.app.knex.destroy();
});

test('accepts a wallet, reports duplicates idempotently', async () => {
  const ctx = await api();
  const s = ctx.app.server;
  const address = newAccount().address;
  const r1 = await s.inject({ method: 'POST', url: '/wallets', payload: { address, source: 'upstream-bot' }, headers: { authorization: INTERNAL } });
  assert.equal(r1.statusCode, 201);
  assert.equal(r1.json().status, 'created');
  assert.equal(r1.json().job.status, 'QUEUED');
  assert.equal(r1.json().job.trxAmount, '5');
  assert.equal(r1.json().job.usdtAmount, '10');
  const r2 = await s.inject({ method: 'POST', url: '/wallets', payload: { address }, headers: { authorization: INTERNAL } });
  assert.equal(r2.statusCode, 200);
  assert.equal(r2.json().status, 'duplicate');
  assert.equal(r2.json().job.id, r1.json().job.id);
  const st = await s.inject({ method: 'GET', url: `/wallets/${address}`, headers: { authorization: INTERNAL } });
  assert.equal(st.statusCode, 200);
  assert.equal(st.json().address, address);
  await ctx.app.knex.destroy();
});

test('invalid addresses and private-key-as-address are rejected without echo', async () => {
  const ctx = await api();
  const s = ctx.app.server;
  const pk = newAccount().privateKey;
  const r = await s.inject({ method: 'POST', url: '/wallets', payload: { address: pk }, headers: { authorization: INTERNAL } });
  assert.equal(r.statusCode, 422);
  assert.equal(r.json().error, 'PRIVATE_KEY_AS_ADDRESS');
  assert.ok(!r.body.includes(pk));
  const r2 = await s.inject({ method: 'POST', url: '/wallets', payload: { address: '0x742d35Cc6634C0532925a3b844Bc454e4438f44e' }, headers: { authorization: INTERNAL } });
  assert.equal(r2.statusCode, 422);
  assert.equal(r2.json().error, 'INVALID_ADDRESS');
  assert.ok(!ctx.logLines.join('\n').includes(pk));
  await ctx.app.knex.destroy();
});

test('malformed requests: bad JSON is not echoed, unknown fields rejected', async () => {
  const ctx = await api();
  const s = ctx.app.server;
  const pk = newAccount().privateKey;
  const bad = await s.inject({ method: 'POST', url: '/wallets', payload: `{"private_key":"${pk}", oops`, headers: { authorization: INTERNAL, 'content-type': 'application/json' } });
  assert.equal(bad.statusCode, 400);
  assert.ok(!bad.body.includes(pk));
  assert.ok(!bad.body.includes('oops'));
  const extra = await s.inject({ method: 'POST', url: '/wallets', payload: { address: newAccount().address, evil: 1 }, headers: { authorization: INTERNAL } });
  assert.equal(extra.statusCode, 400);
  const missing = await s.inject({ method: 'POST', url: '/wallets', payload: {}, headers: { authorization: INTERNAL } });
  assert.equal(missing.statusCode, 400);
  await ctx.app.knex.destroy();
});

test('credentials in the URL are refused', async () => {
  const ctx = await api();
  const r = await ctx.app.server.inject({ method: 'GET', url: `/admin/balance?api_key=${'a'.repeat(40)}` });
  assert.equal(r.statusCode, 400);
  assert.equal(r.json().error, 'CREDENTIALS_IN_URL');
  await ctx.app.knex.destroy();
});

test('private_key over plain HTTP is refused (HTTPS required)', async () => {
  const ctx = await api();
  const t = newAccount();
  const r = await ctx.app.server.inject({ method: 'POST', url: '/wallets', payload: { address: t.address, private_key: t.privateKey }, headers: { authorization: INTERNAL } });
  assert.equal(r.statusCode, 403);
  assert.equal(r.json().error, 'HTTPS_REQUIRED');
  assert.ok(!r.body.includes(t.privateKey));
  await ctx.app.knex.destroy();
});

test('private_key accepted over HTTPS (via trusted proxy) and never stored', async () => {
  const ctx = await api({ TRUST_PROXY: 'true' });
  const t = newAccount();
  const r = await ctx.app.server.inject({
    method: 'POST',
    url: '/wallets',
    payload: { address: t.address, private_key: t.privateKey },
    headers: { authorization: INTERNAL, 'x-forwarded-proto': 'https' },
  });
  assert.equal(r.statusCode, 201);
  assert.ok(!r.body.includes(t.privateKey));
  assert.equal(await ctx.app.knex('target_wallet_secrets').count({ n: '*' }).first().then((x) => Number(x.n)), 0);
  await ctx.app.knex.destroy();
});

test('batch submission', async () => {
  const ctx = await api();
  const a = newAccount().address;
  const r = await ctx.app.server.inject({
    method: 'POST',
    url: '/wallets/batch',
    payload: { wallets: [{ address: a }, { address: a }, { address: 'nope' }] },
    headers: { authorization: INTERNAL },
  });
  assert.equal(r.statusCode, 200);
  assert.deepEqual(r.json().results.map((x) => x.status), ['created', 'duplicate', 'rejected']);
  await ctx.app.knex.destroy();
});

test('rate limiting', async () => {
  const ctx = await api({ API_RATE_LIMIT_MAX: '3' });
  const codes = [];
  for (let i = 0; i < 5; i++) codes.push((await ctx.app.server.inject({ method: 'GET', url: '/health', remoteAddress: '10.0.0.9' })).statusCode);
  assert.deepEqual(codes, [200, 200, 200, 429, 429]);
  await ctx.app.knex.destroy();
});

test('admin endpoints: balance, jobs, pause/resume, settings, retry', async () => {
  const ctx = await api();
  const s = ctx.app.server;
  const h = { authorization: ADMIN };
  const bal = await s.inject({ method: 'GET', url: '/admin/balance', headers: h });
  assert.deepEqual(bal.json(), { address: ctx.mother.address, trx: '1000', usdt: '1000' });
  assert.ok(!bal.body.includes(ctx.mother.privateKey));

  assert.equal((await s.inject({ method: 'POST', url: '/admin/pause', headers: h })).json().paused, true);
  assert.equal((await s.inject({ method: 'POST', url: '/admin/resume', headers: h })).json().paused, false);

  const set = await s.inject({ method: 'PUT', url: '/admin/settings/funding', payload: { trx_amount: '3', usdt_amount: '4.5' }, headers: h });
  assert.equal(set.statusCode, 200);
  assert.equal(set.json().trxAmount, '3');
  assert.equal(set.json().usdtAmount, '4.5');
  const tooMuch = await s.inject({ method: 'PUT', url: '/admin/settings/funding', payload: { trx_amount: '100000' }, headers: h });
  assert.equal(tooMuch.statusCode, 422);

  const address = newAccount().address;
  await s.inject({ method: 'POST', url: '/wallets', payload: { address }, headers: { authorization: INTERNAL } });
  const pending = await s.inject({ method: 'GET', url: '/admin/jobs?status=pending', headers: h });
  assert.equal(pending.json().jobs.length, 1);
  const job = await s.inject({ method: 'GET', url: `/admin/jobs/${address}`, headers: h });
  assert.equal(job.json().trxAmount, '3');
  const retry = await s.inject({ method: 'POST', url: `/admin/jobs/${address}/retry`, headers: h });
  assert.equal(retry.statusCode, 409); // not failed

  const health = await s.inject({ method: 'GET', url: '/admin/health', headers: h });
  assert.equal(health.json().checks.database, 'ok');
  const pub = await s.inject({ method: 'GET', url: '/health' });
  assert.deepEqual(Object.keys(pub.json()), ['status']);
  await ctx.app.knex.destroy();
});
