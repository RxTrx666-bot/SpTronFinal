// USDT TRC-20 contract access and verification.

import { FundingError } from './errors.js';
import { encodeTrc20Transfer } from './tx-builder.js';
import { toHexAddress } from '../wallet/validation.js';

function decodeUint(hex) {
  if (!hex || !/^[0-9a-f]+$/i.test(hex)) throw new Error('bad uint');
  return BigInt(`0x${hex.slice(0, 64)}`);
}

export function decodeAbiString(hex) {
  if (!hex) return '';
  const h = hex.replace(/^0x/, '');
  if (h.length === 64) {
    // bytes32-style return value
    return Buffer.from(h, 'hex').toString('utf8').replace(/\0+$/, '');
  }
  const offset = Number(BigInt(`0x${h.slice(0, 64)}`)) * 2;
  const len = Number(BigInt(`0x${h.slice(offset, offset + 64)}`)) * 2;
  return Buffer.from(h.slice(offset + 64, offset + 64 + len), 'hex').toString('utf8');
}

export class UsdtContract {
  constructor({ provider, contract, callerAddress, decimals = 6 }) {
    this.provider = provider;
    this.contract = contract; // base58
    this.contractHex = toHexAddress(contract);
    this.callerAddress = callerAddress; // used as owner_address for constant calls
    this.decimals = decimals;
    this.verified = null;
  }

  async #call(selector, parameter = '') {
    const r = await this.provider.triggerConstant({ owner: this.callerAddress, contract: this.contract, selector, parameter });
    if (!r.ok || !r.results[0]) {
      throw new FundingError('USDT_CALL_FAILED', `USDT ${selector} call failed: ${r.message || 'no result'}`, { transient: true });
    }
    return r.results[0];
  }

  /**
   * Verify that USDT_CONTRACT_ADDRESS is the intended USDT TRC-20 contract.
   * Throws FundingError('USDT_CONTRACT_INVALID') on any mismatch.
   */
  async verify({ network, expectedSymbol = 'USDT', allowNonstandard = false, logger } = {}) {
    const bad = (m) => new FundingError('USDT_CONTRACT_INVALID', m);
    if (network?.referenceUsdtContract && this.contract !== network.referenceUsdtContract) {
      if (!allowNonstandard) {
        throw bad(`USDT_CONTRACT_ADDRESS differs from the official Tether USDT contract for ${network.name}. Refusing to start. If this is intentional, set USDT_ALLOW_NONSTANDARD_CONTRACT=true.`);
      }
      logger?.warn({ contract: this.contract }, 'USDT contract is not the official reference contract (USDT_ALLOW_NONSTANDARD_CONTRACT=true)');
    }
    if (!(await this.provider.isContract(this.contract))) throw bad('USDT_CONTRACT_ADDRESS is not a deployed smart contract');
    const decimals = Number(decodeUint(await this.#call('decimals()')));
    if (decimals !== this.decimals) throw bad(`USDT contract reports ${decimals} decimals, expected ${this.decimals}`);
    const symbol = decodeAbiString(await this.#call('symbol()'));
    if (symbol !== expectedSymbol) throw bad(`USDT contract symbol is "${symbol.slice(0, 20)}", expected "${expectedSymbol}"`);
    const name = decodeAbiString(await this.#call('name()'));
    this.verified = { name, symbol, decimals, verifiedAt: new Date().toISOString() };
    return this.verified;
  }

  async balanceOf(address) {
    const param = toHexAddress(address).slice(2).padStart(64, '0');
    return decodeUint(await this.#call('balanceOf(address)', param));
  }

  /** Simulate transfer() from the caller and return the energy estimate. */
  async estimateTransfer(toAddress, amountUnits) {
    const data = encodeTrc20Transfer(toHexAddress(toAddress), amountUnits);
    const r = await this.provider.triggerConstant({
      owner: this.callerAddress,
      contract: this.contract,
      selector: 'transfer(address,uint256)',
      parameter: data.slice(8),
    });
    return { ok: r.ok, energyUsed: r.energyUsed, message: r.message };
  }
}
