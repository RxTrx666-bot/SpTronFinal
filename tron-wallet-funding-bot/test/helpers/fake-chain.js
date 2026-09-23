// In-memory TRON chain implementing the provider interface. Used by tests so
// nothing ever touches mainnet or real funds.
//
// It verifies signatures and raw_data_hex exactly like a node would, executes
// TRX and USDT transfers, enforces expiration, models solidification lag, and
// supports failure injection for broadcast / RPC calls.

import { createHash } from 'node:crypto';
import { utils } from 'tronweb';
import { RpcError } from '../../src/blockchain/errors.js';
import { TRANSFER_EVENT_TOPIC } from '../../src/blockchain/tx-builder.js';

const lc = (s) => String(s ?? '').toLowerCase().replace(/^0x/, '');
const hex = (b58) => utils.address.toHex(b58).toLowerCase();
const abiString = (s) => {
  const b = Buffer.from(s, 'utf8');
  return (32n).toString(16).padStart(64, '0') + BigInt(b.length).toString(16).padStart(64, '0') + b.toString('hex').padEnd(64, '0');
};

export const MAINNET_GENESIS = '00000000000000001ebf88508a03865c71d452e25f4d51194196a1d22b6653dc';

export class FakeChain {
  constructor({ usdtContract, genesis = MAINNET_GENESIS, solidLag = 2, usdtSymbol = 'USDT', usdtDecimals = 6 } = {}) {
    this.genesis = genesis;
    this.usdtContract = usdtContract;
    this.usdtHex = usdtContract ? hex(usdtContract) : null;
    this.usdtSymbol = usdtSymbol;
    this.usdtDecimals = usdtDecimals;
    this.solidLag = solidLag;
    this.trx = new Map(); // hex -> bigint
    this.usdt = new Map(); // hex -> bigint
    this.contracts = new Set(usdtContract ? [this.usdtHex] : []);
    this.blocks = [{ number: 1000, timestamp: Date.now(), id: this.#blockId(1000) }];
    this.mempool = new Map(); // txid -> tx
    this.included = new Map(); // txid -> { tx, blockNumber, info }
    this.broadcasts = []; // every broadcast call (txid)
    this.fail = {}; // failure injection
    this.calls = 0;
  }

  #blockId(n) {
    return BigInt(n).toString(16).padStart(16, '0') + createHash('sha256').update(`block${n}`).digest('hex').slice(16);
  }

  get head() {
    return this.blocks[this.blocks.length - 1];
  }

  get solidHead() {
    return this.blocks[Math.max(0, this.blocks.length - 1 - this.solidLag)];
  }

  setTrx(b58, sun) {
    this.trx.set(hex(b58), BigInt(sun));
  }
  setUsdt(b58, units) {
    this.usdt.set(hex(b58), BigInt(units));
  }
  trxOf(b58) {
    return this.trx.get(hex(b58)) ?? 0n;
  }
  usdtOf(b58) {
    return this.usdt.get(hex(b58)) ?? 0n;
  }
  addContract(b58) {
    this.contracts.add(hex(b58));
  }

  /** Produce `n` blocks (3s each), including mempool transactions. */
  mine(n = 1) {
    for (let i = 0; i < n; i++) {
      const number = this.head.number + 1;
      const block = { number, timestamp: this.head.timestamp + 3000, id: this.#blockId(number) };
      this.blocks.push(block);
      for (const [txid, tx] of this.mempool) {
        this.mempool.delete(txid);
        if (tx.raw_data.expiration <= block.timestamp) continue; // expired, dropped
        this.#execute(txid, tx, block);
      }
    }
  }

  /** Advance chain time without including anything (network congestion). */
  stall(n = 1) {
    const saved = this.mempool;
    this.mempool = new Map();
    this.mine(n);
    this.mempool = saved;
  }

  #execute(txid, tx, block) {
    const c = tx.raw_data.contract[0];
    const v = c.parameter.value;
    const owner = lc(v.owner_address);
    const info = { id: txid, blockNumber: block.number, blockTimeStamp: block.timestamp, fee: 0, receipt: {} };
    let ret = 'SUCCESS';
    if (c.type === 'TransferContract') {
      const amount = BigInt(v.amount);
      const to = lc(v.to_address);
      const bal = this.trx.get(owner) ?? 0n;
      const fee = this.trx.has(to) ? 0n : 1_100_000n; // account activation
      if (bal < amount + fee) {
        ret = 'REVERT';
        info.result = 'FAILED';
      } else {
        this.trx.set(owner, bal - amount - fee);
        this.trx.set(to, (this.trx.get(to) ?? 0n) + amount);
        info.fee = Number(fee);
      }
    } else {
      const data = lc(v.data);
      const to = `41${data.slice(8 + 24, 8 + 64)}`;
      const amount = BigInt(`0x${data.slice(8 + 64)}`);
      const fee = 13_000_000n;
      const trxBal = this.trx.get(owner) ?? 0n;
      const bal = this.usdt.get(owner) ?? 0n;
      this.trx.set(owner, trxBal - (fee > trxBal ? trxBal : fee));
      info.fee = Number(fee);
      if (this.fail.usdtRevert || bal < amount) {
        info.result = 'FAILED';
        info.receipt.result = 'REVERT';
        ret = 'REVERT';
      } else {
        this.usdt.set(owner, bal - amount);
        this.usdt.set(to, (this.usdt.get(to) ?? 0n) + amount);
        info.receipt.result = 'SUCCESS';
        info.log = [
          {
            address: this.usdtHex.slice(2),
            topics: [TRANSFER_EVENT_TOPIC, owner.slice(2).padStart(64, '0'), to.slice(2).padStart(64, '0')],
            data: amount.toString(16).padStart(64, '0'),
          },
        ];
      }
    }
    this.included.set(txid, { tx: { ...tx, ret: [{ contractRet: ret }] }, blockNumber: block.number, info });
  }

  #maybeFail(method) {
    this.calls++;
    const f = this.fail[method];
    if (f === 'timeout') throw new RpcError('timeout', `RPC timeout on ${method}`);
    if (f === 'ratelimit') throw new RpcError('rate_limit', `RPC rate limited on ${method}`, { status: 429 });
  }

