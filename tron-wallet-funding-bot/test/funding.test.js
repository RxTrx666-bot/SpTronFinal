import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeApp, newAccount, runUntilSettled } from './helpers/setup.js';

async function submitAndRun(ctx, extra = {}) {
  const target = extra.target ?? newAccount();
  const r = await ctx.app.intake.submit({ address: target.address, ...extra.submit });
  const job = await runUntilSettled(ctx.app, r.job.id);
  return { target, job, submission: r };
}

test('funds TRX + USDT, records txids and sends a success alert', async () => {
  const ctx = await makeApp();
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'COMPLETED');
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  assert.equal(ctx.chain.usdtOf(target.address), 10_000_000n);
  assert.match(job.trx_txid, /^[0-9a-f]{64}$/);
  assert.match(job.usdt_txid, /^[0-9a-f]{64}$/);
  const transfers = await ctx.app.repo.transfersForJob(job.id);
  assert.deepEqual(transfers.map((t) => [t.asset, t.status]), [['TRX', 'CONFIRMED'], ['USDT', 'CONFIRMED']]);
  const events = (await ctx.app.repo.jobEvents(job.id)).map((e) => e.to_status);
  for (const s of ['RECEIVED', 'VALIDATING', 'QUEUED', 'FUNDING_TRX', 'FUNDING_USDT', 'CONFIRMING', 'COMPLETED']) assert.ok(events.includes(s), s);
  await ctx.app.dispatcher.tick();
  assert.equal(ctx.telegram.sent.length, 1);
  assert.match(ctx.telegram.sent[0], /WALLET FUNDED/);
  assert.ok(ctx.telegram.sent[0].includes(job.trx_txid));
  await ctx.app.knex.destroy();
});

test('TRX only (USDT amount = 0) and USDT only (TRX amount = 0)', async () => {
  const a = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  const r1 = await submitAndRun(a);
  assert.equal(r1.job.status, 'COMPLETED');
  assert.equal(a.chain.trxOf(r1.target.address), 5_000_000n);
  assert.equal(a.chain.usdtOf(r1.target.address), 0n);
  assert.equal(r1.job.usdt_txid, null);
  await a.app.knex.destroy();

  const b = await makeApp({ env: { TRX_FUNDING_AMOUNT: '0' } });
  const r2 = await submitAndRun(b);
  assert.equal(r2.job.status, 'COMPLETED');
  assert.equal(b.chain.trxOf(r2.target.address), 0n);
  assert.equal(b.chain.usdtOf(r2.target.address), 10_000_000n);
  await b.app.knex.destroy();
});

test('duplicate submissions never fund twice (sequential and concurrent)', async () => {
  const ctx = await makeApp();
  const target = newAccount();
  const [a, b, c] = await Promise.all([1, 2, 3].map(() => ctx.app.intake.submit({ address: target.address })));
  const ids = new Set([a.job.id, b.job.id, c.job.id]);
  assert.equal(ids.size, 1, 'one job for concurrent duplicates');
  assert.equal([a, b, c].filter((r) => r.status === 'created').length, 1);
  await runUntilSettled(ctx.app, a.job.id);
  const again = await ctx.app.intake.submit({ address: target.address });
  assert.equal(again.status, 'duplicate');
  // repeat requested but not allowed by config
  const rep = await ctx.app.intake.submit({ address: target.address, repeat: true });
  assert.equal(rep.status, 'duplicate');
  await runUntilSettled(ctx.app, again.job.id);
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  assert.equal(ctx.chain.usdtOf(target.address), 10_000_000n);
  const wallet = await ctx.app.knex('wallets').where({ address: target.address }).first();
  assert.equal(wallet.submission_count, 5);
  await ctx.app.knex.destroy();
});

test('repeat funding only when ALLOW_REPEAT_FUNDING=true and explicitly requested', async () => {
  const ctx = await makeApp({ env: { ALLOW_REPEAT_FUNDING: 'true' } });
  const target = newAccount();
  const first = await submitAndRun(ctx, { target });
  assert.equal(first.job.status, 'COMPLETED');
  const dup = await ctx.app.intake.submit({ address: target.address });
  assert.equal(dup.status, 'duplicate');
  const second = await ctx.app.intake.submit({ address: target.address, repeat: true });
  assert.equal(second.status, 'created');
  assert.equal(second.job.round, 2);
  await runUntilSettled(ctx.app, second.job.id);
  assert.equal(ctx.chain.trxOf(target.address), 10_000_000n);
  await ctx.app.knex.destroy();
});

