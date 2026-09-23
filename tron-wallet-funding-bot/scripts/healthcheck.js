// Container / monitoring health probe. Exit 0 when the service reports ok or
// degraded, 1 otherwise. Talks to the local API only.
import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';

const port = Number(process.env.API_PORT || 8080);
const tls = Boolean(process.env.TLS_CERT_FILE && process.env.TLS_KEY_FILE);
const host = process.env.HEALTHCHECK_HOST || '127.0.0.1';
const req = (tls ? httpsRequest : httpRequest)(
  // Local loopback probe of our own certificate; hostname is not the cert CN.
  { host, port, path: '/health', method: 'GET', timeout: 5000, rejectUnauthorized: false },
  (res) => {
    let body = '';
    res.on('data', (c) => (body += c));
    res.on('end', () => {
      try {
        const { status } = JSON.parse(body);
        console.log(status);
        process.exit(status === 'ok' || status === 'degraded' ? 0 : 1);
      } catch {
        process.exit(1);
      }
    });
  },
);
req.on('timeout', () => req.destroy(new Error('timeout')));
req.on('error', (e) => {
  console.error(`unhealthy: ${e.message}`);
  process.exit(1);
});
req.end();
