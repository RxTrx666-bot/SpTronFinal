// Minimal structured logger. Every line is scrubbed of registered secrets and
// secret-looking fields before being written. No third-party transport is used
// so there is exactly one place where output is produced.

import { appendFileSync } from 'node:fs';
import { scrub, scrubObject } from './security/redact.js';

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40, fatal: 50, silent: 100 };

function formatValue(v) {
  if (v === null || v === undefined) return String(v);
  if (typeof v === 'string') return /\s/.test(v) ? JSON.stringify(v) : v;
  return JSON.stringify(v);
}

export function createLogger({ level = 'info', format = 'text', file, sink } = {}) {
  const threshold = LEVELS[level] ?? LEVELS.info;

  function write(line) {
    if (sink) return sink(line);
    process.stdout.write(`${line}\n`);
    if (file) {
      try {
        appendFileSync(file, `${line}\n`, { mode: 0o640 });
      } catch {
        // never crash because of log file problems
      }
    }
  }

  function log(lvl, fields, msg) {
    if (LEVELS[lvl] < threshold) return;
    if (typeof fields === 'string') {
      msg = fields;
      fields = {};
    }
    const safeFields = scrubObject(Object.fromEntries(Object.entries(fields ?? {}).filter(([, v]) => v !== undefined)));
    const safeMsg = scrub(msg ?? '');
    const time = new Date().toISOString();
    let line;
    if (format === 'json') {
      line = JSON.stringify({ time, level: lvl, msg: safeMsg, ...safeFields });
    } else {
      const extras = Object.entries(safeFields)
        .map(([k, v]) => `${k}=${formatValue(v)}`)
        .join(' ');
      line = `${time} [${lvl.toUpperCase()}] ${safeMsg}${extras ? ` ${extras}` : ''}`;
    }
    write(line);
  }

  const logger = {
    debug: (f, m) => log('debug', f, m),
    info: (f, m) => log('info', f, m),
    warn: (f, m) => log('warn', f, m),
    error: (f, m) => log('error', f, m),
    fatal: (f, m) => log('fatal', f, m),
    child(bound) {
      const parent = this;
      const wrap = (fn) => (f, m) => (typeof f === 'string' ? fn({ ...bound }, f) : fn({ ...bound, ...f }, m));
      return {
        debug: wrap(parent.debug),
        info: wrap(parent.info),
        warn: wrap(parent.warn),
        error: wrap(parent.error),
        fatal: wrap(parent.fatal),
        child: (more) => parent.child({ ...bound, ...more }),
      };
    },
  };
  return logger;
}

export const silentLogger = createLogger({ level: 'silent' });
