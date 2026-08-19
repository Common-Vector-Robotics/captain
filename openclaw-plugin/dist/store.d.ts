import { type AuditEventInput, type AuditSink } from './audit.js';
import { type CaptainResult, type TurnInput, type TurnState } from './contracts.js';
import { type IssuedToken } from './security.js';
/** Public identity fields of a stored Captain member. */
export interface StoredMember {
    memberId: string;
    name: string;
    email: string;
    createdAt: string;
    rotatedAt: string | null;
    revokedAt: string | null;
}
/** A stored member including its credential lookup ID and digest. */
export interface StoredMemberAuth extends StoredMember {
    lookupId: string;
    digest: Buffer;
}
/** Composite identifier of one turn. */
export interface TurnKey {
    memberId: string;
    reportId: string;
    turnId: string;
}
/** A stored report row with its server-owned session ID. */
export interface StoredReport {
    memberId: string;
    reportId: string;
    sessionId: string;
    createdAt: string;
    updatedAt: string;
}
/** A turn state that can no longer change. */
export type TerminalTurnState = Exclude<TurnState, 'queued' | 'started'>;
/** Maximum accepted length of a member display name. */
export declare const MAX_MEMBER_NAME_CHARACTERS = 100;
/** Construction options for the durable Captain remote store. */
export interface CaptainRemoteStoreOptions {
    maxGlobalActiveTurns?: number;
    auditLog?: AuditSink;
}
/** A stable public error attached to a failed turn. */
export interface StoredTurnError {
    code: string;
    message: string;
}
/** A fully materialized stored turn row. */
export interface StoredTurn extends TurnKey {
    kind: TurnInput['kind'];
    requestDigest: string;
    payload: TurnInput;
    state: TurnState;
    runId: string | null;
    result: CaptainResult | null;
    error: StoredTurnError | null;
    createdAt: string;
    startedAt: string | null;
    finishedAt: string | null;
}
/** A claimed turn joined with its member and report rows. */
export interface ClaimedTurn extends StoredTurn {
    member: StoredMember;
    report: StoredReport;
}
/** Input required to reserve a new turn idempotently. */
export interface ReserveTurnInput extends TurnKey {
    requestDigest: string;
    payloadJson: string;
}
/** Outcome of a turn reservation, created or replayed. */
export interface ReserveTurnResult {
    status: 'created' | 'existing';
    report: StoredReport;
    turn: StoredTurn;
}
/** Trims and validates a member display name. */
export declare function normalizeMemberName(value: string): string;
/** SQLite-backed durable store for members, reports, turns, and audits. */
export declare class CaptainRemoteStore {
    private readonly databasePath;
    private database;
    private readonly maxGlobalActiveTurns;
    private readonly audit;
    constructor(databasePath: string, options?: CaptainRemoteStoreOptions);
    initialize(): void;
    close(): void;
    recordAudit(event: AuditEventInput): void;
    recordLimitSummary(counts: Record<string, number>): void;
    createMember(name: string, email: string, issued: IssuedToken): StoredMember;
    createMemberWithId(memberId: string, name: string, email: string, issued: IssuedToken): StoredMember;
    listMembers(): StoredMember[];
    findMemberForAuth(lookupId: string): StoredMemberAuth | null;
    prepareMemberRotation(memberId: string): IssuedToken;
    rotateMember(memberId: string, issued: IssuedToken): StoredMember;
    revokeMember(memberId: string): StoredMember;
    reserveTurn(input: ReserveTurnInput): ReserveTurnResult;
    getTurn(key: TurnKey): StoredTurn | null;
    claimNextTurn(maxRunning: number): ClaimedTurn | null;
    finishTurn(key: TurnKey, state: TerminalTurnState, result?: CaptainResult, error?: StoredTurnError): void;
    recoverStartedTurns(): number;
    private getMember;
    private selectReport;
    private selectTurn;
    private countMemberActive;
    private countGlobalActive;
    private touchReport;
    private enqueueAudit;
    private projectAuditBestEffort;
    private inImmediateTransaction;
    private getDatabase;
}
