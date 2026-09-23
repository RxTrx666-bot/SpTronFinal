// Bearer-token authentication with constant-time comparison.
// Two separate keys (least privilege):
//   INTERNAL_API_KEY -> may submit wallets and read their status
//   ADMIN_API_KEY    -> admin/control endpoints
// Credentials are only accepted in the Authorization header, never in URLs.

import { createHash, timingSafeEqual } from 'node:crypto';

const digest = (s) => createHash('sha256').update(String(s)).digest();

export function makeKeyChecker(expected) {
  if (!expected) return () => false;
  const want = digest(expected);
  return (provided) => {
    if (typeof provided !== 'string' || provided.length === 0 || provided.length > 512) return false;
    return timingSafeEqual(digest(provided), want);
  };
}

export function bearerToken(request) {
  const h = request.headers.authorization;
  if (typeof h !== 'string') return null;
  const m = /^Bearer\s+(\S+)\s*$/i.exec(h);
  return m ? m[1] : null;
}

const CREDENTIAL_QUERY = /(^|&)(api[_-]?key|key|token|access[_-]?token|private[_-]?key|secret|password|auth)=/i;

export function hasCredentialInQuery(url) {
  const i = url.indexOf('?');
  return i >= 0 && CREDENTIAL_QUERY.test(url.slice(i + 1));
}

/** Fastify onRequest hook factory (runs before the body is parsed). */
export function requireKey(check) {
  return async (request, reply) => {
    const token = bearerToken(request);
    if (!check(token)) {
      reply.header('www-authenticate', 'Bearer');
      return reply.code(401).send({ error: 'UNAUTHORIZED', message: 'Missing or invalid bearer token' });
    }
  };
}
