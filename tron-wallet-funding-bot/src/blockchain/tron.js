// TronService: chain-level operations used by the funding pipeline.
// Depends only on the provider interface (see provider.js).

import { FundingError, RpcError } from './errors.js';
import { buildTrxTransfer, buildTrc20Transfer, verifyTransaction, findTransferLog } from './tx-builder.js';
import { toHexAddress } from '../wallet/validation.js';

export class TronService {
  constructor({ provider, network, expectedGenesisBlockId, txConfig, logger }) {
    this.provider = provider;
    this.network = network;
    this.expectedGenesisBlockId = expectedGenesisBlockId;
    this.txConfig = txConfig;
    this.logger = logger;
    this.lastHealth = null;
  }

  /** Verify the node is the expected TRON network and is in sync. */
  async checkConnection({ maxLagSeconds = 120 } = {}) {
    const genesis = await this.provider.getGenesisBlockId();
    if (this.expectedGenesisBlockId && genesis.toLowerCase() !== this.expectedGenesisBlockId.toLowerCase()) {
      throw new FundingError(
        'WRONG_NETWORK',
        `RPC node genesis block ${genesis} does not match expected ${this.network.name} genesis ${this.expectedGenesisBlockId}. Refusing to operate.`,
      );
    }
    const head = await this.provider.getNowBlock();
    const lagSeconds = Math.round((Date.now() - head.timestamp) / 1000);
    const health = { ok: lagSeconds <= maxLagSeconds, headBlock: head.number, lagSeconds, checkedAt: new Date().toISOString() };
    this.lastHealth = health;
    if (!health.ok) throw new RpcError('node_error', `Node is out of sync (head block ${lagSeconds}s old)`);
    return health;
  }

  async getTrxBalance(address) {
    return (await this.provider.getAccount(address)).balanceSun;
  }

  async accountExists(address) {
    return (await this.provider.getAccount(address)).exists;
  }

  async isContract(address) {
    return this.provider.isContract(address);
  }

  async #refBlock() {
    const head = await this.provider.getNowBlock();
    return { refBlock: head, expirationMs: head.timestamp + this.txConfig.expirationSeconds * 1000 };
  }

  /** Build + verify (not sign) a TRX transfer. */
  async prepareTrxTransfer({ from, to, amountSun }) {
    const { refBlock, expirationMs } = await this.#refBlock();
    const intent = { kind: 'TRX', ownerHex: toHexAddress(from), toHex: toHexAddress(to), amount: amountSun };
    const tx = buildTrxTransfer({ ownerHex: intent.ownerHex, toHex: intent.toHex, amountSun, refBlock, expirationMs });
    const { expirationMs: exp } = verifyTransaction(tx, intent, { nowMs: refBlock.timestamp });
    return { tx, intent, expirationMs: exp };
  }

  /** Build + verify (not sign) a TRC-20 transfer. */
  async prepareTrc20Transfer({ from, contract, to, amountUnits, feeLimitSun }) {
    const { refBlock, expirationMs } = await this.#refBlock();
    const intent = {
      kind: 'USDT',
      ownerHex: toHexAddress(from),
      contractHex: toHexAddress(contract),
      toHex: toHexAddress(to),
      amount: amountUnits,
      feeLimitSun,
    };
    const tx = buildTrc20Transfer({ ...intent, amountUnits, refBlock, expirationMs });
    const { expirationMs: exp } = verifyTransaction(tx, intent, { nowMs: refBlock.timestamp });
    return { tx, intent, expirationMs: exp };
  }

  async broadcast(signedTx) {
    return this.provider.broadcast(signedTx);
  }

  /**
   * Determine the on-chain state of a transaction we signed.
   *
   * Returns one of:
   *   { state: 'CONFIRMED', blockNumber, feeSun }      solidified and successful
   *   { state: 'FAILED', reason, blockNumber, feeSun } solidified but execution failed
   *   { state: 'PENDING' }                             seen by the node, not solidified yet
   *   { state: 'NOT_FOUND' }                           unknown to the node
   */
  async lookupTransaction(txid, { kind, contractHex, fromHex, toHex, amount }) {
    const info = await this.provider.getTransactionInfo(txid, { solid: true });
    if (info && info.blockNumber !== undefined) {
      const feeSun = BigInt(info.fee ?? 0);
      const blockNumber = Number(info.blockNumber);
      if (info.result === 'FAILED') {
        return { state: 'FAILED', reason: info.receipt?.result || info.resMessage || 'FAILED', blockNumber, feeSun };
      }
      if (kind === 'USDT') {
        if (info.receipt?.result !== 'SUCCESS') {
          return { state: 'FAILED', reason: info.receipt?.result || 'CONTRACT_FAILED', blockNumber, feeSun };
        }
        const log = findTransferLog(info, { contractHex, fromHex, toHex });
        if (!log || log.amount !== BigInt(amount)) {
          return { state: 'FAILED', reason: 'TRANSFER_EVENT_MISMATCH', blockNumber, feeSun };
        }
      } else {
        // System contract: check execution result on the transaction itself.
        const tx = await this.provider.getTransaction(txid);
        const ret = tx?.ret?.[0]?.contractRet;
        if (ret && ret !== 'SUCCESS') return { state: 'FAILED', reason: ret, blockNumber, feeSun };
      }
      return { state: 'CONFIRMED', blockNumber, feeSun };
    }
    const unconfirmed = await this.provider.getTransaction(txid);
    if (unconfirmed) return { state: 'PENDING' };
    return { state: 'NOT_FOUND' };
  }

  /**
   * True only when the transaction can provably never be included any more:
   * the latest SOLIDIFIED block is past its expiration (+ safety margin), so no
   * current or future block can contain it. Uses chain time, not local time.
   */
  async isDefinitelyExpired(expirationMs) {
    const solid = await this.provider.getSolidNowBlock();
    return solid.timestamp > Number(expirationMs) + this.txConfig.expirySafetyMarginSeconds * 1000;
  }

  txUrl(txid) {
    return `${this.network.tronscanTxUrl}${txid}`;
  }
}
