import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import type { IncomingMessage } from "node:http";
import { isIP, SocketAddress } from "node:net";

import { HttpProblem } from "./contracts.js";
import type { StoredMember, StoredMemberAuth } from "./store.js";

const MAX_LIMITER_KEYS = 10_000;
const DEFAULT_IDLE_TTL_MS = 15 * 60_000;
const TOKEN_PATTERN = /^Bearer cap_v1_([A-Za-z0-9_-]{16})\.([A-Za-z0-9_-]{43})$/;
const DUMMY_DIGEST = Buffer.alloc(32);

export interface LimitDecision {
  allowed: boolean;
  retryAfterSeconds?: number;
}

interface TokenBucketEntry {
  tokens: number;
  lastRefillMs: number;
  lastSeenMs: number;
}

export interface TokenBucketOptions {
  ratePerMinute: number;
  burst: number;
  idleTtlMs?: number;
  maxKeys?: number;
}

export class TokenBucketLimiter {
  private readonly entries = new Map<string, TokenBucketEntry>();
  private readonly refillPerMs: number;
  private readonly burst: number;
  private readonly idleTtlMs: number;
  private readonly maxKeys: number;

  constructor(options: TokenBucketOptions) {
    if (!Number.isFinite(options.ratePerMinute) || options.ratePerMinute <= 0) {
      throw new TypeError("ratePerMinute must be positive.");
    }
    if (!Number.isSafeInteger(options.burst) || options.burst <= 0) {
      throw new TypeError("burst must be a positive safe integer.");
    }
    const idleTtlMs = options.idleTtlMs ?? DEFAULT_IDLE_TTL_MS;
    if (!Number.isFinite(idleTtlMs) || idleTtlMs <= 0) {
      throw new TypeError("idleTtlMs must be positive.");
    }
    const maxKeys = options.maxKeys ?? MAX_LIMITER_KEYS;
    if (!Number.isSafeInteger(maxKeys) || maxKeys <= 0 || maxKeys > MAX_LIMITER_KEYS) {
      throw new TypeError(`maxKeys must be between 1 and ${MAX_LIMITER_KEYS}.`);
    }

    this.refillPerMs = options.ratePerMinute / 60_000;
    this.burst = options.burst;
    this.idleTtlMs = idleTtlMs;
    this.maxKeys = maxKeys;
  }

  get retainedKeys(): number {
    return this.entries.size;
  }

  consume(key: string, nowMs = Date.now()): LimitDecision {
    if (!Number.isFinite(nowMs)) throw new TypeError("nowMs must be finite.");

    let entry = this.entries.get(key);
    if (entry && nowMs - entry.lastSeenMs > this.idleTtlMs) {
      this.entries.delete(key);
      entry = undefined;
    }

    if (!entry) {
      this.makeRoom(nowMs);
      entry = {
        tokens: this.burst,
        lastRefillMs: nowMs,
        lastSeenMs: nowMs,
      };
      this.entries.set(key, entry);
    }

    const elapsedMs = Math.max(0, nowMs - entry.lastRefillMs);
    entry.tokens = Math.min(this.burst, entry.tokens + elapsedMs * this.refillPerMs);
    entry.lastRefillMs = Math.max(entry.lastRefillMs, nowMs);
    entry.lastSeenMs = Math.max(entry.lastSeenMs, nowMs);

    if (entry.tokens >= 1) {
      entry.tokens -= 1;
      return { allowed: true };
    }

    return {
      allowed: false,
      retryAfterSeconds: Math.ceil((1 - entry.tokens) / this.refillPerMs / 1_000),
    };
  }

  private makeRoom(nowMs: number): void {
    if (this.entries.size < this.maxKeys) return;

    for (const [key, entry] of this.entries) {
      if (nowMs - entry.lastSeenMs > this.idleTtlMs) this.entries.delete(key);
    }
    if (this.entries.size < this.maxKeys) return;

    let oldestKey: string | undefined;
    let oldestSeenMs = Number.POSITIVE_INFINITY;
    for (const [key, entry] of this.entries) {
      if (entry.lastSeenMs < oldestSeenMs) {
        oldestKey = key;
        oldestSeenMs = entry.lastSeenMs;
      }
    }
    if (oldestKey !== undefined) this.entries.delete(oldestKey);
  }
}

