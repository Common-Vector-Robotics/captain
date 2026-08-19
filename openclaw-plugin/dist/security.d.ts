import type { IncomingMessage } from "node:http";
import { HttpProblem } from "./contracts.js";
import type { StoredMember, StoredMemberAuth } from "./store.js";
export interface LimitDecision {
    allowed: boolean;
    retryAfterSeconds?: number;
}
export interface TokenBucketOptions {
    ratePerMinute: number;
    burst: number;
    idleTtlMs?: number;
    maxKeys?: number;
}
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
export declare function resolveClientSource(req: IncomingMessage): string;
declare const LIMIT_EVENT_KINDS: readonly ["auth_failed", "auth_rate_limited", "poll_rate_limited", "job_rate_limited"];
export type LimitEventKind = typeof LIMIT_EVENT_KINDS[number];
export interface LimitEventAggregatorOptions {
    intervalMs?: number;
    emit?: (counts: Record<string, number>) => void;
}
export declare class LimitEventAggregator {
    private counts;
    private readonly emit?;
    private readonly timer?;
    constructor(options?: LimitEventAggregatorOptions);
    record(kind: string): void;
    flush(): Record<string, number>;
    close(): void;
}
export declare class SecurityProblem extends HttpProblem {
    readonly retryAfterSeconds?: number | undefined;
    constructor(status: 401 | 429, code: "UNAUTHORIZED" | "RATE_LIMITED", message: string, retryAfterSeconds?: number | undefined);
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
export interface PollLimiterOptions {
    now?: () => number;
    events?: LimitEventAggregator;
    ratePerMinute?: number;
    burst?: number;
}
export declare class PollLimiter {
    private readonly limiter;
    private readonly now;
    private readonly events;
    constructor(options?: PollLimiterOptions);
    check(memberId: string): void;
}
export interface IssuedToken {
    token: string;
    lookupId: string;
    secret: string;
    digest: Buffer;
}
export declare function issueMemberToken(): IssuedToken;
export declare function verifyMemberToken(secret: string, digest: Buffer): boolean;
export {};
