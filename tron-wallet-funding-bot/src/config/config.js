// Configuration loading and validation.
//
// loadConfig() returns { config, secrets }:
//   - config: frozen, non-sensitive settings (safe to print via describeConfig)
//   - secrets: sensitive values, loaded via env or *_FILE, registered for scrubbing
//
// Invalid configuration aborts startup with a list of problems. Messages never
// contain secret values.

import { NETWORKS } from './networks.js';
import { loadSecret, SecretError } from '../security/secrets.js';
import { registerSecret } from '../security/redact.js';
import { isValidTronAddress } from '../wallet/validation.js';
import { parseUnits, TRX_DECIMALS, AmountError } from '../blockchain/units.js';

export const USDT_DECIMALS = 6;

export class ConfigError extends Error {
  constructor(problems) {
    super(`Invalid configuration:\n - ${problems.join('\n - ')}`);
    this.name = 'ConfigError';
    this.problems = problems;
  }
}

function bool(env, name, def) {
  const v = env[name];
  if (v === undefined || v === '') return def;
  if (/^(1|true|yes|on)$/i.test(v)) return true;
  if (/^(0|false|no|off)$/i.test(v)) return false;
  throw new Error(`${name} must be true/false`);
}

function int(env, name, def, { min = -Infinity, max = Infinity } = {}) {
  const v = env[name];
  if (v === undefined || v === '') return def;
  if (!/^-?\d+$/.test(v)) throw new Error(`${name} must be an integer`);
  const n = Number(v);
  if (n < min || n > max) throw new Error(`${name} must be between ${min} and ${max}`);
  return n;
}

function str(env, name, def) {
  const v = env[name];
  return v === undefined || v === '' ? def : String(v).trim();
}

function amount(env, name, def, decimals) {
  const v = str(env, name, def);
  try {
    return parseUnits(v, decimals);
  } catch (err) {
    if (err instanceof AmountError) throw new Error(`${name}: ${err.message}`);
    throw err;
  }
}

function url(env, name, def) {
  const v = str(env, name, def);
  if (!v) return v;
  let u;
  try {
    u = new URL(v);
  } catch {
    throw new Error(`${name} is not a valid URL`);
  }
  if (u.username || u.password) throw new Error(`${name} must not contain credentials; use TRON_API_KEY`);
  if (u.search) throw new Error(`${name} must not contain query parameters (never put credentials in URLs)`);
  return v.replace(/\/+$/, '');
}