  // ------------------------------------------------------ provider interface --
  async getNowBlock() {
    this.#maybeFail('getNowBlock');
    return { ...this.head };
  }
  async getSolidNowBlock() {
    this.#maybeFail('getSolidNowBlock');
    return { ...this.solidHead };
  }
  async getGenesisBlockId() {
    return this.genesis;
  }
  async getAccount(b58) {
    this.#maybeFail('getAccount');
    const h = hex(b58);
    return { exists: this.trx.has(h), balanceSun: this.trx.get(h) ?? 0n };
  }
  async isContract(b58) {
    return this.contracts.has(hex(b58));
  }
  async triggerConstant({ owner, contract, selector, parameter }) {
    this.#maybeFail('triggerConstant');
    if (hex(contract) !== this.usdtHex) return { ok: false, results: [], energyUsed: 0, message: 'no contract' };
    switch (selector) {
      case 'decimals()':
        return { ok: true, results: [BigInt(this.usdtDecimals).toString(16).padStart(64, '0')], energyUsed: 0 };
      case 'symbol()':
        return { ok: true, results: [abiString(this.usdtSymbol)], energyUsed: 0 };
      case 'name()':
        return { ok: true, results: [abiString('Tether USD')], energyUsed: 0 };
      case 'balanceOf(address)': {
        const who = `41${lc(parameter).slice(24, 64)}`;
        return { ok: true, results: [(this.usdt.get(who) ?? 0n).toString(16).padStart(64, '0')], energyUsed: 0 };
      }
      case 'transfer(address,uint256)': {
        const amount = BigInt(`0x${lc(parameter).slice(64)}`);
        const bal = this.usdt.get(hex(owner)) ?? 0n;
        if (bal < amount) return { ok: false, results: [], energyUsed: 0, message: 'REVERT opcode executed' };
        return { ok: true, results: [''.padStart(64, '0')], energyUsed: this.fail.highEnergy ? 10_000_000 : 64_285 };
      }
      default:
        return { ok: false, results: [], energyUsed: 0, message: 'unknown selector' };
    }
  }
  async getEnergyFeeSun() {
    return 100n;
  }

  async broadcast(signedTx) {
    const txid = lc(signedTx.txID);
    this.broadcasts.push(txid);
    const mode = typeof this.fail.broadcast === 'function' ? this.fail.broadcast(txid, this.broadcasts.length) : this.fail.broadcast;
    if (mode === 'timeout-lost') throw new RpcError('timeout', 'RPC timeout on /wallet/broadcasttransaction');
    // Node-side validation, as java-tron does.
    const idOk = createHash('sha256').update(Buffer.from(signedTx.raw_data_hex, 'hex')).digest('hex') === txid;
    if (!idOk || !utils.transaction.txCheck(signedTx)) return { accepted: false, duplicate: false, code: 'SIGNATURE_ERROR', message: 'bad tx' };
    const owner = lc(signedTx.raw_data.contract[0].parameter.value.owner_address);
    const rec = lc(utils.crypto.ecRecover(txid, signedTx.signature[0]));
    if (rec !== owner) return { accepted: false, duplicate: false, code: 'SIGNATURE_ERROR', message: 'signature mismatch' };
    if (signedTx.raw_data.expiration <= this.head.timestamp) return { accepted: false, duplicate: false, code: 'TRANSACTION_EXPIRATION_ERROR', message: 'expired' };
    if (typeof mode === 'string' && mode.startsWith('reject:')) return { accepted: false, duplicate: false, code: mode.slice(7), message: 'rejected' };
    if (this.included.has(txid) || this.mempool.has(txid)) return { accepted: false, duplicate: true, code: 'DUP_TRANSACTION_ERROR', message: 'dup' };
    this.mempool.set(txid, signedTx);
    if (mode === 'timeout-accepted') throw new RpcError('timeout', 'RPC timeout on /wallet/broadcasttransaction');
    return { accepted: true, duplicate: false, code: 'SUCCESS', message: '' };
  }

  async getTransaction(txid) {
    this.#maybeFail('getTransaction');
    const inc = this.included.get(lc(txid));
    if (inc) return inc.tx;
    const m = this.mempool.get(lc(txid));
    return m ? { ...m } : null;
  }

  async getTransactionInfo(txid, { solid = true } = {}) {
    this.#maybeFail('getTransactionInfo');
    const inc = this.included.get(lc(txid));
    if (!inc) return null;
    if (solid && inc.blockNumber > this.solidHead.number) return null;
    return inc.info;
  }

  /** Number of distinct transactions that actually executed successfully. */
  executedTransfers() {
    return [...this.included.values()].filter((i) => i.tx.ret[0].contractRet === 'SUCCESS');
  }
}