function normalizeIp(address: string): string | null {
  const value = address.trim();
  const family = isIP(value);
  if (family === 0) return null;

  const parsed = SocketAddress.parse(family === 6 ? `[${value}]` : value);
  if (!parsed) return null;
  const normalized = parsed.address.toLowerCase();
  const mappedPrefix = "::ffff:";
  if (parsed.family === "ipv6" && normalized.startsWith(mappedPrefix)) {
    const mappedAddress = normalized.slice(mappedPrefix.length);
    if (isIP(mappedAddress) === 4) return mappedAddress;
  }
  return normalized;
}

function isLoopback(address: string): boolean {
  if (address === "::1") return true;
  if (isIP(address) !== 4) return false;
  const firstOctet = Number(address.split(".", 1)[0]);
  return firstOctet === 127;
}

export function resolveClientSource(req: IncomingMessage): string {
  const peer = req.socket.remoteAddress
    ? normalizeIp(req.socket.remoteAddress)
    : null;
  if (!peer) return "unknown";
  if (!isLoopback(peer)) return peer;

  const supplied = req.headers["x-captain-client-ip"];
  if (typeof supplied !== "string") return "unknown";
  if (supplied.includes(",")) return "unknown";
  return normalizeIp(supplied) ?? "unknown";
}

const LIMIT_EVENT_KINDS = [
  "auth_failed",
  "auth_rate_limited",
  "poll_rate_limited",
  "job_rate_limited",
] as const;

export type LimitEventKind = typeof LIMIT_EVENT_KINDS[number];
type LimitCounts = Record<LimitEventKind, number>;

export interface LimitEventAggregatorOptions {
  intervalMs?: number;
  emit?: (counts: Record<string, number>) => void;
}

function emptyLimitCounts(): LimitCounts {
  return {
    auth_failed: 0,
    auth_rate_limited: 0,
    poll_rate_limited: 0,
    job_rate_limited: 0,
  };
}

export class LimitEventAggregator {
  private counts = emptyLimitCounts();
  private readonly emit?: (counts: Record<string, number>) => void;
  private readonly timer?: ReturnType<typeof setInterval>;

  constructor(options: LimitEventAggregatorOptions = {}) {
    const intervalMs = options.intervalMs ?? 60_000;
    if (!Number.isFinite(intervalMs) || intervalMs <= 0) {
      throw new TypeError("intervalMs must be positive.");
    }
    this.emit = options.emit;
    if (this.emit) {
      this.timer = setInterval(() => {
        const summary = this.flush();
        if (Object.values(summary).some((count) => count > 0)) this.emit?.(summary);
      }, intervalMs);
      this.timer.unref();
    }
  }

  record(kind: string): void {
    // Only fixed internal event names can become aggregation keys.
    if (LIMIT_EVENT_KINDS.includes(kind as LimitEventKind)) {
      this.counts[kind as LimitEventKind] += 1;
    }
  }

  flush(): Record<string, number> {
    const summary = { ...this.counts };
    this.counts = emptyLimitCounts();
    return summary;
  }

  close(): void {
    if (this.timer) clearInterval(this.timer);
  }
}

export class SecurityProblem extends HttpProblem {
  constructor(
    status: 401 | 429,
    code: "UNAUTHORIZED" | "RATE_LIMITED",
    message: string,
    public readonly retryAfterSeconds?: number,
  ) {
    super(status, code, message);
    this.name = "SecurityProblem";
  }
}

export interface MemberAuthStore {
  findMemberForAuth(lookupId: string): StoredMemberAuth | null;
}

export interface CaptainAuthenticatorOptions {
  now?: () => number;
  events?: LimitEventAggregator;
  invalidAuthPerSourcePerMinute?: number;
  invalidAuthPerSourceBurst?: number;
  invalidAuthGlobalPerMinute?: number;
}

export class CaptainAuthenticator {
  private readonly sourceLimiter: TokenBucketLimiter;
  private readonly lookupLimiter: TokenBucketLimiter;
  private readonly globalLimiter: TokenBucketLimiter;
  private readonly now: () => number;
  private readonly events: LimitEventAggregator;

