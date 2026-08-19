/** Lifecycle state of a submitted Captain turn. */
export type TurnState = 'queued' | 'started' | 'succeeded' | 'failed' | 'timed_out' | 'unknown_outcome';
/** Outcome status reported by the Captain agent for a turn. */
export type CaptainStatus = 'created' | 'updated' | 'queued' | 'needs_clarification' | 'needs_configuration' | 'partial' | 'failed' | 'unknown_outcome';
/** Optional repository context attached to a report. */
export interface ReportContext {
    git_root?: string;
    cwd?: string;
    branch?: string;
    upstream?: string;
    status?: string;
    recent_commits?: string[];
    diff_stat?: string;
}
/** A verification command and its observed result. */
export interface ReportVerification {
    command: string;
    result: string;
}
/** The structured body of an employee status report. */
export interface ReportPayload {
    project?: string;
    context?: ReportContext;
    summary: string[];
    changed_files?: string[];
    verification?: ReportVerification[];
    decisions?: string[];
    blockers?: string[];
    risks?: string[];
    next_steps?: string[];
}
/** A validated turn submission: either a report or a reply. */
export type TurnInput = {
    turn_id: string;
    kind: 'report';
    report: ReportPayload;
    metadata: Record<string, unknown>;
} | {
    turn_id: string;
    kind: 'reply';
    reply: string;
};
/** A single ClickUp task update performed by Captain. */
export interface ClickUpUpdate {
    action: string;
    task_id: string;
}
/** The canonical result object returned by the Captain agent. */
export interface CaptainResult {
    report_id: string;
    status: CaptainStatus;
    clickup_updates: ClickUpUpdate[];
    captain_feedback: string;
    questions: string[];
    warnings: string[];
}
/** The public HTTP envelope describing a turn and its outcome. */
export interface TurnEnvelope {
    report_id: string;
    turn_id: string;
    turn_status: TurnState;
    result?: CaptainResult;
    error?: {
        code: string;
        message: string;
    };
}
/** An error carrying a stable HTTP status and public error code. */
export declare class HttpProblem extends Error {
    readonly status: number;
    readonly code: string;
    constructor(status: number, code: string, message: string);
}
/** Validates an untrusted request body into a strict turn input. */
export declare function parseTurnInput(value: unknown): TurnInput;
/** Serializes a validated turn input with deterministically sorted keys. */
export declare function canonicalizeTurnInput(input: TurnInput): string;
/** Computes the SHA-256 hex digest of the canonical turn input. */
export declare function digestTurnInput(input: TurnInput): string;
/** Normalizes untrusted Captain output into a bounded canonical result. */
export declare function normalizeCaptainResult(reportId: string, value: unknown): CaptainResult;
