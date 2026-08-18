import { describe, expect, it } from "vitest";

import { issueMemberToken, verifyMemberToken } from "../src/security.js";

describe("member token security", () => {
  it("issues the documented token shape and verifies only its secret", () => {
    const issued = issueMemberToken();

    expect(issued.token).toMatch(/^cap_v1_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}$/);
    expect(issued.digest).toHaveLength(32);
    expect(verifyMemberToken(issued.secret, issued.digest)).toBe(true);
    expect(verifyMemberToken("wrong", issued.digest)).toBe(false);
    expect(verifyMemberToken(issued.secret, Buffer.alloc(31))).toBe(false);
  });
});