  constructor(
    private readonly store: MemberAuthStore,
    options: CaptainAuthenticatorOptions = {},
  ) {
    const invalidAuthPerSourcePerMinute = options.invalidAuthPerSourcePerMinute ?? 10;
    const invalidAuthPerSourceBurst = options.invalidAuthPerSourceBurst ?? 5;
    const invalidAuthGlobalPerMinute = options.invalidAuthGlobalPerMinute ?? 100;
    this.sourceLimiter = new TokenBucketLimiter({
      ratePerMinute: invalidAuthPerSourcePerMinute,
      burst: invalidAuthPerSourceBurst,
    });
    this.lookupLimiter = new TokenBucketLimiter({
      ratePerMinute: invalidAuthPerSourcePerMinute,
      burst: invalidAuthPerSourceBurst,
    });
    this.globalLimiter = new TokenBucketLimiter({
      ratePerMinute: invalidAuthGlobalPerMinute,
      burst: invalidAuthGlobalPerMinute,
    });
    this.now = options.now ?? Date.now;
    this.events = options.events ?? new LimitEventAggregator();
  }

  authenticate(req: IncomingMessage): StoredMember {
    const parsed = parseBearerToken(singleAuthorizationHeader(req));
    const member = parsed ? this.store.findMemberForAuth(parsed.lookupId) : null;

    if (parsed) {
      // A dummy digest keeps unknown lookup IDs on the same hashing path.
      const secretMatches = verifyMemberToken(parsed.secret, member?.digest ?? DUMMY_DIGEST);
      if (member?.revokedAt === null && secretMatches) return publicMember(member);
    }

    const nowMs = this.now();
    const decisions = [this.sourceLimiter.consume(resolveClientSource(req), nowMs)];
    if (parsed) decisions.push(this.lookupLimiter.consume(parsed.lookupId, nowMs));
    decisions.push(this.globalLimiter.consume("invalid-auth", nowMs));

    this.events.record("auth_failed");
    const retryAfterSeconds = decisions.reduce((longest, decision) => {
      if (decision.allowed) return longest;
      return Math.max(longest, decision.retryAfterSeconds ?? 1);
    }, 0);
    if (retryAfterSeconds > 0) {
      this.events.record("auth_rate_limited");
      throw new SecurityProblem(
        429,
        "RATE_LIMITED",
        "Too many authentication attempts.",
        retryAfterSeconds,
      );
    }

    throw new SecurityProblem(401, "UNAUTHORIZED", "Authentication required.");
  }
}

function singleAuthorizationHeader(req: IncomingMessage): string | undefined {
  if (Array.isArray(req.rawHeaders)) {
    const values: string[] = [];
    for (let index = 0; index + 1 < req.rawHeaders.length; index += 2) {
      if (req.rawHeaders[index].toLowerCase() === "authorization") {
        values.push(req.rawHeaders[index + 1]);
      }
    }
    return values.length === 1 ? values[0] : undefined;
  }

  const values = req.headersDistinct?.authorization;
  return values?.length === 1 ? values[0] : undefined;
}

function parseBearerToken(header: string | undefined): {
  lookupId: string;
  secret: string;
} | null {
  if (typeof header !== "string") return null;
  const match = TOKEN_PATTERN.exec(header);
  if (!match) return null;
  return { lookupId: match[1], secret: match[2] };
}

function publicMember(member: StoredMemberAuth): StoredMember {
  return {
    memberId: member.memberId,
    name: member.name,
    email: member.email,
    createdAt: member.createdAt,
    rotatedAt: member.rotatedAt,
    revokedAt: member.revokedAt,
  };
}

export interface PollLimiterOptions {
  now?: () => number;
  events?: LimitEventAggregator;
  ratePerMinute?: number;
  burst?: number;
}

export class PollLimiter {
  private readonly limiter: TokenBucketLimiter;
  private readonly now: () => number;
  private readonly events: LimitEventAggregator;

  constructor(options: PollLimiterOptions = {}) {
    this.limiter = new TokenBucketLimiter({
      ratePerMinute: options.ratePerMinute ?? 30,
      burst: options.burst ?? 5,
    });
    this.now = options.now ?? Date.now;
    this.events = options.events ?? new LimitEventAggregator();
  }

  check(memberId: string): void {
    const decision = this.limiter.consume(memberId, this.now());
    if (decision.allowed) return;

    this.events.record("poll_rate_limited");
    throw new SecurityProblem(
      429,
      "RATE_LIMITED",
      "Too many poll requests.",
      decision.retryAfterSeconds,
    );
  }
}

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
