import type { TurnState } from "./contracts.js";
export interface AuditLogOptions {
    now?: () => string;
}
export interface AuditEventInput {
    event: string;
    memberId?: string;
    operation: string;
    route: string;
    reportId?: string;
    turnId?: string;
    fromState?: TurnState;
    toState?: TurnState;
    durationMs?: number;
    code?: string;
    count?: number;
}
export declare class AuditLog {
    readonly path: string;
    private descriptor;
    private readonly now;
    constructor(path: string, options?: AuditLogOptions);
    initialize(): void;
    record(input: AuditEventInput): void;
    recordLimitSummary(counts: Record<string, number>): void;
    close(): void;
}
