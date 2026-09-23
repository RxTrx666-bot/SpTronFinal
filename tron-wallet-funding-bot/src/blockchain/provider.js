// Provider abstraction over the TRON node HTTP API.
//
// The rest of the application depends only on the methods of this class (the
// "provider interface"), so switching from TronGrid to a self-hosted java-tron
// node, or another compatible provider, is a configuration change. Tests use an
// in-memory implementation with the same interface (test/helpers/fake-chain.js).
//
// Provider interface:
//   getNowBlock()                       -> { number, id, timestamp }
//   getSolidNowBlock()                  -> { number, id, timestamp }
//   getGenesisBlockId()                 -> string
//   getAccount(base58)                  -> { exists, balanceSun: bigint }
//   isContract(base58)                  -> boolean
//   triggerConstant({owner, contract, selector, parameter}) -> { ok, results: hex[], energyUsed, message }
//   getEnergyFeeSun()                   -> bigint (sun per energy unit)
//   broadcast(signedTx)                 -> { accepted, duplicate, code, message }
//   getTransaction(txid)                -> tx json | null     (full node, may be unconfirmed)
//   getTransactionInfo(txid, {solid})   -> info json | null

import { RpcError } from './errors.js';

function hexToUtf8(hex) {
  try {
    return Buffer.from(String(hex), 'hex').toString('utf8').replace(/[^\x20-\x7e]/g, '').slice(0, 300);
  } catch {
    return '';
  }
}

function normBlock(b) {
  const header = b?.block_header?.raw_data;
  if (!b?.blockID || !header) throw new RpcError('bad_response', 'Node returned an invalid block');
  return { number: Number(header.number ?? 0), id: b.blockID, timestamp: Number(header.timestamp) };
}

const isEmpty = (o) => !o || (typeof o === 'object' && Object.keys(o).length === 0);

export class TronHttpProvider {
  constructor(rpc) {
    this.rpc = rpc;
  }

  async getNowBlock() {
    return normBlock(await this.rpc.post('/wallet/getnowblock'));
  }

  async getSolidNowBlock() {
    return normBlock(await this.rpc.post('/walletsolidity/getnowblock', {}, { solidity: true }));
  }

  async getGenesisBlockId() {
    const b = await this.rpc.post('/wallet/getblockbynum', { num: 0 });
    if (!b?.blockID) throw new RpcError('bad_response', 'Node did not return block 0');
    return b.blockID;
  }

  async getAccount(address) {
    const acc = await this.rpc.post('/wallet/getaccount', { address, visible: true });
    if (isEmpty(acc)) return { exists: false, balanceSun: 0n };
    return { exists: true, balanceSun: BigInt(acc.balance ?? 0) };
  }

  async isContract(address) {
    const c = await this.rpc.post('/wallet/getcontract', { value: address, visible: true });
    return !isEmpty(c) && Boolean(c.bytecode || c.contract_address);
  }

  async triggerConstant({ owner, contract, selector, parameter = '' }) {
    const r = await this.rpc.post('/wallet/triggerconstantcontract', {
      owner_address: owner,
      contract_address: contract,
      function_selector: selector,
      parameter,
      visible: true,
    });
    const ok = r?.result?.result === true;
    return {
      ok,
      results: Array.isArray(r?.constant_result) ? r.constant_result : [],
      energyUsed: Number(r?.energy_used ?? 0),
      message: ok ? '' : hexToUtf8(r?.result?.message ?? '') || r?.result?.code || 'constant call failed',
    };
  }

  async getEnergyFeeSun() {
    const r = await this.rpc.post('/wallet/getchainparameters');
    const p = (r?.chainParameter ?? []).find((x) => x.key === 'getEnergyFee');
    if (!p?.value) throw new RpcError('bad_response', 'getEnergyFee chain parameter missing');
    return BigInt(p.value);
  }

  /** Single attempt, never retried by the transport. */
  async broadcast(signedTx) {
    const r = await this.rpc.post('/wallet/broadcasttransaction', signedTx, { retry: false });
    const code = r?.code ?? (r?.result ? 'SUCCESS' : 'UNKNOWN');
    const message = r?.message ? hexToUtf8(r.message) || String(r.message).slice(0, 300) : '';
    return {
      accepted: r?.result === true,
      duplicate: code === 'DUP_TRANSACTION_ERROR',
      code,
      message,
    };
  }

  async getTransaction(txid) {
    const r = await this.rpc.post('/wallet/gettransactionbyid', { value: txid });
    return isEmpty(r) ? null : r;
  }

  async getTransactionInfo(txid, { solid = true } = {}) {
    const r = solid
      ? await this.rpc.post('/walletsolidity/gettransactioninfobyid', { value: txid }, { solidity: true })
      : await this.rpc.post('/wallet/gettransactioninfobyid', { value: txid });
    return isEmpty(r) ? null : r;
  }
}
