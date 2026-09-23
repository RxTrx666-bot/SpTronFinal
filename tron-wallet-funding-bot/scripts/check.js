// Pre-flight check: validates configuration, derives the Mother Wallet address,
// verifies the RPC network (genesis block) and the USDT contract, and prints
// balances. Sends nothing, writes nothing except running DB migrations.
//   npm run check
import { existsSync } from 'node:fs';
import { loadConfig } from '../src/config/config.js';
import { createLogger } from '../src/logger.js';
import { createApp } from '../src/app.js';
import { scrubError } from '../src/security/redact.js';
import { sunToTrx, formatUnits } from '../src/blockchain/units.js';

if (existsSync('.env') && typeof process.loadEnvFile === 'function') process.loadEnvFile('.env');
let app;
try {
  const { config, secrets } = loadConfig(process.env);
  const logger = createLogger({ level: 'warn' });
  app = await createApp({ config, secrets, logger, overrides: { disableBot: true } });
  console.log(`Mother wallet:\n${app.signer.address}\n`);
  console.log(`Network:          ${config.network.name}${config.dryRun ? '  (DRY_RUN=true)' : ''}`);
  const h = await app.tron.checkConnection();
  console.log(`RPC:              OK (genesis verified, head block ${h.headBlock}, lag ${h.lagSeconds}s)`);
  const u = await app.usdt.verify({ network: config.network, expectedSymbol: config.usdt.expectedSymbol, allowNonstandard: config.usdt.allowNonstandardContract });
  console.log(`USDT contract:    OK ${config.usdt.contract} (${u.name} / ${u.symbol} / ${u.decimals} decimals)`);
  const b = await app.admin.balances();
  console.log(`Mother TRX:       ${b.trx}`);
  console.log(`Mother USDT:      ${b.usdt}`);
  const a = app.settings.fundingAmounts();
  console.log(`Per wallet:       ${sunToTrx(a.trxAmountSun)} TRX + ${formatUnits(a.usdtAmountUnits, 6)} USDT`);
  console.log(`Paused:           ${app.settings.paused}`);
  console.log('\nAll checks passed.');
} catch (err) {
  console.error(`CHECK FAILED: ${scrubError(err).message}`);
  process.exitCode = 1;
} finally {
  await app?.knex.destroy();
}
