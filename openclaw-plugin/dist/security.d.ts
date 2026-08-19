import type { IncomingMessage } from 'node:http';
import { HttpProblem } from './contracts.js';
import type { StoredMember, StoredMemberAuth } from './store.js';
/** Result of consuming one token from a rate limiter. */
export interface LimitDecision {
    allowed: boolean;
    retryAfterSeconds?: number;
}
/** Construction options for a token-bucket rate limiter. */
export interface TokenBucketOptions {
    ratePerMinute: number;
    burst: number;
    idleTtlMs?: number;
    maxKeys?: number;
}
/** Keyed token-bucket rate limiter with bounded key retention. */
export declare class TokenBucketLimiter {
    private readonly entries;
    private readonly refillPerMs;
    private readonly burst;
    private readonly idleTtlMs;
    private readonly maxKeys;
    constructor(options: TokenBucketOptions);
    get retainedKeys(): number;
    consume(key: string, nowMs?: number): LimitDecision;
    private makeRoom;
}
/** Resolves the client source address, trusting one proxy hop from loopback. */
export declare function resolveClientSource(req: IncomingMessage): string;
declare const LIMIT_EVENT_KINDS: readonly ['auth_failed', 'auth_rate_limited', 'poll_rate_limited', 'job_rate_limited'];
/** One of the fixed rate-limit event names eligible for aggregation. */
export type LimitEventKind = typeof LIMIT_EVENT_KINDS[number];
/** Construction options for the limit event aggregator. */
export interface LimitEventAggregatorOptions {
    intervalMs?: number;
    emit?: (counts: Record<string, number>) => void;
}
/** Counts fixed rate-limit events and emits periodic summaries. */
export declare class LimitEventAggregator {
    private counts;
    private readonly emit?;
    private readonly timer?;
    constructor(options?: LimitEventAggregatorOptions);
    record(kind: string): void;
    flush(): Record<string, number>;
    close(): void;
}
/** An authentication or rate-limit HTTP problem with optional retry hint. */
export declare class SecurityProblem extends HttpProblem {
    readonly retryAfterSeconds?: number | undefined;
    constructor(status: 401 | 429, code: 'UNAUTHORIZED' | 'RATE_LIMITED', message: string, retryAfterSeconds?: number | undefined);
}
/** Lookup interface the authenticator uses to load member credentials. */
export interface MemberAuthStore {
    findMemberForAuth(lookupId: string): StoredMemberAuth | null;
}
/** Construction options for the Captain bearer-token authenticator. */
export interface CaptainAuthenticatorOptions {
    now?: () => number;
    events?: LimitEventAggregator;
    invalidAuthPerSourcePerMinute?: number;
    invalidAuthPerSourceBurst?: number;
    invalidAuthGlobalPerMinute?: number;
}
/** Authenticates bearer tokens with uniform failures and abuse limits. */
export declare class CaptainAuthenticator {
    private readonly store;
    private readonly sourceLimiter;
    private readonly lookupLimiter;
    private readonly globalLimiter;
    private readonly now;
    private readonly events;
    constructor(store: MemberAuthStore, options?: CaptainAuthenticatorOptions);
    authenticate(req: IncomingMessage): StoredMember;
}
/** Construction options for the per-member poll limiter. */
export interface PollLimiterOptions {
    now?: () => number;
    events?: LimitEventAggregator;
    ratePerMinute?: number;
    burst?: number;
}
/** Rate limits authenticated poll requests per member. */
export declare class PollLimiter {
    private readonly limiter;
    private readonly now;
    private readonly events;
    constructor(options?: PollLimiterOptions);
    check(memberId: string): void;
}
/** A freshly issued member token and its derived verification digest. */
export interface IssuedToken {
    token: string;
    lookupId: string;
    secret: string;
    digest: Buffer;
}
/** Issues a new member bearer token, optionally reusing a lookup ID. */
export declare function issueMemberToken(lookupId?: string): IssuedToken;
/** Verifies a token secret against its stored digest in constant time. */
export declare function verifyMemberToken(secret: string, digest: Buffer): boolean;
export {};
