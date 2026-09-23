// Database connection (Knex). SQLite by default; PostgreSQL by setting
// DATABASE_URL=postgres://user:pass@host:5432/db (install the optional `pg`
// dependency). All SQL goes through Knex so both dialects are supported.

import { mkdirSync, chmodSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import knexFactory from 'knex';

const MIGRATIONS_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../migrations');

export function createKnex(databaseUrl) {
  if (databaseUrl.startsWith('sqlite:')) {
    const file = databaseUrl.slice('sqlite:'.length) || ':memory:';
    if (file !== ':memory:') {
      mkdirSync(dirname(resolve(file)), { recursive: true, mode: 0o700 });
    }
    return knexFactory({
      client: 'better-sqlite3',
      connection: { filename: file },
      useNullAsDefault: true,
      pool: {
        min: 1,
        max: 1, // single connection: serialises writes, no SQLITE_BUSY between our own queries
        afterCreate(conn, done) {
          conn.pragma('journal_mode = WAL');
          conn.pragma('foreign_keys = ON');
          conn.pragma('busy_timeout = 5000');
          conn.pragma('synchronous = FULL'); // durability matters more than speed here
          done(null, conn);
        },
      },
    });
  }
  return knexFactory({
    client: 'pg',
    connection: databaseUrl,
    pool: { min: 1, max: 5 },
  });
}

export async function migrate(knex, databaseUrl) {
  await knex.migrate.latest({ directory: MIGRATIONS_DIR, loadExtensions: ['.js'] });
  // Database access control: the SQLite file is readable by the service user only.
  if (databaseUrl?.startsWith('sqlite:')) {
    const file = databaseUrl.slice('sqlite:'.length);
    if (file && file !== ':memory:') {
      for (const f of [file, `${file}-wal`, `${file}-shm`]) {
        try {
          chmodSync(f, 0o600);
        } catch {
          /* file may not exist yet */
        }
      }
    }
  }
}
