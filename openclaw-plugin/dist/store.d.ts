import { type CaptainResult, type TurnInput, type TurnState } from "./contracts.js";
import type { IssuedToken } from "./security.js";
export interface StoredMember {
    memberId: string;
    name: string;
    email: string;
    createdAt: string;
    rotatedAt: string | null;
    revokedAt: string | null;
}
export interface StoredMemberAuth extends StoredMember {
    lookupId: string;
    digest: Buffer;
}
export interface TurnKey {
    memberId: string;
    reportId: string;
    turnId: string;
}
export interface StoredReport {
    memberId: string;
    reportId: string;
    sessionId: string;
    createdAt: string;
    updatedAt: string;
}
export type TerminalTurnState = Exclude<TurnState, "queued" | "started">;
export interface StoredTurnError {
    code: string;
    message: string;
}
export interface StoredTurn extends TurnKey {
    kind: TurnInput["kind"];
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
export interface ClaimedTurn extends StoredTurn {
    member: StoredMember;
    report: StoredReport;
}
export interface ReserveTurnInput extends TurnKey {
    requestDigest: string;
    payloadJson: string;
}
export type ReserveTurnResult = {
    status: "created" | "existing";
    report: StoredReport;
    turn: StoredTurn;
};
export declare class CaptainRemoteStore {
    private readonly databasePath;
    private database;
    constructor(databasePath: string);
    initialize(): void;
    close(): void;
    createMember(name: string, email: string, issued: IssuedToken): StoredMember;
    listMembers(): StoredMember[];
    findMemberForAuth(lookupId: string): StoredMemberAuth | null;
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
    private inImmediateTransaction;
    private getDatabase;
}
