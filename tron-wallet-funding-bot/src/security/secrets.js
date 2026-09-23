// Secret loading. A secret NAME can be supplied as:
//   NAME=<value>            (environment variable)
//   NAME_FILE=/path/to/file (Docker secrets, systemd LoadCredential=, Vault agent, ...)
// The file form is preferred in production: the value never appears in the
// process environment of child processes or in `docker inspect`.
//
// After reading, the variable is removed from process.env so that it cannot
// leak through crash reporters, child processes or accidental env dumps.

import { readFileSync, statSync } from 'node:fs';
import { registerSecret } from './redact.js';

export class SecretError extends Error {
  constructor(message) {
    super(message);
    this.name = 'SecretError';
  }
}

export function loadSecret(name, { required = false, env = process.env, scrubEnv = true } = {}) {
  const fileVar = `${name}_FILE`;
  let value;
  if (env[fileVar]) {
    const path = env[fileVar];
    try {
      const st = statSync(path);
      // Warn-level strictness: group/other readable secret files are refused.
      if (process.platform !== 'win32' && (st.mode & 0o077) !== 0 && !env.ALLOW_INSECURE_SECRET_FILE_PERMS) {
        throw new SecretError(`${fileVar} must not be readable by group/others (chmod 600)`);
      }
      value = readFileSync(path, 'utf8').trim();
    } catch (err) {
      if (err instanceof SecretError) throw err;
      throw new SecretError(`Unable to read ${fileVar} (${err.code ?? 'error'})`);
    }
  } else if (env[name] !== undefined && env[name] !== '') {
    value = String(env[name]).trim();
  }
  if (scrubEnv) {
    delete env[name];
  }
  if (!value) {
    if (required) throw new SecretError(`Missing required secret ${name} (or ${fileVar})`);
    return undefined;
  }
  registerSecret(value);
  return value;
}
