// Run database migrations only (needs DATABASE_URL, nothing else).
//   npm run migrate
import { existsSync } from 'node:fs';
import { createKnex, migrate } from '../src/database/database.js';
import { scrub } from '../src/security/redact.js';

if (existsSync('.env') && typeof process.loadEnvFile === 'function') process.loadEnvFile('.env');
const url = process.env.DATABASE_URL || 'sqlite:./data/funding.db';
const knex = createKnex(url);
try {
  await migrate(knex, url);
  const [, done] = await knex.migrate.list({ directory: new URL('../migrations', import.meta.url).pathname, loadExtensions: ['.js'] });
  console.log(`Migrations up to date (${url.startsWith('sqlite:') ? url : 'postgres'}). Pending: ${done.length}`);
} catch (err) {
  console.error(`Migration failed: ${scrub(err.message)}`);
  process.exitCode = 1;
} finally {
  await knex.destroy();
}
