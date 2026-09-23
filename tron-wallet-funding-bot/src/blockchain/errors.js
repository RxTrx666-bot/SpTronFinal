// Error types for the blockchain layer and the funding pipeline.

/** Error raised by the RPC transport. `transient` means "safe to retry a read". */
export class RpcError extends Error {
  constructor(kind, message, { status, transient = true, retryAfterMs } = {}) {
    super(message);
    this.name = 'RpcError';
    this.kind = kind; // timeout | network | rate_limit | http | node_error | bad_response
    this.status = status;
    this.transient = transient;
    this.retryAfterMs = retryAfterMs;
    this.code = `RPC_${kind.toUpperCase()}`;
  }
}

/**
 * A funding failure with a stable machine-readable code.
 * transient=true  -> the job is retried automatically with backoff
 * transient=false -> the job is marked FAILED and needs operator action
 */
export class FundingError extends Error {
  constructor(code, message, { transient = false, pause = false } = {}) {
    super(message);
    this.name = 'FundingError';
    this.code = code;
    this.transient = transient;
    this.pause = pause; // auto-pause funding (e.g. Mother Wallet empty)
  }
}

export function isTransient(err) {
  if (!err) return false;
  if (err instanceof FundingError) return err.transient;
  if (err instanceof RpcError) return err.transient;
  // Database busy / network style errors.
  return ['SQLITE_BUSY', 'ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'EAI_AGAIN'].includes(err.code);
}
