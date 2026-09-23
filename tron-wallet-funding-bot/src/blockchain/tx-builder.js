// Local TRON transaction construction and verification.
//
// Transactions are built entirely on this machine from a recent block reference
// (TAPOS). The RPC provider is never asked to build a transaction for us, so a
// malicious or buggy provider cannot alter recipient, amount or contract.
//
// After building, the protobuf bytes (raw_data_hex) are decoded again with an
// independent decoder and every field is compared against the intent before the
// transaction may be signed.

import { createHash } from 'node:crypto';
import { utils } from 'tronweb';
import { toSafeNumber } from './units.js';

export const TRANSFER_SELECTOR = 'a9059cbb'; // transfer(address,uint256)
export const TRANSFER_EVENT_TOPIC = 'ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';

export class TxBuildError extends Error {
  constructor(message) {
    super(message);
    this.name = 'TxBuildError';
    this.code = 'TX_VERIFICATION_FAILED';
  }
}

const lc = (s) => String(s ?? '').toLowerCase().replace(/^0x/, '');

function refFromBlock(block) {
  if (!block?.id || !/^[0-9a-f]{64}$/i.test(block.id)) throw new TxBuildError('Invalid reference block');
  return {
    ref_block_bytes: (block.number & 0xffff).toString(16).padStart(4, '0'),
    ref_block_hash: lc(block.id).slice(16, 32),
  };
}

function finalize(rawData) {
  const tx = { visible: false, raw_data: rawData };
  const pb = utils.transaction.txJsonToPb(tx);
  tx.raw_data_hex = lc(utils.transaction.txPbToRawDataHex(pb));
  tx.txID = lc(utils.transaction.txPbToTxID(pb));
  return tx;
}

/** ABI-encode transfer(address,uint256) call data. */
export function encodeTrc20Transfer(toHex, amountUnits) {
  const addr = lc(toHex);
  if (!/^41[0-9a-f]{40}$/.test(addr)) throw new TxBuildError('Invalid recipient hex address');
  const amt = BigInt(amountUnits);
  if (amt <= 0n || amt >= 2n ** 256n) throw new TxBuildError('Invalid token amount');
  return TRANSFER_SELECTOR + addr.slice(2).padStart(64, '0') + amt.toString(16).padStart(64, '0');
}

export function buildTrxTransfer({ ownerHex, toHex, amountSun, refBlock, expirationMs, timestampMs = Date.now() }) {
  const raw = {
    contract: [
      {
        parameter: {
          value: { owner_address: lc(ownerHex), to_address: lc(toHex), amount: toSafeNumber(amountSun) },
          type_url: 'type.googleapis.com/protocol.TransferContract',
        },
        type: 'TransferContract',
      },
    ],
    ...refFromBlock(refBlock),
    expiration: expirationMs,
    timestamp: timestampMs,
  };
  return finalize(raw);
}

export function buildTrc20Transfer({ ownerHex, contractHex, toHex, amountUnits, feeLimitSun, refBlock, expirationMs, timestampMs = Date.now() }) {
  const raw = {
    contract: [
      {
        parameter: {
          value: {
            owner_address: lc(ownerHex),
            contract_address: lc(contractHex),
            data: encodeTrc20Transfer(toHex, amountUnits),
            call_value: 0,
          },
          type_url: 'type.googleapis.com/protocol.TriggerSmartContract',
        },
        type: 'TriggerSmartContract',
      },
    ],
    ...refFromBlock(refBlock),
    expiration: expirationMs,
    timestamp: timestampMs,
    fee_limit: toSafeNumber(feeLimitSun),
  };
  return finalize(raw);
}

/**
 * Verify a transaction against the intended transfer, using the serialized
 * bytes (what is actually signed and executed), not the JSON.
 *
 * intent = { kind: 'TRX', ownerHex, toHex, amount }
 *        | { kind: 'USDT', ownerHex, contractHex, toHex, amount, feeLimitSun }
 */
export function verifyTransaction(tx, intent, { nowMs = Date.now(), maxExpirationMs = 24 * 3600 * 1000 } = {}) {
  const fail = (m) => {
    throw new TxBuildError(`Transaction verification failed: ${m}`);
  };
  if (!tx || typeof tx.raw_data_hex !== 'string' || typeof tx.txID !== 'string') fail('missing fields');
  const idFromBytes = createHash('sha256').update(Buffer.from(tx.raw_data_hex, 'hex')).digest('hex');
  if (idFromBytes !== lc(tx.txID)) fail('txID mismatch');
  if (!utils.transaction.txCheck(tx)) fail('raw_data does not match raw_data_hex');

  const type = intent.kind === 'TRX' ? 'TransferContract' : 'TriggerSmartContract';
  let decoded;
  try {
    decoded = utils.deserializeTx.deserializeTransaction(type, tx.raw_data_hex);
  } catch {
    fail('unable to decode raw_data_hex');
  }
  if (!Array.isArray(decoded.contract) || decoded.contract.length !== 1) fail('expected exactly one contract');
  const c = decoded.contract[0];
  if (c.type !== type) fail('unexpected contract type');
  if (Number(c.Permission_id ?? 0) !== 0) fail('unexpected permission id');
  const v = c.parameter?.value ?? {};
  if (lc(v.owner_address) !== lc(intent.ownerHex)) fail('owner mismatch');

  if (intent.kind === 'TRX') {
    if (lc(v.to_address) !== lc(intent.toHex)) fail('recipient mismatch');
    if (BigInt(v.amount) !== BigInt(intent.amount)) fail('amount mismatch');
    if (Number(decoded.fee_limit ?? 0) !== 0) fail('unexpected fee_limit');
  } else {
    if (lc(v.contract_address) !== lc(intent.contractHex)) fail('token contract mismatch');
    if (Number(v.call_value ?? 0) !== 0 || Number(v.call_token_value ?? 0) !== 0 || Number(v.token_id ?? 0) !== 0) {
      fail('unexpected TRX/TRC10 value attached to contract call');
    }
    if (lc(v.data) !== encodeTrc20Transfer(intent.toHex, intent.amount)) fail('call data mismatch');
    if (BigInt(decoded.fee_limit ?? 0) !== BigInt(intent.feeLimitSun)) fail('fee_limit mismatch');
  }
  if (lc(decoded.data ?? '') !== '') fail('unexpected memo data');
  const exp = Number(decoded.expiration);
  if (!(exp > nowMs - 60_000) || exp > nowMs + maxExpirationMs) fail('expiration out of range');
  return { txID: lc(tx.txID), expirationMs: exp };
}

/** Parse a USDT Transfer event from a transaction info log entry. */
export function findTransferLog(info, { contractHex, fromHex, toHex }) {
  const logs = Array.isArray(info?.log) ? info.log : [];
  const pad = (h) => lc(h).replace(/^41/, '').padStart(64, '0');
  const contract20 = lc(contractHex).replace(/^41/, '');
  for (const l of logs) {
    const topics = (l.topics ?? []).map(lc);
    if (lc(l.address).replace(/^41/, '') !== contract20) continue;
    if (topics[0] !== TRANSFER_EVENT_TOPIC) continue;
    if (topics[1] !== pad(fromHex) || topics[2] !== pad(toHex)) continue;
    return { amount: BigInt(`0x${lc(l.data) || '0'}`) };
  }
  return null;
}
