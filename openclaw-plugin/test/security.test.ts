import type { IncomingMessage } from "node:http";

import { describe, expect, it, vi } from "vitest";

import {
  CaptainAuthenticator,
  LimitEventAggregator,
  PollLimiter,
  SecurityProblem,
  TokenBucketLimiter,
  issueMemberToken,
  resolveClientSource,
  verifyMemberToken,
} from "../src/security.js";
import type { StoredMemberAuth } from "../src/store.js";

function request(
  remoteAddress: string | undefined,
  headers: Record<string, string | string[] | undefined> = {},
): IncomingMessage {
  return {
    headers,
    socket: { remoteAddress },
  } as IncomingMessage;
}

function authorization(token: string): Record<string, string> {
  return { authorization: `Bearer ${token}` };
}

function differentBase64Url(value: string): string {
  return `${value[0] === "A" ? "B" : "A"}${value.slice(1)}`;
}

function expectProblem(action: () => unknown, status: number, retryAfterSeconds?: number): void {
  const error = captureProblem(action);
  expect(error).toMatchObject({ status });
  if (retryAfterSeconds !== undefined) {
    expect(error).toMatchObject({ retryAfterSeconds });
  }
}

function captureProblem(action: () => unknown): SecurityProblem {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(SecurityProblem);
    return error as SecurityProblem;
  }
  throw new Error("Expected a SecurityProblem.");
}

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

describe("TokenBucketLimiter", () => {
  it("limits an immediate burst and reports when one token is available", () => {
    const limiter = new TokenBucketLimiter({ ratePerMinute: 30, burst: 5 });

    for (let attempt = 0; attempt < 5; attempt += 1) {
      expect(limiter.consume("member-1", 0)).toEqual({ allowed: true });
    }
    expect(limiter.consume("member-1", 0)).toEqual({
      allowed: false,
      retryAfterSeconds: 2,
    });
    expect(limiter.consume("member-1", 1_000)).toEqual({
      allowed: false,
      retryAfterSeconds: 1,
    });
    expect(limiter.consume("member-1", 2_000)).toEqual({ allowed: true });
  });

  it("starts an idle key with a fresh burst after expiration", () => {
    const limiter = new TokenBucketLimiter({
      ratePerMinute: 30,
      burst: 2,
      idleTtlMs: 1_000,
    });

    expect(limiter.consume("member-1", 0).allowed).toBe(true);
    expect(limiter.consume("member-1", 0).allowed).toBe(true);
    expect(limiter.consume("member-1", 0).allowed).toBe(false);
    expect(limiter.consume("member-1", 1_001).allowed).toBe(true);
    expect(limiter.consume("member-1", 1_001).allowed).toBe(true);
  });

  it("removes expired entries before evicting the least-recently-seen key", () => {
    const limiter = new TokenBucketLimiter({
      ratePerMinute: 1,
      burst: 1,
      idleTtlMs: 100,
      maxKeys: 3,
    });

    limiter.consume("expired-1", 0);
    limiter.consume("expired-2", 0);
    limiter.consume("active", 90);
    limiter.consume("new", 101);

    expect(limiter.retainedKeys).toBe(2);
    expect(limiter.consume("active", 101).allowed).toBe(false);
    expect(limiter.consume("expired-1", 101).allowed).toBe(true);
  });

  it("never retains more than 10,000 keys", () => {
    const limiter = new TokenBucketLimiter({ ratePerMinute: 1, burst: 1 });

    for (let index = 0; index < 10_050; index += 1) {
      limiter.consume(`key-${index}`, index);
    }

    expect(limiter.retainedKeys).toBe(10_000);
  });
});

describe("resolveClientSource", () => {
  it("trusts one valid proxy address from a loopback peer", () => {
    expect(resolveClientSource(request("127.0.0.1", {
      "x-captain-client-ip": "203.0.113.8",
    }))).toBe("203.0.113.8");
  });

  it("uses a direct non-loopback peer instead of forwarded headers", () => {
    expect(resolveClientSource(request("198.51.100.3", {
      "x-captain-client-ip": "203.0.113.8",
      "x-forwarded-for": "192.0.2.1",
    }))).toBe("198.51.100.3");
  });

  it("rejects comma lists and malformed proxy addresses", () => {
    expect(resolveClientSource(request("127.0.0.1", {
      "x-captain-client-ip": "forged, 203.0.113.8",
    }))).toBe("unknown");
    expect(resolveClientSource(request("::1", {
      "x-captain-client-ip": ["203.0.113.8", "198.51.100.3"],
    }))).toBe("unknown");
    expect(resolveClientSource(request("not-an-ip"))).toBe("unknown");
  });

  it("accepts a single client address from IPv6 loopback peers", () => {
    expect(resolveClientSource(request("::1", {
      "x-captain-client-ip": "2001:db8::8",
    }))).toBe("2001:db8::8");
    expect(resolveClientSource(request("::ffff:127.0.0.1", {
      "x-captain-client-ip": "203.0.113.8",
    }))).toBe("203.0.113.8");
    expect(resolveClientSource(request("::ffff:127.0.0.42", {
      "x-captain-client-ip": "203.0.113.9",
    }))).toBe("203.0.113.9");
  });
});

