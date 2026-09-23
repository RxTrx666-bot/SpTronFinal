// Initial schema. Works on SQLite (default) and PostgreSQL.
// Token amounts are stored as decimal strings of integer base units (sun /
// USDT 6-decimal units) to avoid any floating point or 64-bit overflow issue.
// Timestamps are stored as integer milliseconds since epoch.
//
// No column in this schema stores a private key in plaintext.

export async function up(knex) {
  await knex.schema.createTable('wallets', (t) => {
    t.increments('id').primary();
    t.string('address', 34).notNullable().unique();
    t.string('source', 64);
    t.integer('submission_count').notNullable().defaultTo(1);
    t.boolean('has_encrypted_key').notNullable().defaultTo(false);
    t.bigInteger('first_received_at').notNullable();
    t.bigInteger('last_received_at').notNullable();
  });

  await knex.schema.createTable('funding_jobs', (t) => {
    t.string('id', 36).primary();
    t.string('address', 34).notNullable().references('address').inTable('wallets');
    t.string('mode', 8).notNullable(); // live | dry_run
    t.integer('round').notNullable().defaultTo(1);
    t.string('status', 16).notNullable();
    t.string('trx_amount_sun', 40).notNullable();
    t.string('usdt_amount_units', 40).notNullable();
    t.string('trx_sent_sun', 40);
    t.string('usdt_sent_units', 40);
    t.string('trx_txid', 64);
    t.string('usdt_txid', 64);
    t.string('error_code', 64);
    t.text('error_message');
    t.integer('retry_count').notNullable().defaultTo(0);
    t.bigInteger('next_attempt_at').notNullable().defaultTo(0);
    t.string('source', 64);
    t.bigInteger('received_at').notNullable();
    t.bigInteger('updated_at').notNullable();
    t.bigInteger('funded_at');
    // Idempotency: one job per address per mode per funding round.
    t.unique(['address', 'mode', 'round']);
    t.index(['status', 'next_attempt_at']);
  });

  await knex.schema.createTable('transfers', (t) => {
    t.increments('id').primary();
    t.string('job_id', 36).notNullable().references('id').inTable('funding_jobs');
    t.string('asset', 8).notNullable(); // TRX | USDT
    t.string('to_address', 34).notNullable();
    t.string('amount', 40).notNullable();
    t.string('status', 16).notNullable(); // SIGNED | BROADCAST | REJECTED | CONFIRMED | FAILED | EXPIRED | DRY_RUN
    t.string('txid', 64).unique();
    // Signed transaction (public data: contains the signature, never the key).
    // Kept so the *same* transaction can be re-broadcast safely after a crash.
    t.text('signed_tx');
    t.bigInteger('expiration_at');
    t.integer('block_number');
    t.string('fee_sun', 40);
    t.string('error_code', 64);
    t.text('error_message');
    t.integer('broadcast_attempts').notNullable().defaultTo(0);
    t.bigInteger('created_at').notNullable();
    t.bigInteger('updated_at').notNullable();
    t.bigInteger('confirmed_at');
    t.index(['job_id', 'asset']);
    t.index(['status']);
  });

  await knex.schema.createTable('job_events', (t) => {
    t.increments('id').primary();
    t.string('job_id', 36).notNullable().references('id').inTable('funding_jobs');
    t.string('from_status', 16);
    t.string('to_status', 16);
    t.text('message');
    t.bigInteger('created_at').notNullable();
    t.index(['job_id']);
  });

  // Notification outbox: delivery state is fully independent of funding state.
  await knex.schema.createTable('notifications', (t) => {
    t.increments('id').primary();
    t.string('job_id', 36);
    t.string('kind', 32).notNullable();
    t.text('text').notNullable();
    t.string('status', 12).notNullable(); // PENDING | SENT | FAILED
    t.integer('attempts').notNullable().defaultTo(0);
    t.bigInteger('next_attempt_at').notNullable().defaultTo(0);
    t.text('last_error');
    t.bigInteger('created_at').notNullable();
    t.bigInteger('sent_at');
    t.unique(['job_id', 'kind']);
    t.index(['status', 'next_attempt_at']);
  });

  await knex.schema.createTable('settings', (t) => {
    t.string('key', 64).primary();
    t.text('value').notNullable();
    t.string('updated_by', 64);
    t.bigInteger('updated_at').notNullable();
  });

  // Single-instance lease: prevents two bot processes spending from the same
  // Mother Wallet at the same time.
  await knex.schema.createTable('leases', (t) => {
    t.string('name', 64).primary();
    t.string('owner', 64).notNullable();
    t.bigInteger('expires_at').notNullable();
  });

  // Only used when TARGET_PRIVATE_KEY_POLICY=encrypt (off by default).
  await knex.schema.createTable('target_wallet_secrets', (t) => {
    t.string('address', 34).primary().references('address').inTable('wallets');
    t.text('ciphertext').notNullable();
    t.string('iv', 32).notNullable();
    t.string('tag', 32).notNullable();
    t.string('key_id', 32).notNullable();
    t.bigInteger('created_at').notNullable();
  });
}

export async function down(knex) {
  for (const table of ['target_wallet_secrets', 'leases', 'settings', 'notifications', 'job_events', 'transfers', 'funding_jobs', 'wallets']) {
    await knex.schema.dropTableIfExists(table);
  }
}