test('insufficient USDT: job FAILED, nothing sent, alert sent, funding auto-paused', async () => {
  const ctx = await makeApp();
  ctx.chain.setUsdt(ctx.mother.address, 5_000_000n); // only 5 USDT
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'FAILED');
  assert.equal(job.error_code, 'INSUFFICIENT_USDT');
  assert.equal(ctx.chain.broadcasts.length, 0, 'no transaction broadcast (TRX not sent either)');
  assert.equal(ctx.chain.trxOf(target.address), 0n);
  assert.equal(ctx.app.settings.paused, true);
  await ctx.app.dispatcher.tick();
  assert.ok(ctx.telegram.sent.some((m) => m.includes('FUNDING FAILED') && m.includes('Insufficient Mother Wallet USDT balance')));
  assert.ok(ctx.telegram.sent.some((m) => m.includes('AUTO-PAUSED')));

  // Top up, resume, retry -> completes, exactly once
  ctx.chain.setUsdt(ctx.mother.address, 100_000_000n);
  await ctx.app.admin.resume('test');
  await ctx.app.admin.retry(target.address, 'test');
  const done = await runUntilSettled(ctx.app, job.id);
  assert.equal(done.status, 'COMPLETED');
  assert.equal(ctx.chain.usdtOf(target.address), 10_000_000n);
  await ctx.app.knex.destroy();
});

test('insufficient TRX for fees blocks USDT funding', async () => {
  const ctx = await makeApp({ env: { TRX_FUNDING_AMOUNT: '0' } });
  ctx.chain.setTrx(ctx.mother.address, 10_000_000n); // 10 TRX < 30 fee limit + buffer
  const { job } = await submitAndRun(ctx);
  assert.equal(job.status, 'FAILED');
  assert.equal(job.error_code, 'INSUFFICIENT_TRX');
  assert.equal(ctx.chain.broadcasts.length, 0);
  await ctx.app.knex.destroy();
});

test('dry run validates and signs but never broadcasts', async () => {
  const ctx = await makeApp({ env: { DRY_RUN: 'true' } });
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'COMPLETED');
  assert.equal(job.mode, 'dry_run');
  assert.equal(ctx.chain.broadcasts.length, 0);
  assert.equal(ctx.chain.trxOf(target.address), 0n);
  const logs = ctx.logLines.join('\n');
  assert.match(logs, new RegExp(`\\[DRY RUN\\] Would send 5 TRX to ${target.address}`));
  assert.match(logs, new RegExp(`\\[DRY RUN\\] Would send 10 USDT to ${target.address}`));
  const transfers = await ctx.app.repo.transfersForJob(job.id);
  assert.ok(transfers.every((t) => t.status === 'DRY_RUN' && t.txid === null && t.signed_tx === null));
  await ctx.app.dispatcher.tick();
  assert.match(ctx.telegram.sent[0], /DRY RUN/);
  await ctx.app.knex.destroy();
});

test('dry run still reports insufficient balance', async () => {
  const ctx = await makeApp({ env: { DRY_RUN: 'true' } });
  ctx.chain.setUsdt(ctx.mother.address, 0n);
  const { job } = await submitAndRun(ctx);
  assert.equal(job.status, 'FAILED');
  assert.equal(job.error_code, 'INSUFFICIENT_USDT');
  await ctx.app.knex.destroy();
});

test('broadcast timeout where the node DID accept: no second transaction is created', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  ctx.chain.fail.broadcast = (txid, n) => (n === 1 ? 'timeout-accepted' : undefined);
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'COMPLETED');
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n, 'funded exactly once');
  assert.equal(new Set(ctx.chain.broadcasts).size, 1, 'only one distinct txid ever broadcast');
  await ctx.app.knex.destroy();
});

test('broadcast timeout where the tx was lost: same signed tx is re-broadcast, not a new one', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  ctx.chain.fail.broadcast = (txid, n) => (n === 1 ? 'timeout-lost' : undefined);
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'COMPLETED');
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  assert.ok(ctx.chain.broadcasts.length >= 2);
  assert.equal(new Set(ctx.chain.broadcasts).size, 1, 'rebroadcast used the identical txid');
  await ctx.app.knex.destroy();
});

