import type { TurnState } from './contracts.js';
/** Construction options for an append-only audit log. */
export interface AuditLogOptions {
    now?: () => string;
}
/** Caller-supplied fields for one audit event. */
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
/** A fixed-shape audit record as persisted to the JSONL log. */
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
/** Destination for audit events, such as a JSONL file. */
export interface AuditSink {
    initialize(): void;
    append(event: AuditEvent): void;
    close(): void;
}
/** Validates event fields and produces a complete audit record. */
export declare function createAuditEvent(input: AuditEventInput, eventId?: string, timestamp?: string): AuditEvent;
/** Parses and revalidates a stored audit event line. */
export declare function parseAuditEvent(serialized: string): AuditEvent;
/** Converts aggregated limit counts into audit event inputs. */
export declare function limitSummaryAuditInputs(counts: Record<string, number>): AuditEventInput[];
/** Durable append-only JSONL audit log with owner-only file modes. */
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