export function loadConfig(env = process.env) {
  const problems = [];
  const attempt = (fn, fallback) => {
    try {
      return fn();
    } catch (err) {
      problems.push(err.message);
      return fallback;
    }
  };

  // ---- secrets -------------------------------------------------------------
  const secret = (name, required) =>
    attempt(() => loadSecret(name, { required, env }), undefined);

  const secrets = {
    motherPrivateKey: secret('MOTHER_PRIVATE_KEY', true),
    tronApiKey: secret('TRON_API_KEY', false),
    internalApiKey: secret('INTERNAL_API_KEY', false),
    adminApiKey: secret('ADMIN_API_KEY', false),
    telegramBotToken: secret('TELEGRAM_BOT_TOKEN', false),
    targetKeyEncryptionKey: secret('TARGET_KEY_ENCRYPTION_KEY', false),
  };

  // ---- network ---------------------------------------------------------------
  const networkName = str(env, 'TRON_NETWORK', 'mainnet');
  const network = NETWORKS[networkName];
  if (!network) problems.push(`TRON_NETWORK must be one of: ${Object.keys(NETWORKS).join(', ')}`);

  const rpcUrl = attempt(() => url(env, 'TRON_RPC_URL', ''), '');
  if (!rpcUrl) problems.push('TRON_RPC_URL is required (e.g. https://api.trongrid.io)');
  else if (!rpcUrl.startsWith('https://') && !bool(env, 'ALLOW_INSECURE_RPC', false)) {
    problems.push('TRON_RPC_URL must use https:// (set ALLOW_INSECURE_RPC=true only for a local node)');
  }
  const solidityUrl = attempt(() => url(env, 'TRON_SOLIDITY_URL', rpcUrl), rpcUrl);

  const usdtContract = str(env, 'USDT_CONTRACT_ADDRESS', '');
  if (!usdtContract) problems.push('USDT_CONTRACT_ADDRESS is required');
  else if (!isValidTronAddress(usdtContract)) problems.push('USDT_CONTRACT_ADDRESS is not a valid TRON address');

  const expectedGenesis = str(env, 'EXPECTED_GENESIS_BLOCK_ID', network?.genesisBlockId);
  if (expectedGenesis && !/^[0-9a-f]{64}$/.test(expectedGenesis)) {
    problems.push('EXPECTED_GENESIS_BLOCK_ID must be 64 lowercase hex characters');
  }

  // ---- funding amounts --------------------------------------------------------
  const trxAmountSun = attempt(() => amount(env, 'TRX_FUNDING_AMOUNT', '0', TRX_DECIMALS), 0n);
  const usdtAmountUnits = attempt(() => amount(env, 'USDT_FUNDING_AMOUNT', '0', USDT_DECIMALS), 0n);
  const maxTrxPerWalletSun = attempt(() => amount(env, 'MAX_TRX_PER_WALLET', '100', TRX_DECIMALS), 0n);
  const maxUsdtPerWalletUnits = attempt(() => amount(env, 'MAX_USDT_PER_WALLET', '1000', USDT_DECIMALS), 0n);
  if (trxAmountSun > maxTrxPerWalletSun) problems.push('TRX_FUNDING_AMOUNT exceeds MAX_TRX_PER_WALLET');
  if (usdtAmountUnits > maxUsdtPerWalletUnits) problems.push('USDT_FUNDING_AMOUNT exceeds MAX_USDT_PER_WALLET');

  const funding = {
    trxAmountSun,
    usdtAmountUnits,
    maxTrxPerWalletSun,
    maxUsdtPerWalletUnits,
    dailyTrxLimitSun: attempt(() => amount(env, 'DAILY_TRX_LIMIT', '0', TRX_DECIMALS), 0n),
    dailyUsdtLimitUnits: attempt(() => amount(env, 'DAILY_USDT_LIMIT', '0', USDT_DECIMALS), 0n),
    usdtFeeLimitSun: attempt(() => amount(env, 'USDT_FEE_LIMIT_TRX', '30', TRX_DECIMALS), 0n),
    trxFeeBufferSun: attempt(() => amount(env, 'TRX_FEE_BUFFER_TRX', '2', TRX_DECIMALS), 0n),
    minMotherTrxReserveSun: attempt(() => amount(env, 'MIN_MOTHER_TRX_RESERVE', '0', TRX_DECIMALS), 0n),
    allowRepeatFunding: attempt(() => bool(env, 'ALLOW_REPEAT_FUNDING', false), false),
    allowContractDestinations: attempt(() => bool(env, 'ALLOW_CONTRACT_DESTINATIONS', false), false),
    autoPauseOnInsufficientBalance: attempt(() => bool(env, 'AUTO_PAUSE_ON_INSUFFICIENT_BALANCE', true), true),
  };
  if (funding.usdtFeeLimitSun > 1000n * 1_000_000n) problems.push('USDT_FEE_LIMIT_TRX must be <= 1000');

  const tx = {
    expirationSeconds: attempt(() => int(env, 'TX_EXPIRATION_SECONDS', 90, { min: 30, max: 3600 }), 90),
    expirySafetyMarginSeconds: attempt(() => int(env, 'EXPIRY_SAFETY_MARGIN_SECONDS', 60, { min: 10, max: 3600 }), 60),
    confirmationTimeoutSeconds: attempt(() => int(env, 'CONFIRMATION_TIMEOUT_SECONDS', 240, { min: 10, max: 3600 }), 240),
    confirmationPollMs: attempt(() => int(env, 'CONFIRMATION_POLL_MS', 3000, { min: 10, max: 60000 }), 3000),
  };

  const queue = {
    concurrency: attempt(() => int(env, 'QUEUE_CONCURRENCY', 1, { min: 1, max: 10 }), 1),
    pollIntervalMs: attempt(() => int(env, 'WORKER_POLL_INTERVAL_MS', 2000, { min: 10, max: 60000 }), 2000),
    maxAutoRetries: attempt(() => int(env, 'MAX_AUTO_RETRIES', 5, { min: 0, max: 100 }), 5),
    retryBaseDelayMs: attempt(() => int(env, 'RETRY_BASE_DELAY_MS', 30000, { min: 10, max: 3600000 }), 30000),
    leaseTtlSeconds: attempt(() => int(env, 'INSTANCE_LEASE_TTL_SECONDS', 30, { min: 5, max: 600 }), 30),
  };

  const rpc = {
    url: rpcUrl,
    solidityUrl,
    apiKeyHeader: str(env, 'TRON_API_KEY_HEADER', 'TRON-PRO-API-KEY'),
    timeoutMs: attempt(() => int(env, 'RPC_TIMEOUT_MS', 10000, { min: 500, max: 120000 }), 10000),
    maxRetries: attempt(() => int(env, 'RPC_MAX_RETRIES', 3, { min: 0, max: 10 }), 3),
    minIntervalMs: attempt(() => int(env, 'RPC_MIN_INTERVAL_MS', 0, { min: 0, max: 10000 }), 0),
  };

  // ---- API --------------------------------------------------------------------
  const api = {
    enabled: attempt(() => bool(env, 'API_ENABLED', true), true),
    host: str(env, 'API_HOST', '127.0.0.1'),
    port: attempt(() => int(env, 'API_PORT', 8080, { min: 1, max: 65535 }), 8080),
    tlsCertFile: str(env, 'TLS_CERT_FILE', ''),
    tlsKeyFile: str(env, 'TLS_KEY_FILE', ''),
    trustProxy: str(env, 'TRUST_PROXY', ''),
    allowInsecureHttp: attempt(() => bool(env, 'ALLOW_INSECURE_HTTP', false), false),
    allowPrivateKeyOverHttp: attempt(() => bool(env, 'ALLOW_PRIVATE_KEY_OVER_HTTP', false), false),
    rateLimitMax: attempt(() => int(env, 'API_RATE_LIMIT_MAX', 60, { min: 1, max: 100000 }), 60),
    rateLimitWindowMs: attempt(() => int(env, 'API_RATE_LIMIT_WINDOW_MS', 60000, { min: 1000, max: 3600000 }), 60000),
    bodyLimitBytes: attempt(() => int(env, 'API_BODY_LIMIT_BYTES', 65536, { min: 1024, max: 1048576 }), 65536),
    maxBatchSize: attempt(() => int(env, 'API_MAX_BATCH_SIZE', 100, { min: 1, max: 1000 }), 100),
  };
  const loopback = ['127.0.0.1', '::1', 'localhost'].includes(api.host);
  const tls = Boolean(api.tlsCertFile && api.tlsKeyFile);
  if ((api.tlsCertFile && !api.tlsKeyFile) || (!api.tlsCertFile && api.tlsKeyFile)) {
    problems.push('TLS_CERT_FILE and TLS_KEY_FILE must be set together');
  }
  if (api.enabled) {
    if (!secrets.internalApiKey) problems.push('INTERNAL_API_KEY is required when API_ENABLED=true');
    if (!loopback && !tls && !api.allowInsecureHttp) {
      problems.push('API_HOST is not loopback and no TLS is configured: set TLS_CERT_FILE/TLS_KEY_FILE, bind to 127.0.0.1 behind an HTTPS reverse proxy, or (not recommended) ALLOW_INSECURE_HTTP=true');
    }
  }
  for (const [name, value] of [['INTERNAL_API_KEY', secrets.internalApiKey], ['ADMIN_API_KEY', secrets.adminApiKey]]) {
    if (value && value.length < 32) problems.push(`${name} must be at least 32 characters (use: openssl rand -hex 32)`);
  }
  if (secrets.internalApiKey && secrets.adminApiKey && secrets.internalApiKey === secrets.adminApiKey) {
    problems.push('ADMIN_API_KEY must differ from INTERNAL_API_KEY (least privilege)');
  }

  // ---- target private key policy ----------------------------------------------
  const targetKeyPolicy = str(env, 'TARGET_PRIVATE_KEY_POLICY', 'discard');
  if (!['reject', 'discard', 'encrypt'].includes(targetKeyPolicy)) {
    problems.push('TARGET_PRIVATE_KEY_POLICY must be reject, discard or encrypt');
  }
  if (targetKeyPolicy === 'encrypt') {
    const k = secrets.targetKeyEncryptionKey;
    if (!k || Buffer.from(k, 'base64').length !== 32) {
      problems.push('TARGET_KEY_ENCRYPTION_KEY must be 32 random bytes, base64 encoded (openssl rand -base64 32) when TARGET_PRIVATE_KEY_POLICY=encrypt');
    }
  }

  // ---- telegram ---------------------------------------------------------------
  const chatIds = str(env, 'TELEGRAM_CHAT_ID', '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  for (const id of chatIds) if (!/^-?\d+$/.test(id)) problems.push('TELEGRAM_CHAT_ID must be numeric (comma separated for several)');
  const adminUserIds = str(env, 'TELEGRAM_ADMIN_USER_IDS', '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  for (const id of adminUserIds) if (!/^\d+$/.test(id)) problems.push('TELEGRAM_ADMIN_USER_IDS must be numeric user ids');
  const telegram = {
    enabled: Boolean(secrets.telegramBotToken && chatIds.length),
    chatIds,
    adminUserIds,
    commandsEnabled: attempt(() => bool(env, 'TELEGRAM_COMMANDS_ENABLED', true), true),
    apiBaseUrl: attempt(() => url(env, 'TELEGRAM_API_BASE_URL', 'https://api.telegram.org'), 'https://api.telegram.org'),
  };
  if (secrets.telegramBotToken && !chatIds.length) problems.push('TELEGRAM_CHAT_ID is required when TELEGRAM_BOT_TOKEN is set');

  // ---- database -----------------------------------------------------------------
  const databaseUrl = str(env, 'DATABASE_URL', 'sqlite:./data/funding.db');
  if (!/^(sqlite:|postgres(ql)?:\/\/)/.test(databaseUrl)) {
    problems.push('DATABASE_URL must start with sqlite: or postgres://');
  }
  try {
    if (databaseUrl.startsWith('postgres')) {
      const u = new URL(databaseUrl);
      if (u.password) registerSecret(decodeURIComponent(u.password));
    }
  } catch {
    problems.push('DATABASE_URL is not a valid URL');
  }

  const config = {
    network: network ?? NETWORKS.mainnet,
    expectedGenesisBlockId: expectedGenesis,
    usdt: {
      contract: usdtContract,
      decimals: USDT_DECIMALS,
      expectedSymbol: str(env, 'USDT_EXPECTED_SYMBOL', 'USDT'),
      allowNonstandardContract: attempt(() => bool(env, 'USDT_ALLOW_NONSTANDARD_CONTRACT', false), false),
    },
    rpc,
    funding,
    tx,
    queue,
    api: { ...api, tls },
    telegram,
    targetKeyPolicy,
    databaseUrl,
    dryRun: attempt(() => bool(env, 'DRY_RUN', false), false),
    startPaused: attempt(() => bool(env, 'START_PAUSED', false), false),
    log: {
      level: str(env, 'LOG_LEVEL', 'info'),
      format: str(env, 'LOG_FORMAT', 'text'),
      file: str(env, 'LOG_FILE', ''),
    },
  };

  if (problems.length) throw new ConfigError(problems);
  return { config: deepFreeze(config), secrets };
}

function deepFreeze(o) {
  for (const v of Object.values(o)) if (v && typeof v === 'object' && !Object.isFrozen(v)) deepFreeze(v);
  return Object.freeze(o);
}

export { SecretError };
