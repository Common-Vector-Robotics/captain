import {
  chmodSync,
  closeSync,
  constants,
  fchmodSync,
  fsyncSync,
  mkdirSync,
  openSync,
  writeSync,
} from "node:fs";
import { dirname } from "node:path";

import type { TurnState } from "./contracts.js";

const EVENT_NAMES = new Set([
  "member_created",
  "member_rotated",
  "member_revoked",
  "submit_authenticated",
  "poll_authenticated",
  "http_error",
  "turn_queued",
  "turn_started",
  "turn_succeeded",
  "turn_failed",
  "turn_timed_out",
  "turn_unknown_outcome",
  "limit_summary",
]);
const OPERATIONS = new Set(["member", "submit", "poll", "turn", "limit"]);
const ROUTES = new Set(["local_cli", "submit", "poll", "worker", "auth", "ingress"]);
const STATES = new Set<TurnState>([
  "queued",
  "started",
  "succeeded",
  "failed",
  "timed_out",
  "unknown_outcome",
]);
const STABLE_CODE = /^[A-Z][A-Z0-9_]{0,63}$/;
const SAFE_IDENTIFIER = /^[A-Za-z0-9._-]{1,128}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const LIMIT_CODES = [
  ["auth_failed", "AUTH_FAILED"],
  ["auth_rate_limited", "AUTH_RATE_LIMITED"],
  ["poll_rate_limited", "POLL_RATE_LIMITED"],
  ["job_rate_limited", "JOB_RATE_LIMITED"],
] as const;

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

function optionalIdentifier(
  value: string | undefined,
  field: "member" | "report" | "turn",
): string | null {
  if (value === undefined) return null;
  const pattern = field === "report" ? SAFE_IDENTIFIER : UUID;
  if (!pattern.test(value)) throw new TypeError(`Audit ${field} ID is invalid.`);
  return value;
}

function optionalState(value: TurnState | undefined): TurnState | null {
  if (value === undefined) return null;
  if (!STATES.has(value)) throw new TypeError("Audit turn state is invalid.");
  return value;
}

function optionalCount(value: number | undefined): number | null {
  if (value === undefined) return null;
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError("Audit count must be a positive safe integer.");
  }
  return value;
}

function optionalDuration(value: number | undefined): number | null {
  if (value === undefined) return null;
  if (!Number.isFinite(value) || value < 0) {
    throw new TypeError("Audit duration must be finite and nonnegative.");
  }
  return Math.round(value);
}

export class AuditLog {
  private descriptor: number | null = null;
  private readonly now: () => string;

  constructor(
    public readonly path: string,
    options: AuditLogOptions = {},
  ) {
    this.now = options.now ?? (() => new Date().toISOString());
  }

  initialize(): void {
    if (this.descriptor !== null) return;
    const directory = dirname(this.path);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    chmodSync(directory, 0o700);
    const descriptor = openSync(
      this.path,
      constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT,
      0o600,
    );
    try {
      fchmodSync(descriptor, 0o600);
    } catch (error) {
      closeSync(descriptor);
      throw error;
    }
    this.descriptor = descriptor;
  }

  record(input: AuditEventInput): void {
    if (this.descriptor === null) throw new Error("Captain audit log is not initialized.");
    if (!EVENT_NAMES.has(input.event)) throw new TypeError("Audit event is invalid.");
    if (!OPERATIONS.has(input.operation)) throw new TypeError("Audit operation is invalid.");
    if (!ROUTES.has(input.route)) throw new TypeError("Audit route is invalid.");
    if (input.code !== undefined && !STABLE_CODE.test(input.code)) {
      throw new TypeError("Audit code is invalid.");
    }

    const event = {
      timestamp: this.now(),
      event: input.event,
      member_id: optionalIdentifier(input.memberId, "member"),
      operation: input.operation,
      route: input.route,
      report_id: optionalIdentifier(input.reportId, "report"),
      turn_id: optionalIdentifier(input.turnId, "turn"),
      from_state: optionalState(input.fromState),
      to_state: optionalState(input.toState),
      duration_ms: optionalDuration(input.durationMs),
      code: input.code ?? null,
      count: optionalCount(input.count),
    };
    const line = Buffer.from(`${JSON.stringify(event)}\n`, "utf8");
    let offset = 0;
    while (offset < line.length) {
      const written = writeSync(
        this.descriptor,
        line,
        offset,
        line.length - offset,
      );
      if (written < 1) throw new Error("Captain audit append did not complete.");
      offset += written;
    }
    fsyncSync(this.descriptor);
  }

  recordLimitSummary(counts: Record<string, number>): void {
    for (const [kind, code] of LIMIT_CODES) {
      const count = counts[kind];
      if (!Number.isSafeInteger(count) || count <= 0) continue;
      this.record({
        event: "limit_summary",
        operation: "limit",
        route: kind.startsWith("auth_") ? "auth" : kind.startsWith("poll_") ? "poll" : "submit",
        code,
        count,
      });
    }
  }

  close(): void {
    if (this.descriptor === null) return;
    closeSync(this.descriptor);
    this.descriptor = null;
  }
}
