import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeApp, newAccount, runUntilSettled } from './helpers/setup.js';
import { TelegramBot } from '../src/telegram/bot.js';
import { TelegramClient } from '../src/telegram/client.js';
import { RpcClient } from '../src/blockchain/http.js';
import { TronHttpProvider } from '../src/blockchain/provider.js';
import { UsdtContract } from '../src/blockchain/usdt.js';
import { TronService } from '../src/blockchain/tron.js';
import { NETWORKS } from '../src/config/networks.js';
import { registerSecret } from '../src/security/redact.js';
import { FakeChain } from './helpers/fake-chain.js';

function bot(ctx, extraConfig = {}) {
  const config = { ...ctx.config, telegram: { ...ctx.config.telegram, chatIds: ['111'], adminUserIds: [], ...extraConfig } };
  return new TelegramBot({ client: ctx.telegram, admin: ctx.app.admin, config, logger: ctx.app.logger });
}

test('telegram commands: balance, pending, job, pause/resume, set amounts, retry', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  const b = bot(ctx);
  assert.match(await b.execute('/balance', 'tg:1'), /TRX:<\/b> 1000/);
  const t = newAccount();
  const r = await ctx.app.intake.submit({ address: t.address });
  assert.match(await b.execute('/pending', 'tg:1'), new RegExp(t.address));
  await runUntilSettled(ctx.app, r.job.id);
  const tx = await b.execute(`/tx ${t.address}`, 'tg:1');
  assert.match(tx, /CONFIRMED/);
  assert.match(tx, /tronscan\.org\/#\/transaction\/[0-9a-f]{64}/);
  assert.match(await b.execute('/completed', 'tg:1'), new RegExp(t.address));
  assert.match(await b.execute('/pause', 'tg:1'), /paused/);
  assert.equal(ctx.app.settings.paused, true);
  assert.match(await b.execute('/resume', 'tg:1'), /resumed/);
  assert.match(await b.execute('/set_trx 2.5', 'tg:1'), /TRX:<\/b> 2\.5/);
  assert.match(await b.execute('/set_usdt abc', 'tg:1'), /❌/);
  assert.match(await b.execute(`/retry ${t.address}`, 'tg:1'), /only FAILED or RETRYING/);
  assert.match(await b.execute('/status', 'tg:1'), /Health/);
  assert.match(await b.execute('/config', 'tg:1'), /TRX per wallet/);
  await ctx.app.knex.destroy();
});

test('telegram: messages from unauthorized chats/users are ignored', async () => {
  const ctx = await makeApp();
  const b = bot(ctx, { adminUserIds: ['42'] });
  assert.equal(b.isAuthorized({ chat: { id: 111 }, from: { id: 42 } }), true);
  assert.equal(b.isAuthorized({ chat: { id: 111 }, from: { id: 7 } }), false);
  assert.equal(b.isAuthorized({ chat: { id: 999 }, from: { id: 42 } }), false);
  await b.handle({ chat: { id: 999 }, from: { id: 42 }, text: '/pause' });
  assert.equal(ctx.app.settings.paused, false);
  assert.equal(ctx.telegram.sent.length, 0);
  await ctx.app.knex.destroy();
});

test('telegram client never exposes the bot token in errors', async () => {
  const token = '123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-ZQ';
  registerSecret(token);
  const fetchImpl = async (url) => {
    throw new Error(`connect failed for ${url}`);
  };
  const c = new TelegramClient({ token, chatIds: ['1'], fetchImpl });
  await assert.rejects(c.sendMessage('1', 'hi'), (err) => !err.message.includes(token) && !err.message.includes('AAEh'));
  const c2 = new TelegramClient({
    token,
    chatIds: ['1'],
    fetchImpl: async () => new Response(JSON.stringify({ ok: false, description: `Unauthorized bot${token}` }), { status: 401 }),
  });
  await assert.rejects(c2.sendMessage('1', 'hi'), (err) => !err.message.includes(token));
  assert.ok(!JSON.stringify(c2).includes(token));
});

