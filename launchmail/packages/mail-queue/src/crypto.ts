import crypto from "node:crypto";

const ALGORITHM = "aes-256-gcm";
const IV_LENGTH = 16;
const AUTH_TAG_LENGTH = 16;

// The encryption key is the single secret behind both AES-GCM at-rest
// encryption (SMTP passwords, DKIM keys) and HMAC signing of public links
// (tracking ids, unsubscribe tokens, click destinations). It must be set
// explicitly and never change: rotating it makes existing ciphertext
// undecryptable and invalidates every signed link already in the wild.
function getKey(): Buffer {
  const raw = process.env.MAIL_ENCRYPTION_KEY;
  if (!raw) {
    throw new Error(
      "MAIL_ENCRYPTION_KEY environment variable is required (set once, never change)",
    );
  }
  return crypto.createHash("sha256").update(raw).digest();
}

export function encrypt(plaintext: string): string {
  const key = getKey();
  const iv = crypto.randomBytes(IV_LENGTH);
  const cipher = crypto.createCipheriv(ALGORITHM, key, iv);
  const encrypted = Buffer.concat([
    cipher.update(plaintext, "utf8"),
    cipher.final(),
  ]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, encrypted]).toString("base64");
}

export function decrypt(encoded: string): string {
  const key = getKey();
  const buf = Buffer.from(encoded, "base64");
  const iv = buf.subarray(0, IV_LENGTH);
  const tag = buf.subarray(IV_LENGTH, IV_LENGTH + AUTH_TAG_LENGTH);
  const encrypted = buf.subarray(IV_LENGTH + AUTH_TAG_LENGTH);
  const decipher = crypto.createDecipheriv(ALGORITHM, key, iv);
  decipher.setAuthTag(tag);
  return decipher.update(encrypted) + decipher.final("utf8");
}

// --- HMAC signing for public links ---------------------------------------
// Signed tokens stop attackers from forging tracking ids, unsubscribe links,
// or click destinations. The signature is HMAC-SHA256 over the value, keyed
// by the same encryption secret, hex-encoded.

const SIG_PREFIX_LENGTH = 16;

export function sign(value: string): string {
  return crypto.createHmac("sha256", getKey()).update(value).digest("hex");
}

export function verify(value: string, sig: string): boolean {
  const expected = sign(value);
  // Length must match before timingSafeEqual, which throws on mismatched
  // buffer lengths.
  if (expected.length !== sig.length) return false;
  try {
    return crypto.timingSafeEqual(
      Buffer.from(expected, "hex"),
      Buffer.from(sig, "hex"),
    );
  } catch {
    return false;
  }
}

// Produce a compact "<id>.<sigPrefix>" token for embedding in URLs.
export function signId(id: string): string {
  return `${id}.${sign(id).slice(0, SIG_PREFIX_LENGTH)}`;
}

// Validate a "<id>.<sigPrefix>" token; return the id when the signature is
// valid, otherwise null.
export function parseSignedId(token: string): string | null {
  const idx = token.lastIndexOf(".");
  if (idx <= 0 || idx === token.length - 1) return null;
  const id = token.slice(0, idx);
  const sig = token.slice(idx + 1);
  const expected = sign(id).slice(0, SIG_PREFIX_LENGTH);
  if (expected.length !== sig.length) return null;
  try {
    return crypto.timingSafeEqual(
      Buffer.from(expected, "utf8"),
      Buffer.from(sig, "utf8"),
    )
      ? id
      : null;
  } catch {
    return null;
  }
}
