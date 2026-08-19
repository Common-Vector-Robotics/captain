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
export interface AuditEvent {
    event_id: string;
    timestamp: string;
    event: string;
    member_id: string | null;
    operation: string;
    route: string;
    report_id: string | null;
    turn_id: string | null;
    from_state: TurnState | null;
    to_state: TurnState | null;
    duration_ms: number | null;
    code: string | null;
    count: number | null;
}
export interface AuditSink {
    initialize(): void;
    append(event: AuditEvent): void;
    close(): void;
}
export declare function createAuditEvent(input: AuditEventInput, eventId?: string, timestamp?: string): AuditEvent;
export declare function parseAuditEvent(serialized: string): AuditEvent;
export declare function limitSummaryAuditInputs(counts: Record<string, number>): AuditEventInput[];
export declare class AuditLog implements AuditSink {
    readonly path: string;
    private descriptor;
    private readonly now;
    constructor(path: string, options?: AuditLogOptions);
    initialize(): void;
    append(event: AuditEvent): void;
    record(input: AuditEventInput): void;
    recordLimitSummary(counts: Record<string, number>): void;
    close(): void;
}
