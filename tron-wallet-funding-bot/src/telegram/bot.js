// Telegram admin commands (long polling).
//
// Only messages from the configured TELEGRAM_CHAT_ID chat(s) are processed, and
// if TELEGRAM_ADMIN_USER_IDS is set, only from those Telegram users. Everything
// else is ignored. Commands never print private keys (the bot has no access to
// them), and all output passes through the scrubber.

import { esc } from './alerts.js';
import { scrubError } from '../security/redact.js';

export const COMMANDS = [
  ['help', 'Show commands'],
  ['status', 'Bot health'],
  ['balance', 'Mother Wallet TRX / USDT balance'],
  ['pending', 'Pending funding jobs'],
  ['completed', 'Recently completed jobs'],
  ['failed', 'Failed jobs'],
  ['job', '<id|address> Job details + transactions'],
  ['tx', '<id|address> Transaction IDs for a wallet'],
  ['retry', '<id|address> Retry a failed job'],
  ['pause', 'Pause funding'],
  ['resume', 'Resume funding'],
  ['set_trx', '<amount> Set TRX funding amount'],
  ['set_usdt', '<amount> Set USDT funding amount'],
  ['config', 'Show funding configuration'],
];

function jobLine(j) {
  const amounts = [j.trxAmount !== '0' ? `${j.trxAmount} TRX` : null, j.usdtAmount !== '0' ? `${j.usdtAmount} USDT` : null].filter(Boolean).join(' + ');
  return `• <code>${esc(j.address)}</code>\n  ${esc(j.status)} · ${esc(amounts)}${j.error ? ` · ${esc(j.error.code)}` : ''}\n  <code>${esc(j.id)}</code>`;
}

export class TelegramBot {
  constructor({ client, admin, config, logger }) {
    this.client = client;
    this.admin = admin;
    this.config = config;
    this.logger = logger;
    this.offset = 0;
    this.running = false;
  }

  isAuthorized(msg) {
    const chatOk = this.config.telegram.chatIds.includes(String(msg?.chat?.id));
    const users = this.config.telegram.adminUserIds;
    const userOk = users.length === 0 || users.includes(String(msg?.from?.id));
    return chatOk && userOk;
  }

  async start() {
    this.running = true;
    try {
      await this.client.setMyCommands(COMMANDS.map(([command, description]) => ({ command, description: description.slice(0, 256) })));
    } catch (err) {
      this.logger.warn({ error: scrubError(err).message }, 'Unable to register Telegram commands');
    }
    // Skip any backlog: Telegram re-delivers unconfirmed updates for 24h, and
    // old commands (e.g. /retry, /pause) must never be replayed after a restart.
    try {
      const backlog = await this.client.getUpdates(-1, 0);
      const last = (backlog ?? []).at(-1);
      if (last) this.offset = last.update_id + 1;
    } catch (err) {
      this.logger.warn({ error: scrubError(err).message }, 'Unable to skip Telegram backlog');
    }
    this.loop();
  }

  stop() {
    this.running = false;
  }

  async loop() {
    while (this.running) {
      try {
        const updates = await this.client.getUpdates(this.offset, 25);
        for (const u of updates ?? []) {
          this.offset = Math.max(this.offset, u.update_id + 1);
          if (u.message?.text) await this.handle(u.message);
        }
      } catch (err) {
        this.logger.warn({ error: scrubError(err).message }, 'Telegram polling error');
        await new Promise((r) => setTimeout(r, err.retryAfterMs ?? 5000));
      }
    }
  }

  async handle(msg) {
    if (!this.isAuthorized(msg)) {
      this.logger.warn({ chat: msg?.chat?.id, user: msg?.from?.id }, 'Ignored Telegram message from unauthorized chat/user');
      return;
    }
    const reply = await this.execute(msg.text, `tg:${msg.from?.id ?? 'unknown'}`);
    if (reply) {
      try {
        await this.client.sendMessage(msg.chat.id, reply);
      } catch (err) {
        this.logger.warn({ error: scrubError(err).message }, 'Telegram reply failed');
      }
    }
  }

