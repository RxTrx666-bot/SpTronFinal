// Entry point.
//
// Startup sequence:
//   1. load + validate configuration (secrets from env / *_FILE)
//   2. derive the Mother Wallet address from the key (key never printed)
//   3. database migrations
//   4. verify RPC points at the expected TRON network and is in sync
//   5. verify the USDT TRC-20 contract (address, decimals, symbol)
//   6. start queue worker, notification dispatcher, Telegram bot, HTTP API

import { existsSync } from 'node:fs';
import { loadConfig, ConfigError } from './config/config.js';
import { createLogger } from './logger.js';
import { createApp } from './app.js';
import { sunToTrx, formatUnits } from './blockchain/units.js';
import { formatSystemAlert } from './telegram/alerts.js';
import { scrubError } from './security/redact.js';
import { sleep } from './util/mutex.js';

async function withRetries(fn, { attempts = 5, delayMs = 3000, logger, what }) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (err) {
      // Wrong network / wrong contract are permanent: never retry them.
      if (i >= attempts || ['WRONG_NETWORK', 'USDT_CONTRACT_INVALID'].includes(err.code)) throw err;
      logger.warn({ attempt: i, error: scrubError(err).message }, `${what} failed; retrying`);
      await sleep(delayMs * i);
    }
  }
}

async function main() {
  if (existsSync('.env') && typeof process.loadEnvFile === 'function') process.loadEnvFile('.env');

  let loaded;
  try {
    loaded = loadConfig(process.env);
  } catch (err) {
    const msg = err instanceof ConfigError ? err.message : scrubError(err).message;
    process.stderr.write(`[FATAL] ${msg}\n`);
    process.exit(78); // EX_CONFIG: systemd will not hot-loop thanks to RestartPreventExitStatus
  }
  const { config, secrets } = loaded;
  const logger = createLogger(config.log);

  const app = await createApp({ config, secrets, logger });
  // The signer now holds the only reference to the Mother key.
  secrets.motherPrivateKey = undefined;
  loaded = undefined;
  // Only the address is ever displayed.
  process.stdout.write(`\nMother wallet:\n${app.signer.address}\n\n`);
  logger.info({ network: config.network.name, dryRun: config.dryRun, mother: app.signer.address }, 'Starting TRON wallet funding bot');
  if (config.network.name !== 'mainnet') logger.warn({ network: config.network.name }, 'NOT running on TRON mainnet (test network profile)');
  if (config.dryRun) logger.warn('[DRY RUN] DRY_RUN=true: transactions are validated and signed but NEVER broadcast');

  const health = await withRetries(() => app.tron.checkConnection(), { logger, what: 'RPC connection check' });
  logger.info({ headBlock: health.headBlock, lagSeconds: health.lagSeconds }, 'Connected to TRON node (genesis verified)');

  const usdtInfo = await withRetries(
    () => app.usdt.verify({ network: config.network, expectedSymbol: config.usdt.expectedSymbol, allowNonstandard: config.usdt.allowNonstandardContract, logger }),
    { logger, what: 'USDT contract verification' },
  );
  logger.info({ contract: config.usdt.contract, name: usdtInfo.name, symbol: usdtInfo.symbol, decimals: usdtInfo.decimals }, 'USDT TRC-20 contract verified');

  try {
    const b = await app.admin.balances();
    const amounts = app.settings.fundingAmounts();
    logger.info(
      { trx: b.trx, usdt: b.usdt, fundTrxPerWallet: sunToTrx(amounts.trxAmountSun), fundUsdtPerWallet: formatUnits(amounts.usdtAmountUnits, 6), paused: app.settings.paused },
      'Mother Wallet balances',
    );
  } catch (err) {
    logger.warn({ error: scrubError(err).message }, 'Unable to read Mother Wallet balances at startup');
  }

  await app.queue.start();
  app.dispatcher.start();
  if (app.bot) app.bot.start();
  if (app.server) {
    await app.server.listen({ host: config.api.host, port: config.api.port });
    logger.info({ host: config.api.host, port: config.api.port, tls: config.api.tls, adminApi: Boolean(secrets.adminApiKey) }, 'HTTP API listening');
  }
  await app.repo.enqueueNotification({
    kind: 'startup',
    text: formatSystemAlert('FUNDING BOT STARTED', `Network: ${config.network.label}\nMother: ${app.signer.address}\nDry run: ${config.dryRun}\nPaused: ${app.settings.paused}`),
  });

  let shuttingDown = false;
  const shutdown = async (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    logger.info({ signal }, 'Shutting down (finishing in-flight work)');
    try {
      if (app.server) await app.server.close();
      app.bot?.stop();
      await app.queue.stop({ timeoutMs: 30000 });
      await app.dispatcher.tick().catch(() => {});
      app.dispatcher.stop();
      await app.knex.destroy();
    } catch (err) {
      logger.error({ error: scrubError(err).message }, 'Error during shutdown');
    }
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('unhandledRejection', (err) => logger.error({ error: scrubError(err).message }, 'Unhandled rejection'));
}

main().catch((err) => {
  process.stderr.write(`[FATAL] ${scrubError(err).message}\n`);
  process.exit(1);
});
