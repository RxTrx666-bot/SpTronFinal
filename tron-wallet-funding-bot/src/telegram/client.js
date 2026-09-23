// Minimal Telegram Bot API client (fetch-based, no dependencies).
// The bot token is part of the Bot API URL path, so URLs are never logged and
// every error message is scrubbed before surfacing.

import { scrub } from '../security/redact.js';
import { fetchWithTimeout } from '../util/mutex.js';

export class TelegramError extends Error {
  constructor(message, { retryAfterMs } = {}) {
    super(scrub(message));
    this.name = 'TelegramError';
    this.retryAfterMs = retryAfterMs;
  }
}

export class TelegramClient {
  #token;

  constructor({ token, chatIds, apiBaseUrl = 'https://api.telegram.org', fetchImpl = globalThis.fetch, timeoutMs = 15000 }) {
    this.#token = token;
    this.chatIds = chatIds;
    this.apiBaseUrl = apiBaseUrl;
    this.fetch = fetchImpl;
    this.timeoutMs = timeoutMs;
  }

  async call(method, body, { timeoutMs = this.timeoutMs } = {}) {
    let res;
    try {
      res = await fetchWithTimeout(
        this.fetch,
        `${this.apiBaseUrl}/bot${this.#token}/${method}`,
        { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) },
        timeoutMs,
      );
    } catch (err) {
      throw new TelegramError(`Telegram ${method} request failed (${err?.name === 'TimeoutError' ? 'timeout' : err?.cause?.code ?? 'network'})`);
    }
    let data = {};
    try {
      data = await res.json();
    } catch {
      /* ignore */
    }
    if (!res.ok || data.ok !== true) {
      const ra = data?.parameters?.retry_after;
      throw new TelegramError(`Telegram ${method} failed: HTTP ${res.status} ${data?.description ?? ''}`.trim(), {
        retryAfterMs: ra ? ra * 1000 : undefined,
      });
    }
    return data.result;
  }

  async sendMessage(chatId, text) {
    return this.call('sendMessage', {
      chat_id: chatId,
      text: scrub(text).slice(0, 4000),
      parse_mode: 'HTML',
      disable_web_page_preview: true,
    });
  }

  /** Send to every configured chat. Throws if any chat failed. */
  async broadcast(text) {
    const errors = [];
    for (const id of this.chatIds) {
      try {
        await this.sendMessage(id, text);
      } catch (err) {
        errors.push(err);
      }
    }
    if (errors.length === this.chatIds.length) throw errors[0];
    return { failed: errors.length };
  }

  async getUpdates(offset, timeoutSec = 25) {
    return this.call('getUpdates', { offset, timeout: timeoutSec, allowed_updates: ['message'] }, { timeoutMs: (timeoutSec + 10) * 1000 });
  }

  async setMyCommands(commands) {
    return this.call('setMyCommands', { commands });
  }

  toJSON() {
    return { chatIds: this.chatIds };
  }
}
