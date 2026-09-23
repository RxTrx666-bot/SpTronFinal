// Composition root: wires all services together. Used by index.js and tests.

import { createKnex, migrate } from './database/database.js';
import { Repository } from './database/models.js';
import { MotherSigner } from './security/signer.js';
import { FieldCipher } from './security/crypto.js';
import { RpcClient } from './blockchain/http.js';
import { TronHttpProvider } from './blockchain/provider.js';
import { TronService } from './blockchain/tron.js';
import { UsdtContract } from './blockchain/usdt.js';
import { RuntimeSettings } from './admin/settings.js';
import { AdminService } from './admin/admin-service.js';
import { FundingService } from './wallet/funding.js';
import { IntakeService } from './wallet/intake.js';
import { FundingQueue } from './queue/queue.js';
import { TelegramClient } from './telegram/client.js';
import { TelegramBot } from './telegram/bot.js';
import { NotificationDispatcher } from './telegram/alerts.js';
import { buildServer } from './api/server.js';

/**
 * Build the application. `overrides` lets tests inject a fake provider,
 * Telegram client, clock or knex instance.
 */
export async function createApp({ config, secrets, logger, overrides = {} }) {
  const signer = new MotherSigner(secrets.motherPrivateKey);
  // Drop our reference to the raw key: from here on only the signer holds it.
  secrets = { ...secrets, motherPrivateKey: undefined };

  const knex = overrides.knex ?? createKnex(config.databaseUrl);
  await migrate(knex, config.databaseUrl);
  const repo = new Repository(knex, { clock: overrides.clock });

  const provider =
    overrides.provider ??
    new TronHttpProvider(
      new RpcClient({
        url: config.rpc.url,
        solidityUrl: config.rpc.solidityUrl,
        apiKey: secrets.tronApiKey,
        apiKeyHeader: config.rpc.apiKeyHeader,
        timeoutMs: config.rpc.timeoutMs,
        maxRetries: config.rpc.maxRetries,
        minIntervalMs: config.rpc.minIntervalMs,
        logger,
      }),
    );
  const tron = new TronService({ provider, network: config.network, expectedGenesisBlockId: config.expectedGenesisBlockId, txConfig: config.tx, logger });
  const usdt = new UsdtContract({ provider, contract: config.usdt.contract, callerAddress: signer.address, decimals: config.usdt.decimals });

  const settings = new RuntimeSettings({ repo, config, logger });
  await settings.load();

  let queue;
  const funding = new FundingService({
    repo,
    tron,
    usdt,
    signer,
    settings,
    config,
    logger,
    clock: overrides.clock,
    sleep: overrides.sleep,
    hasLease: () => (overrides.hasLease ? overrides.hasLease() : queue?.hasLease() ?? false),
  });
  queue = new FundingQueue({ repo, funding, config, logger });

  const cipher = config.targetKeyPolicy === 'encrypt' ? new FieldCipher(secrets.targetKeyEncryptionKey) : null;
  const intake = new IntakeService({ repo, settings, config, signer, cipher, logger, onNewJob: () => queue.notify() });
  const admin = new AdminService({ repo, tron, usdt, signer, settings, funding, queue, config });

  const telegram =
    overrides.telegram ??
    (config.telegram.enabled
      ? new TelegramClient({ token: secrets.telegramBotToken, chatIds: config.telegram.chatIds, apiBaseUrl: config.telegram.apiBaseUrl })
      : null);
  const dispatcher = new NotificationDispatcher({ repo, telegram, logger, intervalMs: overrides.notifyIntervalMs ?? 3000 });
  const bot = telegram && config.telegram.commandsEnabled && !overrides.disableBot ? new TelegramBot({ client: telegram, admin, config, logger }) : null;

  const server = config.api.enabled ? await buildServer({ config, secrets, intake, admin, logger }) : null;

  return { config, logger, signer, knex, repo, provider, tron, usdt, settings, funding, queue, intake, admin, telegram, dispatcher, bot, server };
}
