// Tiny async mutex. Used to serialise the "check balance -> build -> sign ->
// persist -> broadcast" critical section so concurrent jobs can never spend the
// same Mother Wallet balance twice.

export class Mutex {
  #tail = Promise.resolve();
  #locked = false;

  get locked() {
    return this.#locked;
  }

  async run(fn) {
    const prev = this.#tail;
    let release;
    this.#tail = new Promise((r) => (release = r));
    await prev;
    this.#locked = true;
    try {
      return await fn();
    } finally {
      this.#locked = false;
      release();
    }
  }
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * fetch() with a hard timeout. Uses a regular (ref'd) timer so a hung request
 * always settles, and raises an error named 'TimeoutError'.
 */
export async function fetchWithTimeout(fetchImpl, url, init, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => {
    const err = new Error(`Request timed out after ${timeoutMs}ms`);
    err.name = 'TimeoutError';
    ctrl.abort(err);
  }, timeoutMs);
  try {
    return await Promise.race([
      fetchImpl(url, { ...init, signal: ctrl.signal }),
      new Promise((_, reject) => ctrl.signal.addEventListener('abort', () => reject(ctrl.signal.reason), { once: true })),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
