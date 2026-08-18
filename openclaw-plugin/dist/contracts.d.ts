export type TurnState = "queued" | "started" | "succeeded" | "failed" | "timed_out" | "unknown_outcome";
export type CaptainStatus = "created" | "updated" | "queued" | "needs_clarification" | "needs_configuration" | "partial" | "failed" | "unknown_outcome";
export interface ReportContext {
    git_root?: string;
    cwd?: string;
    branch?: string;
    upstream?: string;
    status?: string;
    recent_commits?: string[];
    diff_stat?: string;
}
export interface ReportVerification {
    command: string;
    result: string;
}
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
export type TurnInput = {
    turn_id: string;
    kind: "report";
    report: ReportPayload;
    metadata: Record<string, unknown>;
} | {
    turn_id: string;
    kind: "reply";
    reply: string;
};
export interface ClickUpUpdate {
    action: string;
    task_id: string;
}
export interface CaptainResult {
    report_id: string;
    status: CaptainStatus;
    clickup_updates: ClickUpUpdate[];
    captain_feedback: string;
    questions: string[];
    warnings: string[];
}
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
export declare class HttpProblem extends Error {
    readonly status: number;
    readonly code: string;
    constructor(status: number, code: string, message: string);
}
export declare function parseTurnInput(value: unknown): TurnInput;
export declare function canonicalizeTurnInput(input: TurnInput): string;
export declare function digestTurnInput(input: TurnInput): string;
export declare function normalizeCaptainResult(reportId: string, value: unknown): CaptainResult;
