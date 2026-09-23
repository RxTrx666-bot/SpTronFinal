// Fastify HTTP(S) server.

import { readFileSync } from 'node:fs';
import Fastify from 'fastify';
import rateLimit from '@fastify/rate-limit';
import { registerRoutes, errorHandler } from './routes.js';
import { makeKeyChecker, hasCredentialInQuery } from './authentication.js';

function parseTrustProxy(v) {
  if (!v) return false;
  if (/^(true|1)$/i.test(v)) return true;
  if (/^\d+$/.test(v)) return Number(v);
  return v.split(',').map((s) => s.trim());
}

export async function buildServer({ config, secrets, intake, admin, logger }) {
  const https = config.api.tls ? { key: readFileSync(config.api.tlsKeyFile), cert: readFileSync(config.api.tlsCertFile) } : null;
  const app = Fastify({
    logger: false, // we log ourselves, without bodies or query strings
    https,
    bodyLimit: config.api.bodyLimitBytes,
    trustProxy: parseTrustProxy(config.api.trustProxy),
    return503OnClosing: true,
    ajv: { customOptions: { removeAdditional: false, allErrors: false } },
  });

  await app.register(rateLimit, {
    global: true,
    max: config.api.rateLimitMax,
    timeWindow: config.api.rateLimitWindowMs,
    allowList: (req) => req.url === '/health/live',
  });

  app.addHook('onRequest', async (request, reply) => {
    if (hasCredentialInQuery(request.url)) {
      return reply.code(400).send({ error: 'CREDENTIALS_IN_URL', message: 'Credentials must never be sent in the URL; use the Authorization header / JSON body' });
    }
    reply.header('cache-control', 'no-store');
    reply.header('x-content-type-options', 'nosniff');
    if (config.api.tls) reply.header('strict-transport-security', 'max-age=31536000');
  });

  app.addHook('onResponse', async (request, reply) => {
    const path = request.url.split('?')[0];
    logger.info({ method: request.method, path: path.replace(/\/T[1-9A-HJ-NP-Za-km-z]{33}/, '/:address'), status: reply.statusCode, ms: Math.round(reply.elapsedTime), ip: request.ip }, 'HTTP request');
  });

  app.setErrorHandler(errorHandler(logger));
  app.setNotFoundHandler((request, reply) => reply.code(404).send({ error: 'NOT_FOUND', message: 'Not found' }));

  registerRoutes(app, {
    intake,
    admin,
    config: { ...config, adminApiEnabled: Boolean(secrets.adminApiKey) },
    checkInternal: makeKeyChecker(secrets.internalApiKey),
    checkAdmin: makeKeyChecker(secrets.adminApiKey),
  });
  return app;
}
