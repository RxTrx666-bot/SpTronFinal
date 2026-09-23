// Alert message formatting (Telegram HTML) and the notification outbox
// dispatcher.
//
// Funding code never talks to Telegram directly: it only inserts rows into the
// `notifications` outbox. The dispatcher delivers them independently, so a
// Telegram outage can never cause (or block) a blockchain transaction, and a
// transaction is never repeated because a notification failed.

import { scrub, scrubError } from '../security/redact.js';
import { sunToTrx, formatUnits } from '../blockchain/units.js';

export const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function txLine(network, txid) {
  if (!txid) return '—';
  return `<a href="${esc(network.tronscanTxUrl + txid)}">${esc(txid)}</a>`;
}

export function formatFundedMessage({ job, network, dryRun, usdtDecimals = 6 }) {
  const trx = BigInt(job.trx_sent_sun ?? 0);
  const usdt = BigInt(job.usdt_sent_units ?? 0);
  const lines = [
    dryRun ? '🧪 <b>DRY RUN — WALLET WOULD BE FUNDED</b>' : '🚨 <b>WALLET FUNDED</b>',
    '',
    '<b>Network:</b>',
    esc(network.label),
    '',
    '<b>Wallet:</b>',
    `<code>${esc(job.address)}</code>`,
    '',
    '<b>TRX funded:</b>',
    trx > 0n ? `${sunToTrx(trx)} TRX` : '—',
    '',
    '<b>USDT funded:</b>',
    usdt > 0n ? `${formatUnits(usdt, usdtDecimals)} USDT` : '—',
  ];
  if (!dryRun) {
    lines.push('', '<b>TRX Transaction:</b>', txLine(network, job.trx_txid), '', '<b>USDT Transaction:</b>', txLine(network, job.usdt_txid));
  }
  lines.push('', '<b>Status:</b>', dryRun ? '🧪 SIMULATED (nothing broadcast)' : '✅ SUCCESS');
  return lines.join('\n');
}

export function formatFailedMessage({ job, network, reason, code, willRetry, dryRun }) {
  const lines = [
    willRetry ? '⚠️ <b>FUNDING RETRYING</b>' : `❌ <b>FUNDING FAILED</b>${dryRun ? ' (DRY RUN)' : ''}`,
    '',
    '<b>Network:</b>',
    esc(network.label),
    '',
    '<b>Wallet:</b>',
    `<code>${esc(job.address)}</code>`,
    '',
    '<b>Reason:</b>',
    esc(scrub(reason)),
  ];
  if (code) lines.push('', `<b>Code:</b> <code>${esc(code)}</code>`);
  if (job.trx_txid) lines.push('', '<b>TRX Transaction:</b>', txLine(network, job.trx_txid));
  if (job.usdt_txid) lines.push('', '<b>USDT Transaction:</b>', txLine(network, job.usdt_txid));
  lines.push('', `<b>Job:</b> <code>${esc(job.id)}</code>`);
  return lines.join('\n');
}

export function formatSystemAlert(title, body) {
  return `⚠️ <b>${esc(title)}</b>\n\n${esc(scrub(body))}`;
}

/** Delivers queued notifications. Failures only affect notification rows. */
export class NotificationDispatcher {
  constructor({ repo, telegram, logger, intervalMs = 3000, maxAttempts = 12 }) {
    this.repo = repo;
    this.telegram = telegram;
    this.logger = logger;
    this.intervalMs = intervalMs;
    this.maxAttempts = maxAttempts;
    this.timer = null;
    this.running = false;
  }

  start() {
    if (this.timer) return;
    this.timer = setInterval(() => this.tick().catch(() => {}), this.intervalMs);
    this.timer.unref?.();
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
  }

  async tick() {
    if (this.running) return 0;
    this.running = true;
    let sent = 0;
    try {
      const due = await this.repo.dueNotifications(10);
      for (const n of due) {
        const attempts = Number(n.attempts) + 1;
        if (!this.telegram) {
          // Telegram disabled: record that the alert was not delivered, quietly.
          await this.repo.markNotification(n.id, { status: 'SKIPPED', attempts, last_error: 'Telegram not configured' });
          continue;
        }
        try {
          await this.telegram.broadcast(n.text);
          await this.repo.markNotification(n.id, { status: 'SENT', attempts, sent_at: this.repo.now(), last_error: null });
          sent++;
        } catch (err) {
          const safe = scrubError(err).message;
          const giveUp = attempts >= this.maxAttempts;
          await this.repo.markNotification(n.id, {
            status: giveUp ? 'FAILED' : 'PENDING',
            attempts,
            last_error: safe,
            next_attempt_at: this.repo.now() + Math.min(15 * 60_000, 5000 * 2 ** (attempts - 1)),
          });
          this.logger?.warn({ notification: n.id, attempts, error: safe }, 'Telegram notification failed (funding state unaffected)');
        }
      }
    } catch (err) {
      this.logger?.error({ error: scrubError(err).message }, 'Notification dispatcher error');
    } finally {
      this.running = false;
    }
    return sent;
  }
}