  /** Execute a command string and return the HTML reply. Exposed for tests. */
  async execute(text, by) {
    const [rawCmd, ...args] = String(text).trim().split(/\s+/);
    if (!rawCmd.startsWith('/')) return null;
    const cmd = rawCmd.slice(1).split('@')[0].toLowerCase();
    try {
      switch (cmd) {
        case 'start':
        case 'help':
          return ['<b>TRON Funding Bot</b>', '', ...COMMANDS.map(([c, d]) => `/${c} — ${esc(d)}`)].join('\n');
        case 'status':
        case 'health': {
          const h = await this.admin.health({ deep: true });
          return [
            `<b>Health:</b> ${h.status === 'ok' ? '✅' : h.status === 'degraded' ? '⚠️' : '❌'} ${esc(h.status)}`,
            `<b>Network:</b> ${esc(h.network)}${h.dryRun ? ' (DRY RUN)' : ''}`,
            `<b>Mother:</b> <code>${esc(h.motherAddress)}</code>`,
            `<b>Paused:</b> ${h.paused ? 'yes' : 'no'}`,
            ...Object.entries(h.checks).map(([k, v]) => `<b>${esc(k)}:</b> ${esc(v)}`),
            `<b>Jobs:</b> ${esc(JSON.stringify(h.jobs ?? {}))}`,
            `<b>Uptime:</b> ${h.uptimeSeconds}s`,
          ].join('\n');
        }
        case 'balance': {
          const b = await this.admin.balances();
          return `<b>Mother Wallet</b>\n<code>${esc(b.address)}</code>\n\n<b>TRX:</b> ${esc(b.trx)}\n<b>USDT:</b> ${esc(b.usdt)}`;
        }
        case 'pending':
        case 'completed':
        case 'failed': {
          const jobs = await this.admin.jobs(cmd, 15);
          if (!jobs.length) return `No ${cmd} jobs.`;
          return [`<b>${esc(cmd.toUpperCase())} (${jobs.length})</b>`, '', ...jobs.map(jobLine)].join('\n');
        }
        case 'job':
        case 'tx': {
          if (!args[0]) return `Usage: /${cmd} &lt;job id | address&gt;`;
          const j = await this.admin.job(args[0]);
          if (!j) return 'Job not found.';
          const lines = [jobLine(j), ''];
          for (const t of j.transfers) {
            lines.push(`<b>${esc(t.asset)}</b> ${esc(t.amount)} · ${esc(t.status)}${t.txid ? `\n<a href="${esc(t.url)}">${esc(t.txid)}</a>` : ''}${t.error ? `\n${esc(t.error)}` : ''}`);
          }
          if (!j.transfers.length) lines.push('No transactions yet.');
          return lines.join('\n');
        }
        case 'retry': {
          if (!args[0]) return 'Usage: /retry &lt;job id | address&gt;';
          const j = await this.admin.retry(args[0], by);
          return `🔁 Retry scheduled\n${jobLine(j)}`;
        }
        case 'pause':
          await this.admin.pause(by);
          return '⏸ Funding <b>paused</b>. Already-broadcast transactions are still tracked.';
        case 'resume':
          await this.admin.resume(by);
          return '▶️ Funding <b>resumed</b>.';
        case 'set_trx':
        case 'set_usdt': {
          if (!args[0]) return `Usage: /${cmd} &lt;amount&gt;`;
          const s = await this.admin.setAmount(cmd === 'set_trx' ? 'TRX' : 'USDT', args[0], by);
          return `✅ Funding amounts updated (applies to new jobs)\n<b>TRX:</b> ${esc(s.trxAmount)}\n<b>USDT:</b> ${esc(s.usdtAmount)}`;
        }
        case 'config': {
          const s = this.admin.settings.snapshot();
          return [
            '<b>Funding configuration</b>',
            `<b>TRX per wallet:</b> ${esc(s.trxAmount)} (max ${esc(s.maxTrxPerWallet)})`,
            `<b>USDT per wallet:</b> ${esc(s.usdtAmount)} (max ${esc(s.maxUsdtPerWallet)})`,
            `<b>Paused:</b> ${s.paused ? `yes (${esc(s.pauseReason ?? '')})` : 'no'}`,
            `<b>Dry run:</b> ${s.dryRun ? 'yes' : 'no'}`,
            `<b>Repeat funding:</b> ${this.config.funding.allowRepeatFunding ? 'allowed' : 'disabled'}`,
          ].join('\n');
        }
        default:
          return 'Unknown command. /help';
      }
    } catch (err) {
      return `❌ ${esc(scrubError(err).message)}`;
    }
  }
}