describe("CaptainAuthenticator", () => {
  function fixture(revokedAt: string | null = null) {
    const issued = issueMemberToken();
    const member: StoredMemberAuth = {
      memberId: "member-1",
      name: "Agent One",
      email: "agent@example.com",
      createdAt: "2026-08-18T12:00:00.000Z",
      rotatedAt: null,
      revokedAt,
      lookupId: issued.lookupId,
      digest: issued.digest,
    };
    const store = {
      findMemberForAuth: (lookupId: string) => lookupId === issued.lookupId ? member : null,
    };
    return { issued, member, store };
  }

  it("returns a public member for an exact valid bearer token", () => {
    const { issued, store } = fixture();
    const authenticator = new CaptainAuthenticator(store);

    const member = authenticator.authenticate(request(
      "198.51.100.3",
      authorization(issued.token),
    ));

    expect(member).toEqual({
      memberId: "member-1",
      name: "Agent One",
      email: "agent@example.com",
      createdAt: "2026-08-18T12:00:00.000Z",
      rotatedAt: null,
      revokedAt: null,
    });
    expect(member).not.toHaveProperty("lookupId");
    expect(member).not.toHaveProperty("digest");
  });

  it("returns the same unauthorized shape for every authentication failure class", () => {
    const active = fixture();
    const revoked = fixture("2026-08-18T13:00:00.000Z");
    const cases = [
      { authenticator: new CaptainAuthenticator(active.store), headers: {} },
      { authenticator: new CaptainAuthenticator(active.store), headers: { authorization: active.issued.token } },
      { authenticator: new CaptainAuthenticator(active.store), headers: { authorization: `bearer ${active.issued.token}` } },
      { authenticator: new CaptainAuthenticator(active.store), headers: { authorization: `Bearer  ${active.issued.token}` } },
      { authenticator: new CaptainAuthenticator(active.store), headers: { authorization: [`Bearer ${active.issued.token}`] } },
      { authenticator: new CaptainAuthenticator(active.store), headers: authorization("cap_v1_short.secret") },
      { authenticator: new CaptainAuthenticator(active.store), headers: authorization(`cap_v1_${differentBase64Url(active.issued.lookupId)}.${active.issued.secret}`) },
      { authenticator: new CaptainAuthenticator(active.store), headers: authorization(`cap_v1_${active.issued.lookupId}.${differentBase64Url(active.issued.secret)}`) },
      { authenticator: new CaptainAuthenticator(revoked.store), headers: authorization(revoked.issued.token) },
    ];

    const problems = cases.map((testCase) => captureProblem(
      () => testCase.authenticator.authenticate(request("198.51.100.3", testCase.headers)),
    ));
    expect(problems.map(({ status, code, message, retryAfterSeconds }) => ({
      status,
      code,
      message,
      retryAfterSeconds,
    }))).toEqual(Array.from({ length: cases.length }, () => ({
      status: 401,
      code: "UNAUTHORIZED",
      message: "Authentication required.",
      retryAfterSeconds: undefined,
    })));
  });

  it("rate limits unknown, known-wrong, and revoked lookup IDs identically", () => {
    const active = fixture();
    const revoked = fixture("2026-08-18T13:00:00.000Z");
    const tokens = [
      {
        authenticator: new CaptainAuthenticator(active.store, { now: () => 0 }),
        token: `cap_v1_${differentBase64Url(active.issued.lookupId)}.${active.issued.secret}`,
      },
      {
        authenticator: new CaptainAuthenticator(active.store, { now: () => 0 }),
        token: `cap_v1_${active.issued.lookupId}.${differentBase64Url(active.issued.secret)}`,
      },
      {
        authenticator: new CaptainAuthenticator(revoked.store, { now: () => 0 }),
        token: revoked.issued.token,
      },
    ];

    for (const { authenticator, token } of tokens) {
      const problems = Array.from({ length: 6 }, (_, attempt) => captureProblem(
        () => authenticator.authenticate(request(
          `198.51.100.${attempt + 1}`,
          authorization(token),
        )),
      ));
      expect(problems.map(({ status, code, message, retryAfterSeconds }) => ({
        status,
        code,
        message,
        retryAfterSeconds,
      }))).toEqual([
        ...Array.from({ length: 5 }, () => ({
          status: 401,
          code: "UNAUTHORIZED",
          message: "Authentication required.",
          retryAfterSeconds: undefined,
        })),
        {
          status: 429,
          code: "RATE_LIMITED",
          message: "Too many authentication attempts.",
          retryAfterSeconds: 6,
        },
      ]);
    }
  });

  it("limits failures from one verified source", () => {
    const { store } = fixture();
    const authenticator = new CaptainAuthenticator(store, { now: () => 0 });

    for (let attempt = 0; attempt < 5; attempt += 1) {
      expectProblem(() => authenticator.authenticate(request("198.51.100.3")), 401);
    }
    expectProblem(() => authenticator.authenticate(request("198.51.100.3")), 429, 6);
  });

  it("limits failures for one known lookup ID across sources", () => {
    const { issued, store } = fixture();
    const authenticator = new CaptainAuthenticator(store, { now: () => 0 });
    const wrongToken = `cap_v1_${issued.lookupId}.${differentBase64Url(issued.secret)}`;

    for (let attempt = 0; attempt < 5; attempt += 1) {
      expectProblem(() => authenticator.authenticate(request(
        `198.51.100.${attempt + 1}`,
        authorization(wrongToken),
      )), 401);
    }
    expectProblem(() => authenticator.authenticate(request(
      "198.51.100.6",
      authorization(wrongToken),
    )), 429, 6);
  });

  it("limits global failures across distinct sources", () => {
    const { store } = fixture();
    const authenticator = new CaptainAuthenticator(store, { now: () => 0 });

    for (let attempt = 0; attempt < 100; attempt += 1) {
      expectProblem(() => authenticator.authenticate(request("127.0.0.1", {
        "x-captain-client-ip": `2001:db8::${attempt + 1}`,
      })), 401);
    }
    expectProblem(() => authenticator.authenticate(request("127.0.0.1", {
      "x-captain-client-ip": "2001:db8::101",
    })), 429, 1);
  });

  it("accepts a valid token after its source, lookup, and global invalid buckets reject", () => {
    const { issued, store } = fixture();
    const authenticator = new CaptainAuthenticator(store, { now: () => 0 });
    const wrongToken = `cap_v1_${issued.lookupId}.${differentBase64Url(issued.secret)}`;

    for (let attempt = 0; attempt < 101; attempt += 1) {
      const source = attempt < 6 ? "203.0.113.8" : `2001:db8::${attempt}`;
      expectProblem(() => authenticator.authenticate(request("127.0.0.1", {
        authorization: `Bearer ${wrongToken}`,
        "x-captain-client-ip": source,
      })), attempt >= 5 ? 429 : 401);
    }

    expect(authenticator.authenticate(request("127.0.0.1", {
      authorization: `Bearer ${issued.token}`,
      "x-captain-client-ip": "203.0.113.8",
    })).memberId).toBe("member-1");
  });
});

