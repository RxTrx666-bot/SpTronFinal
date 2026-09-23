// Test harness: builds the full application against the FakeChain and an
// in-memory SQLite database. Keys are generated randomly per run.

import { utils } from 'tronweb';
import knexFactory from 'knex';
import { loadConfig } from '../../src/config/config.js';
import { createApp } from '../../src/app.js';
import { createLogger } from '../../src/logger.js';
import { FakeChain } from './fake-chain.js';

export const USDT_MAINNET = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t';

export function newAccount() {
  const a = utils.accounts.generateAccount();
  return { address: a.address.base58, privateKey: a.privateKey };
}

export class FakeTelegram {
  constructor() {
    this.sent = [];
    this.failing = false;
  }
  async broadcast(text) {
    if (this.failing) throw new Error('Telegram is down (simulated)');
    this.sent.push(text);
  }
  async sendMessage(chatId, text) {
    return this.broadcast(text);
  }
}

export function baseEnv(mother, extra = {}) {
  return {
    MOTHER_PRIVATE_KEY: mother.privateKey,
    TRON_NETWORK: 'mainnet',
    TRON_RPC_URL: 'https://rpc.invalid',
    USDT_CONTRACT_ADDRESS: USDT_MAINNET,
    TRX_FUNDING_AMOUNT: '5',
    USDT_FUNDING_AMOUNT: '10',
    INTERNAL_API_KEY: 'i'.repeat(40),
    ADMIN_API_KEY: 'a'.repeat(40),
    DATABASE_URL: 'sqlite::memory:',
    CONFIRMATION_POLL_MS: '10',
    CONFIRMATION_TIMEOUT_SECONDS: '10',
    RETRY_BASE_DELAY_MS: '10',
    TX_EXPIRATION_SECONDS: '60',
    EXPIRY_SAFETY_MARGIN_SECONDS: '10',
    ...extra,
  };
}

/**
 * Create an app wired to a FakeChain. `sleep` mines one block per call so the
 * confirmation loop makes progress deterministically.
 */
export async function makeApp({ env = {}, chainOptions = {}, fund = true, logLines } = {}) {
  const mother = newAccount();
  const chain = new FakeChain({ usdtContract: USDT_MAINNET, ...chainOptions });
  if (fund) {
    chain.setTrx(mother.address, 1000n * 1_000_000n);
    chain.setUsdt(mother.address, 1000n * 1_000_000n);
  }
  const { config, secrets } = loadConfig(baseEnv(mother, env));
  const lines = logLines ?? [];
  const logger = createLogger({ level: 'debug', format: 'text', sink: (l) => lines.push(l) });
  const knex = knexFactory({ client: 'better-sqlite3', connection: { filename: ':memory:' }, useNullAsDefault: true, pool: { min: 1, max: 1 } });
  const telegram = new FakeTelegram();
  const app = await createApp({
    config,
    secrets,
    logger,
    overrides: {
      provider: chain,
      knex,
      telegram,
      disableBot: true,
      hasLease: () => true,
      // Service time follows simulated chain time (3s per block).
      clock: () => chain.head.timestamp,
      sleep: async () => chain.mine(1),
    },
  });
  await app.usdt.verify({ network: config.network });
  return { app, chain, mother, telegram, logLines: lines, config };
}

export async function runUntilSettled(app, jobId, maxPasses = 30) {
  let job;
  for (let i = 0; i < maxPasses; i++) {
    job = await app.funding.processJob(jobId);
    if (['COMPLETED', 'FAILED'].includes(job.status)) return job;
    // make the job due immediately for the next pass
    await app.repo.updateJob(jobId, { next_attempt_at: 0 });
  }
  return job;
}
