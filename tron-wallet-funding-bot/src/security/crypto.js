// AES-256-GCM field encryption. Only used when TARGET_PRIVATE_KEY_POLICY=encrypt
// (off by default). The encryption key comes from TARGET_KEY_ENCRYPTION_KEY
// (env or *_FILE / secret manager) and is never stored in the database.

import { createCipheriv, createDecipheriv, randomBytes, createHash } from 'node:crypto';

export class FieldCipher {
  #key;

  constructor(base64Key) {
    const key = Buffer.from(String(base64Key ?? ''), 'base64');
    if (key.length !== 32) throw new Error('Encryption key must be 32 bytes (base64)');
    this.#key = key;
    // Non-secret identifier so rotated keys can be told apart.
    this.keyId = createHash('sha256').update(key).digest('hex').slice(0, 16);
  }

  encrypt(plaintext, aad = '') {
    const iv = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', this.#key, iv);
    cipher.setAAD(Buffer.from(aad));
    const ct = Buffer.concat([cipher.update(String(plaintext), 'utf8'), cipher.final()]);
    return {
      ciphertext: ct.toString('base64'),
      iv: iv.toString('base64'),
      tag: cipher.getAuthTag().toString('base64'),
      keyId: this.keyId,
    };
  }

  decrypt({ ciphertext, iv, tag }, aad = '') {
    const decipher = createDecipheriv('aes-256-gcm', this.#key, Buffer.from(iv, 'base64'));
    decipher.setAAD(Buffer.from(aad));
    decipher.setAuthTag(Buffer.from(tag, 'base64'));
    return Buffer.concat([decipher.update(Buffer.from(ciphertext, 'base64')), decipher.final()]).toString('utf8');
  }

  toJSON() {
    return { keyId: this.keyId };
  }
}