describe("PollLimiter", () => {
  it("limits polls per member and refills independently", () => {
    let now = 0;
    const limiter = new PollLimiter({ now: () => now });

    for (let attempt = 0; attempt < 5; attempt += 1) limiter.check("member-1");
    expectProblem(() => limiter.check("member-1"), 429, 2);
    expect(() => limiter.check("member-2")).not.toThrow();

    now = 2_000;
    expect(() => limiter.check("member-1")).not.toThrow();
  });
});

describe("LimitEventAggregator", () => {
  it("emits a fixed-shape summary on the interval without another event", () => {
    vi.useFakeTimers();
    const emitted: Record<string, number>[] = [];
    const events = new LimitEventAggregator({
      intervalMs: 60_000,
      emit: (counts) => emitted.push(counts),
    });
    try {
      events.record("auth_failed");
      events.record("auth_failed");
      events.record("client-controlled-value");
      expect(emitted).toEqual([]);

      vi.advanceTimersByTime(60_000);
      expect(emitted).toEqual([{
        auth_failed: 2,
        auth_rate_limited: 0,
        poll_rate_limited: 0,
      }]);
      expect(events.flush()).toEqual({
        auth_failed: 0,
        auth_rate_limited: 0,
        poll_rate_limited: 0,
      });
    } finally {
      events.close();
      vi.useRealTimers();
    }
  });

  it("counts authentication and poll rejections without dynamic keys", () => {
    const events = new LimitEventAggregator();
    const issued = issueMemberToken();
    const store = { findMemberForAuth: () => null };
    const authenticator = new CaptainAuthenticator(store, { now: () => 0, events });
    const polls = new PollLimiter({ now: () => 0, events });

    for (let attempt = 0; attempt < 6; attempt += 1) {
      expectProblem(() => authenticator.authenticate(request(
        `198.51.100.${attempt + 1}`,
        authorization(issued.token),
      )), attempt === 5 ? 429 : 401);
      if (attempt < 5) polls.check("member-1");
    }
    expectProblem(() => polls.check("member-1"), 429, 2);

    expect(events.flush()).toEqual({
      auth_failed: 6,
      auth_rate_limited: 1,
      poll_rate_limited: 1,
    });
  });
});