test('RPC client: retries reads on 429/5xx, honours timeouts, never retries broadcast', async () => {
  let calls = 0;
  const seq = [429, 503, 200];
  const fetchImpl = async (url, opts) => {
    calls++;
    assert.equal(opts.headers['TRON-PRO-API-KEY'], 'k'.repeat(36));
    assert.ok(!url.includes('k'.repeat(36)), 'api key never in URL');
    const status = seq.shift() ?? 200;
    return new Response(JSON.stringify({ blockID: 'x' }), { status, headers: status === 429 ? { 'retry-after': '0' } : {} });
  };
  const rpc = new RpcClient({ url: 'https://node.test', apiKey: 'k'.repeat(36), maxRetries: 3, fetchImpl });
  const r = await rpc.post('/wallet/getblockbynum', { num: 0 });
  assert.equal(r.blockID, 'x');
  assert.equal(calls, 3);

  let bcalls = 0;
  const rpc2 = new RpcClient({ url: 'https://node.test', maxRetries: 5, fetchImpl: async () => (bcalls++, new Response('', { status: 503 })) });
  await assert.rejects(new TronHttpProvider(rpc2).broadcast({ txID: '00' }), { code: 'RPC_HTTP' });
  assert.equal(bcalls, 1, 'broadcast is attempted exactly once by the transport');

  const slow = new RpcClient({
    url: 'https://node.test',
    timeoutMs: 50,
    maxRetries: 0,
    fetchImpl: (u, { signal }) => new Promise((_, rej) => signal.addEventListener('abort', () => rej(signal.reason))),
  });
  await assert.rejects(slow.post('/wallet/getnowblock'), { code: 'RPC_TIMEOUT' });
});

test('startup safety: wrong network and fake USDT contracts are refused', async () => {
  const chain = new FakeChain({ usdtContract: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', genesis: 'ab'.repeat(32) });
  const tron = new TronService({ provider: chain, network: NETWORKS.mainnet, expectedGenesisBlockId: NETWORKS.mainnet.genesisBlockId, txConfig: { expirationSeconds: 60, expirySafetyMarginSeconds: 10 } });
  await assert.rejects(tron.checkConnection(), { code: 'WRONG_NETWORK' });

  const caller = newAccount().address;
  // Right address but wrong decimals
  const c1 = new FakeChain({ usdtContract: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', usdtDecimals: 18 });
  await assert.rejects(new UsdtContract({ provider: c1, contract: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', callerAddress: caller }).verify({ network: NETWORKS.mainnet }), /decimals/);
  // A look-alike contract (same symbol, 6 decimals) at a different address
  const fake = newAccount().address;
  const c2 = new FakeChain({ usdtContract: fake });
  await assert.rejects(new UsdtContract({ provider: c2, contract: fake, callerAddress: caller }).verify({ network: NETWORKS.mainnet }), /differs from the official/);
  // ...unless explicitly allowed (e.g. testnet token)
  const ok = await new UsdtContract({ provider: c2, contract: fake, callerAddress: caller }).verify({ network: NETWORKS.mainnet, allowNonstandard: true });
  assert.equal(ok.decimals, 6);
  // Wrong symbol
  const c3 = new FakeChain({ usdtContract: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', usdtSymbol: 'USDC' });
  await assert.rejects(new UsdtContract({ provider: c3, contract: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', callerAddress: caller }).verify({ network: NETWORKS.mainnet }), /symbol/);
});

test('telegram: backlog from before a restart is skipped (no command replay)', async () => {
  const ctx = await makeApp();
  const calls = [];
  const client = {
    setMyCommands: async () => {},
    getUpdates: async (offset) => {
      calls.push(offset);
      if (offset === -1) return [{ update_id: 500, message: { chat: { id: 111 }, from: { id: 1 }, text: '/pause' } }];
      b.stop();
      return [];
    },
    sendMessage: async () => {},
  };
  const config = { ...ctx.config, telegram: { ...ctx.config.telegram, chatIds: ['111'], adminUserIds: [] } };
  const b = new TelegramBot({ client, admin: ctx.app.admin, config, logger: ctx.app.logger });
  await b.start();
  await new Promise((r) => setTimeout(r, 20));
  assert.deepEqual(calls.slice(0, 2), [-1, 501]);
  assert.equal(ctx.app.settings.paused, false, 'old /pause was not replayed');
  await ctx.app.knex.destroy();
});