test('a transaction that provably expired is replaced by exactly one new transaction', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  const target = newAccount();
  // Node "accepts" but the tx never makes it into a block; network moves on.
  ctx.chain.fail.broadcast = (txid, n) => (n <= 2 ? 'timeout-lost' : undefined);
  const r = await ctx.app.intake.submit({ address: target.address });
  await ctx.app.funding.processJob(r.job.id); // broadcast #1 lost, confirmation window elapses
  let transfers = await ctx.app.repo.transfersForJob(r.job.id);
  assert.equal(transfers.length, 1);
  const firstTxid = transfers[0].txid;
  // Chain time passes well beyond expiration (60s) + margin (10s) + solid lag
  ctx.chain.stall(40);
  await ctx.app.repo.updateJob(r.job.id, { next_attempt_at: 0 });
  const job = await runUntilSettled(ctx.app, r.job.id);
  assert.equal(job.status, 'COMPLETED');
  transfers = await ctx.app.repo.transfersForJob(r.job.id);
  assert.equal(transfers.find((t) => t.txid === firstTxid).status, 'EXPIRED');
  assert.equal(transfers.filter((t) => t.status === 'CONFIRMED').length, 1);
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n, 'funded exactly once');
  await ctx.app.knex.destroy();
});

test('crash after persisting but before broadcast is recovered without double funding', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  const target = newAccount();
  // Simulate a process crash right at broadcast time.
  const realBroadcast = ctx.app.tron.broadcast.bind(ctx.app.tron);
  ctx.app.tron.broadcast = async () => {
    throw Object.assign(new Error('process crashed'), { code: 'CRASH' });
  };
  const r = await ctx.app.intake.submit({ address: target.address });
  await ctx.app.funding.processJob(r.job.id);
  const [t] = await ctx.app.repo.transfersForJob(r.job.id);
  assert.equal(t.status, 'SIGNED');
  assert.ok(t.signed_tx && t.txid);
  // "Restart": broadcasting works again; the worker must reuse the stored tx.
  ctx.app.tron.broadcast = realBroadcast;
  const job = await runUntilSettled(ctx.app, r.job.id);
  assert.equal(job.status, 'COMPLETED');
  assert.deepEqual([...new Set(ctx.chain.broadcasts)], [t.txid]);
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  await ctx.app.knex.destroy();
});

test('node rejection (non-transient) fails the job; retry creates a new tx only after the old one expired', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  ctx.chain.fail.broadcast = 'reject:CONTRACT_VALIDATE_ERROR';
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'FAILED');
  assert.equal(job.error_code, 'BROADCAST_CONTRACT_VALIDATE_ERROR');
  ctx.chain.fail.broadcast = undefined;
  await ctx.app.admin.retry(job.id, 'test');
  // Immediately after retry, the rejected tx is not provably expired yet: it is
  // re-broadcast (same txid) rather than replaced.
  const done = await runUntilSettled(ctx.app, job.id);
  assert.equal(done.status, 'COMPLETED');
  assert.equal(new Set(ctx.chain.broadcasts).size, 1);
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  await ctx.app.knex.destroy();
});

test('transient node rejection (SERVER_BUSY) is retried automatically', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  ctx.chain.fail.broadcast = (txid, n) => (n === 1 ? 'reject:SERVER_BUSY' : undefined);
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'COMPLETED');
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  await ctx.app.knex.destroy();
});

test('USDT transfer reverting on chain marks the job FAILED; TRX part is not re-sent on retry', async () => {
  const ctx = await makeApp();
  ctx.chain.fail.usdtRevert = true;
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'FAILED');
  assert.equal(job.error_code, 'TX_FAILED_ONCHAIN');
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  ctx.chain.fail.usdtRevert = false;
  await ctx.app.admin.retry(target.address, 'test');
  const done = await runUntilSettled(ctx.app, job.id);
  assert.equal(done.status, 'COMPLETED');
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n, 'TRX not sent twice');
  assert.equal(ctx.chain.usdtOf(target.address), 10_000_000n);
  await ctx.app.knex.destroy();
});

test('RPC outage is transient: job retried with backoff then succeeds', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  ctx.chain.fail.getAccount = 'timeout';
  const target = newAccount();
  const r = await ctx.app.intake.submit({ address: target.address });
  let job = await ctx.app.funding.processJob(r.job.id);
  assert.equal(job.status, 'RETRYING');
  assert.equal(job.retry_count, 1);
  assert.ok(job.next_attempt_at > Date.now() - 1000);
  ctx.chain.fail.getAccount = undefined;
  job = await runUntilSettled(ctx.app, r.job.id);
  assert.equal(job.status, 'COMPLETED');
  await ctx.app.knex.destroy();
});

