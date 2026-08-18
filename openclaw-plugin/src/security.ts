import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

export interface IssuedToken {
  token: string;
  lookupId: string;
  secret: string;
  digest: Buffer;
}

export function issueMemberToken(): IssuedToken {
  const lookupId = randomBytes(12).toString("base64url");
  const secret = randomBytes(32).toString("base64url");
  return {
    token: `cap_v1_${lookupId}.${secret}`,
    lookupId,
    secret,
    digest: createHash("sha256").update(secret, "utf8").digest(),
  };
}

export function verifyMemberToken(secret: string, digest: Buffer): boolean {
  const candidate = createHash("sha256").update(secret, "utf8").digest();
  return candidate.length === digest.length && timingSafeEqual(candidate, digest);
}
