// Mother Wallet signer. The private key lives only inside this object's private
// field. It is never returned, serialised, logged or sent anywhere: signing is
// done locally with secp256k1 over the transaction ID.

import { createHash } from 'node:crypto';
import { inspect } from 'node:util';
import { utils } from 'tronweb';
import { addressFromPrivateKey, looksLikePrivateKey, toHexAddress } from '../wallet/validation.js';
import { registerSecret } from './redact.js';

export class SignerError extends Error {
  constructor(message) {
    super(message);
    this.name = 'SignerError';
  }
}

export class MotherSigner {
  #key;

  constructor(privateKey) {
    if (!looksLikePrivateKey(privateKey)) {
      throw new SignerError('MOTHER_PRIVATE_KEY is not a 64-hex-character private key');
    }
    const key = privateKey.trim().replace(/^0x/i, '').toLowerCase();
    registerSecret(key);
    const address = addressFromPrivateKey(key);
    if (!address) throw new SignerError('MOTHER_PRIVATE_KEY is not a valid secp256k1 private key');
    this.#key = key;
    this.address = address;
    this.hexAddress = toHexAddress(address);
    Object.freeze(this);
  }

  /**
   * Sign a transaction whose raw_data_hex has already been verified against the
   * intended transfer. Returns a new object; input is not mutated.
   */
  sign(tx) {
    const rawHex = String(tx.raw_data_hex ?? '');
    if (!/^[0-9a-f]+$/i.test(rawHex)) throw new SignerError('Transaction has no raw_data_hex');
    const expectedId = createHash('sha256').update(Buffer.from(rawHex, 'hex')).digest('hex');
    if (expectedId !== String(tx.txID).toLowerCase()) {
      throw new SignerError('txID does not match sha256(raw_data_hex); refusing to sign');
    }
    const copy = { ...tx, signature: undefined };
    delete copy.signature;
    const signed = utils.crypto.signTransaction(this.#key, copy);
    const recovered = utils.crypto.ecRecover(signed.txID, signed.signature[0]).toLowerCase();
    if (recovered !== this.hexAddress) throw new SignerError('Signature self-check failed');
    return signed;
  }

  toJSON() {
    return { address: this.address };
  }

  toString() {
    return `MotherSigner(${this.address})`;
  }

  [inspect.custom]() {
    return this.toString();
  }
}