test('persistent transient errors give up after MAX_AUTO_RETRIES', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0', MAX_AUTO_RETRIES: '2' } });
  ctx.chain.fail.getAccount = 'ratelimit';
  const { job } = await submitAndRun(ctx);
  assert.equal(job.status, 'FAILED');
  assert.equal(job.retry_count, 2);
  assert.equal(ctx.chain.broadcasts.length, 0);
  await ctx.app.knex.destroy();
});

test('Telegram failure never repeats a blockchain transaction', async () => {
  const ctx = await makeApp();
  ctx.telegram.failing = true;
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'COMPLETED');
  for (let i = 0; i < 3; i++) await ctx.app.dispatcher.tick().catch(() => {});
  const notif = await ctx.app.knex('notifications').where({ job_id: job.id }).first();
  assert.equal(notif.status, 'PENDING');
  assert.ok(notif.attempts >= 1);
  // Re-processing the job is a no-op
  await ctx.app.funding.processJob(job.id);
  assert.equal(ctx.chain.broadcasts.length, 2);
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  // Telegram comes back: the pending notification is delivered once
  ctx.telegram.failing = false;
  await ctx.app.knex('notifications').update({ next_attempt_at: 0 });
  await ctx.app.dispatcher.tick();
  await ctx.app.dispatcher.tick();
  assert.equal(ctx.telegram.sent.filter((m) => m.includes('WALLET FUNDED')).length, 1);
  await ctx.app.knex.destroy();
});

test('paused funding creates no transactions; resume continues', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  await ctx.app.admin.pause('test');
  const target = newAccount();
  const r = await ctx.app.intake.submit({ address: target.address });
  const job = await ctx.app.funding.processJob(r.job.id);
  assert.equal(job.status, 'QUEUED');
  assert.equal(ctx.chain.broadcasts.length, 0);
  await ctx.app.admin.resume('test');
  const done = await runUntilSettled(ctx.app, r.job.id);
  assert.equal(done.status, 'COMPLETED');
  await ctx.app.knex.destroy();
});

test('concurrent jobs cannot overspend the Mother Wallet (race condition)', async () => {
  const ctx = await makeApp({ env: { TRX_FUNDING_AMOUNT: '0', USDT_FUNDING_AMOUNT: '10', QUEUE_CONCURRENCY: '5', AUTO_PAUSE_ON_INSUFFICIENT_BALANCE: 'false' } });
  ctx.chain.setUsdt(ctx.mother.address, 25_000_000n); // enough for 2 wallets, not 5
  const targets = [1, 2, 3, 4, 5].map(() => newAccount());
  const subs = await Promise.all(targets.map((t) => ctx.app.intake.submit({ address: t.address })));
  // Run all five at the same time (no mining between build and broadcast).
  const jobs = await Promise.all(subs.map((s) => runUntilSettled(ctx.app, s.job.id)));
  const completed = jobs.filter((j) => j.status === 'COMPLETED').length;
  const failed = jobs.filter((j) => j.status === 'FAILED' && j.error_code === 'INSUFFICIENT_USDT').length;
  assert.equal(completed, 2);
  assert.equal(failed, 3);
  const funded = targets.filter((t) => ctx.chain.usdtOf(t.address) === 10_000_000n).length;
  assert.equal(funded, 2);
  assert.equal(ctx.chain.executedTransfers().length, 2);
  await ctx.app.knex.destroy();
});

test('destination that is a smart contract is refused', async () => {
  const ctx = await makeApp();
  const target = newAccount();
  ctx.chain.addContract(target.address);
  const { job } = await submitAndRun(ctx, { target });
  assert.equal(job.status, 'FAILED');
  assert.equal(job.error_code, 'DESTINATION_IS_CONTRACT');
  assert.equal(ctx.chain.broadcasts.length, 0);
  await ctx.app.knex.destroy();
});

test('USDT fee limit too low for the energy estimate is refused before broadcast', async () => {
  const ctx = await makeApp({ env: { TRX_FUNDING_AMOUNT: '0' } });
  ctx.chain.fail.highEnergy = true;
  const { job } = await submitAndRun(ctx);
  assert.equal(job.status, 'FAILED');
  assert.equal(job.error_code, 'FEE_LIMIT_TOO_LOW');
  assert.equal(ctx.chain.broadcasts.length, 0);
  await ctx.app.knex.destroy();
});

