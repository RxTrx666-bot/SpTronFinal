// HTTP routes: wallet intake (internal bots) and admin/control endpoints.

import { requireKey } from './authentication.js';
import { ValidationError } from '../wallet/validation.js';
import { SettingsError } from '../admin/settings.js';
import { FundingError } from '../blockchain/errors.js';

const walletSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['address'],
  properties: {
    address: { type: 'string', minLength: 1, maxLength: 128 },
    private_key: { type: 'string', maxLength: 130 },
    source: { type: 'string', maxLength: 64 },
    repeat: { type: 'boolean' },
  },
};

function presentSubmission(result, admin) {
  return { status: result.status, job: admin.presentJob(result.job) };
}

export function registerRoutes(app, { intake, admin, config, checkInternal, checkAdmin }) {
  const internalAuth = requireKey(checkInternal);
  const adminAuth = requireKey(checkAdmin);

  const privateKeyAllowed = (request) => request.protocol === 'https' || config.api.allowPrivateKeyOverHttp;

  async function submitOne(body, request) {
    if (body.private_key !== undefined && !privateKeyAllowed(request)) {
      // Do not even look at the key over plaintext HTTP.
      delete body.private_key;
      throw new ValidationError('HTTPS_REQUIRED', 'private_key may only be sent over HTTPS');
    }
    const privateKey = body.private_key;
    delete body.private_key;
    return intake.submit({ address: body.address, privateKey, source: body.source, repeat: body.repeat === true });
  }

  // ---------------------------------------------------------------- public --
  app.get('/health', async () => {
    const h = await admin.health();
    return { status: h.status };
  });
  app.get('/health/live', async () => ({ status: 'ok' }));

  // --------------------------------------------------------------- internal --
  app.post('/wallets', { onRequest: internalAuth, schema: { body: walletSchema } }, async (request, reply) => {
    const result = await submitOne(request.body, request);
    return reply.code(result.status === 'created' ? 201 : 200).send(presentSubmission(result, admin));
  });

  app.post(
    '/wallets/batch',
    {
      onRequest: internalAuth,
      schema: {
        body: {
          type: 'object',
          additionalProperties: false,
          required: ['wallets'],
          properties: { wallets: { type: 'array', minItems: 1, maxItems: config.api.maxBatchSize, items: walletSchema } },
        },
      },
    },
    async (request) => {
      const results = [];
      for (const w of request.body.wallets) {
        try {
          results.push(presentSubmission(await submitOne(w, request), admin));
        } catch (err) {
          if (err instanceof ValidationError) {
            // Echo the address only if it is a syntactically valid address.
            results.push({ status: 'rejected', error: err.code, message: err.message, address: /^T[1-9A-HJ-NP-Za-km-z]{33}$/.test(w.address) ? w.address : undefined });
          } else throw err;
        }
      }
      return { results };
    },
  );

  app.get('/wallets/:address', { onRequest: internalAuth }, async (request, reply) => {
    const { address } = request.params;
    if (!/^T[1-9A-HJ-NP-Za-km-z]{33}$/.test(address)) return reply.code(400).send({ error: 'INVALID_ADDRESS', message: 'Invalid TRON address' });
    const j = await admin.job(address);
    if (!j) return reply.code(404).send({ error: 'NOT_FOUND', message: 'No funding job for this address' });
    return j;
  });

  // ------------------------------------------------------------------ admin --
  if (!config.adminApiEnabled) return;

  app.get('/admin/health', { onRequest: adminAuth }, async () => admin.health({ deep: true }));
  app.get('/admin/balance', { onRequest: adminAuth }, async () => admin.balances());
  app.get(
    '/admin/jobs',
    {
      onRequest: adminAuth,
      schema: {
        querystring: {
          type: 'object',
          additionalProperties: false,
          properties: {
            status: { type: 'string', maxLength: 16 },
            limit: { type: 'integer', minimum: 1, maximum: 500 },
          },
        },
      },
    },
    async (request) => ({ jobs: await admin.jobs(request.query.status ?? 'pending', request.query.limit ?? 50) }),
  );
  app.get('/admin/jobs/:id', { onRequest: adminAuth }, async (request, reply) => {
    const j = await admin.job(request.params.id);
    if (!j) return reply.code(404).send({ error: 'NOT_FOUND', message: 'Job not found' });
    return j;
  });
  app.post('/admin/jobs/:id/retry', { onRequest: adminAuth }, async (request) => ({ job: await admin.retry(request.params.id, 'admin-api') }));
  app.post('/admin/pause', { onRequest: adminAuth }, async () => admin.pause('admin-api'));
  app.post('/admin/resume', { onRequest: adminAuth }, async () => admin.resume('admin-api'));
  app.get('/admin/settings', { onRequest: adminAuth }, async () => admin.settings.snapshot());
  app.put(
    '/admin/settings/funding',
    {
      onRequest: adminAuth,
      schema: {
        body: {
          type: 'object',
          additionalProperties: false,
          minProperties: 1,
          properties: {
            trx_amount: { type: 'string', pattern: '^\\d+(\\.\\d{1,6})?$' },
            usdt_amount: { type: 'string', pattern: '^\\d+(\\.\\d{1,6})?$' },
          },
        },
      },
    },
    async (request) => {
      let snap;
      if (request.body.trx_amount !== undefined) snap = await admin.setAmount('TRX', request.body.trx_amount, 'admin-api');
      if (request.body.usdt_amount !== undefined) snap = await admin.setAmount('USDT', request.body.usdt_amount, 'admin-api');
      return snap;
    },
  );
}

/** Map errors to safe HTTP responses (never echo request bodies). */
export function errorHandler(logger) {
  return (err, request, reply) => {
    if (err instanceof ValidationError) {
      return reply.code(err.code === 'HTTPS_REQUIRED' ? 403 : 422).send({ error: err.code, message: err.message });
    }
    if (err instanceof SettingsError) return reply.code(422).send({ error: err.code, message: err.message });
    if (err instanceof FundingError && ['NOT_FOUND', 'NOT_RETRYABLE'].includes(err.code)) {
      return reply.code(err.code === 'NOT_FOUND' ? 404 : 409).send({ error: err.code, message: err.message });
    }
    if (err.validation) {
      return reply.code(400).send({ error: 'MALFORMED_REQUEST', message: 'Request does not match the expected schema' });
    }
    if (err.statusCode === 429) return reply.code(429).send({ error: 'RATE_LIMITED', message: 'Too many requests' });
    if (err.statusCode === 413) return reply.code(413).send({ error: 'PAYLOAD_TOO_LARGE', message: 'Request body too large' });
    if (err.statusCode === 415) return reply.code(415).send({ error: 'UNSUPPORTED_MEDIA_TYPE', message: 'Use application/json' });
    if (err.statusCode && err.statusCode >= 400 && err.statusCode < 500) {
      // Includes JSON parse errors, whose native messages quote the raw body.
      return reply.code(400).send({ error: 'MALFORMED_REQUEST', message: 'Malformed request' });
    }
    logger.error({ route: request.routeOptions?.url, error: err.message }, 'API internal error');
    return reply.code(500).send({ error: 'INTERNAL_ERROR', message: 'Internal error' });
  };
}
