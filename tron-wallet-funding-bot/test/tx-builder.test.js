import { test } from 'node:test';
import assert from 'node:assert/strict';
import { utils } from 'tronweb';
import { buildTrxTransfer, buildTrc20Transfer, verifyTransaction, encodeTrc20Transfer } from '../src/blockchain/tx-builder.js';
import { MotherSigner } from '../src/security/signer.js';
import { toHexAddress } from '../src/wallet/validation.js';
import { newAccount, USDT_MAINNET } from './helpers/setup.js';

const refBlock = { number: 71234567, id: '00000000043f00a7' + 'ab'.repeat(24), timestamp: Date.now() };

function setup() {
  const mother = newAccount();
  const target = newAccount();
  return { mother, target, signer: new MotherSigner(mother.privateKey), ownerHex: toHexAddress(mother.address), toHex: toHexAddress(target.address) };
}

test('builds, verifies and signs a TRX transfer locally', () => {
  const { signer, ownerHex, toHex } = setup();
  const tx = buildTrxTransfer({ ownerHex, toHex, amountSun: 5_000_000n, refBlock, expirationMs: refBlock.timestamp + 60_000 });
  verifyTransaction(tx, { kind: 'TRX', ownerHex, toHex, amount: 5_000_000n }, { nowMs: refBlock.timestamp });
  const signed = signer.sign(tx);
  assert.equal(signed.signature.length, 1);
  assert.equal(utils.crypto.ecRecover(signed.txID, signed.signature[0]).toLowerCase(), ownerHex);
  assert.equal(tx.raw_data.ref_block_bytes, (refBlock.number & 0xffff).toString(16).padStart(4, '0'));
});

test('builds and verifies a USDT transfer with exact 6-decimal amount encoding', () => {
  const { ownerHex, toHex } = setup();
  const contractHex = toHexAddress(USDT_MAINNET);
  const intent = { kind: 'USDT', ownerHex, contractHex, toHex, amount: 10_000_000n, feeLimitSun: 30_000_000n };
  const tx = buildTrc20Transfer({ ...intent, amountUnits: 10_000_000n, refBlock, expirationMs: refBlock.timestamp + 60_000 });
  verifyTransaction(tx, intent, { nowMs: refBlock.timestamp });
  const data = tx.raw_data.contract[0].parameter.value.data;
  assert.equal(data.slice(0, 8), 'a9059cbb');
  assert.equal(BigInt(`0x${data.slice(72)}`), 10_000_000n); // 10 USDT = 10 * 10^6
  assert.equal(encodeTrc20Transfer(toHex, 1n).slice(-2), '01');
});

test('verification rejects any tampering (recipient, amount, contract, fee limit, txid)', () => {
  const { ownerHex, toHex } = setup();
  const other = toHexAddress(newAccount().address);
  const tx = buildTrxTransfer({ ownerHex, toHex, amountSun: 5_000_000n, refBlock, expirationMs: refBlock.timestamp + 60_000 });
  const opts = { nowMs: refBlock.timestamp };
  assert.throws(() => verifyTransaction(tx, { kind: 'TRX', ownerHex, toHex: other, amount: 5_000_000n }, opts), /recipient mismatch/);
  assert.throws(() => verifyTransaction(tx, { kind: 'TRX', ownerHex, toHex, amount: 6_000_000n }, opts), /amount mismatch/);
  // JSON says one thing, bytes say another
  const forged = structuredClone(tx);
  forged.raw_data.contract[0].parameter.value.to_address = other;
  assert.throws(() => verifyTransaction(forged, { kind: 'TRX', ownerHex, toHex: other, amount: 5_000_000n }, opts), /raw_data does not match|txID/);
  const badId = { ...tx, txID: 'ff'.repeat(32) };
  assert.throws(() => verifyTransaction(badId, { kind: 'TRX', ownerHex, toHex, amount: 5_000_000n }, opts), /txID mismatch/);

  const contractHex = toHexAddress(USDT_MAINNET);
  const intent = { kind: 'USDT', ownerHex, contractHex, toHex, amount: 10_000_000n, feeLimitSun: 30_000_000n };
  const utx = buildTrc20Transfer({ ...intent, amountUnits: 10_000_000n, refBlock, expirationMs: refBlock.timestamp + 60_000 });
  assert.throws(() => verifyTransaction(utx, { ...intent, contractHex: other }, opts), /token contract mismatch/);
  assert.throws(() => verifyTransaction(utx, { ...intent, feeLimitSun: 1n }, opts), /fee_limit mismatch/);
  assert.throws(() => verifyTransaction(utx, { ...intent, amount: 10n }, opts), /call data mismatch/);
});

test('signer refuses to sign when txID does not match raw bytes', () => {
  const { signer, ownerHex, toHex } = setup();
  const tx = buildTrxTransfer({ ownerHex, toHex, amountSun: 1n, refBlock, expirationMs: refBlock.timestamp + 60_000 });
  assert.throws(() => signer.sign({ ...tx, txID: '00'.repeat(32) }), /refusing to sign/);
});

test('amounts beyond safe integer range are refused', () => {
  const { ownerHex, toHex } = setup();
  assert.throws(() => buildTrxTransfer({ ownerHex, toHex, amountSun: 2n ** 60n, refBlock, expirationMs: refBlock.timestamp + 60_000 }));
});