test('private keys submitted with a wallet are validated, never stored, never logged', async () => {
  const ctx = await makeApp();
  const target = newAccount();
  const r = await ctx.app.intake.submit({ address: target.address, privateKey: target.privateKey });
  assert.equal(r.status, 'created');
  await runUntilSettled(ctx.app, r.job.id);
  await ctx.app.dispatcher.tick();
  const dump = JSON.stringify(await Promise.all(['wallets', 'funding_jobs', 'transfers', 'job_events', 'notifications', 'settings', 'target_wallet_secrets'].map((t) => ctx.app.knex(t).select())));
  assert.ok(!dump.includes(target.privateKey));
  assert.ok(!dump.includes(ctx.mother.privateKey));
  const logs = ctx.logLines.join('\n');
  assert.ok(!logs.includes(target.privateKey));
  assert.ok(!logs.includes(ctx.mother.privateKey));
  assert.ok(!ctx.telegram.sent.join('\n').includes(target.privateKey));
  // mismatching key is rejected
  const other = newAccount();
  await assert.rejects(ctx.app.intake.submit({ address: newAccount().address, privateKey: other.privateKey }), { code: 'PRIVATE_KEY_ADDRESS_MISMATCH' });
  await ctx.app.knex.destroy();
});

test('TARGET_PRIVATE_KEY_POLICY=encrypt stores AES-GCM ciphertext only', async () => {
  const key = Buffer.alloc(32, 7).toString('base64');
  const ctx = await makeApp({ env: { TARGET_PRIVATE_KEY_POLICY: 'encrypt', TARGET_KEY_ENCRYPTION_KEY: key } });
  const target = newAccount();
  await ctx.app.intake.submit({ address: target.address, privateKey: target.privateKey });
  const row = await ctx.app.knex('target_wallet_secrets').where({ address: target.address }).first();
  assert.ok(row);
  assert.ok(!JSON.stringify(row).includes(target.privateKey));
  const { FieldCipher } = await import('../src/security/crypto.js');
  assert.equal(new FieldCipher(key).decrypt(row, target.address), target.privateKey.toLowerCase());
  await ctx.app.knex.destroy();
});

test('TARGET_PRIVATE_KEY_POLICY=reject refuses keys', async () => {
  const ctx = await makeApp({ env: { TARGET_PRIVATE_KEY_POLICY: 'reject' } });
  const target = newAccount();
  await assert.rejects(ctx.app.intake.submit({ address: target.address, privateKey: target.privateKey }), { code: 'PRIVATE_KEY_NOT_ACCEPTED' });
  await ctx.app.knex.destroy();
});

test('funding amounts changed at runtime apply to new jobs and respect caps', async () => {
  const ctx = await makeApp({ env: { MAX_TRX_PER_WALLET: '20' } });
  await ctx.app.admin.setAmount('TRX', '7.5', 'test');
  await assert.rejects(ctx.app.admin.setAmount('TRX', '21', 'test'), /MAX_TRX_PER_WALLET/);
  await assert.rejects(ctx.app.admin.setAmount('USDT', '1.1234567', 'test'), /decimal places/);
  const { target, job } = await submitAndRun(ctx);
  assert.equal(job.status, 'COMPLETED');
  assert.equal(ctx.chain.trxOf(target.address), 7_500_000n);
  await ctx.app.knex.destroy();
});

test('daily limit stops funding', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0', DAILY_TRX_LIMIT: '8' } });
  const first = await submitAndRun(ctx);
  assert.equal(first.job.status, 'COMPLETED');
  const second = await submitAndRun(ctx);
  assert.equal(second.job.status, 'FAILED');
  assert.equal(second.job.error_code, 'DAILY_LIMIT_TRX');
  await ctx.app.knex.destroy();
});

test('a job left in RECEIVED by a crash is picked up and funded once', async () => {
  const ctx = await makeApp({ env: { USDT_FUNDING_AMOUNT: '0' } });
  const target = newAccount();
  await ctx.app.repo.upsertWallet({ address: target.address });
  const job = await ctx.app.repo.createJob({ address: target.address, mode: 'live', trxAmountSun: 5_000_000n, usdtAmountUnits: 0n });
  const due = await ctx.app.repo.dueJobs({ limit: 10 });
  assert.ok(due.some((j) => j.id === job.id));
  const done = await runUntilSettled(ctx.app, job.id);
  assert.equal(done.status, 'COMPLETED');
  assert.equal(ctx.chain.trxOf(target.address), 5_000_000n);
  await ctx.app.knex.destroy();
});
