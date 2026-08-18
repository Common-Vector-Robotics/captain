import { createHash } from "node:crypto";

export type TurnState =
  | "queued"
  | "started"
  | "succeeded"
  | "failed"
  | "timed_out"
  | "unknown_outcome";

export type CaptainStatus =
  | "created"
  | "updated"
  | "queued"
  | "needs_clarification"
  | "needs_configuration"
  | "partial"
  | "failed"
  | "unknown_outcome";

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

export type TurnInput =
  | {
      turn_id: string;
      kind: "report";
      report: ReportPayload;
      metadata: Record<string, unknown>;
    }
  | { turn_id: string; kind: "reply"; reply: string };

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
  error?: { code: string; message: string };
}

export class HttpProblem extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "HttpProblem";
  }
}

const TURN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_VALIDATION_NODES = 1_024;
const MAX_CAPTAIN_RESULT_STRING_LENGTH = 4_096;
const MAX_CAPTAIN_RESULT_ITEMS = 32;
const CAPTAIN_STATUSES = new Set<CaptainStatus>([
  "created",
  "updated",
  "queued",
  "needs_clarification",
  "needs_configuration",
  "partial",
  "failed",
  "unknown_outcome",
]);
const REPORT_KEYS = new Set([
  "project",
  "context",
  "summary",
  "changed_files",
  "verification",
  "decisions",
  "blockers",
  "risks",
  "next_steps",
]);
const CONTEXT_KEYS = new Set([
  "git_root",
  "cwd",
  "branch",
  "upstream",
  "status",
  "recent_commits",
  "diff_stat",
]);
const METADATA_KEYS = new Set(["client", "repository", "branch", "timestamp"]);
const VERIFICATION_KEYS = new Set(["command", "result"]);
const RESULT_KEYS = new Set([
  "report_id",
  "status",
  "clickup_updates",
  "captain_feedback",
  "questions",
  "warnings",
]);
const CLICKUP_UPDATE_KEYS = new Set(["action", "task_id"]);
const RESERVED_SEGMENTS = new Set([
  "auth",
  "authentication",
  "authenticated",
  "authorization",
  "authorized",
  "identity",
  "claim",
  "claims",
  "token",
  "agent",
  "agents",
  "session",
  "sessions",
  "model",
  "models",
  "workspace",
  "workspaces",
  "tool",
  "tools",
  "thinking",
  "runtime",
]);

function invalidRequest(message: string): never {
  throw new HttpProblem(400, "INVALID_REQUEST", message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, message: string): Record<string, unknown> {
  if (!isRecord(value)) {
    invalidRequest(message);
  }
  return value;
}

function requireString(value: unknown, message: string): string {
  if (typeof value !== "string") {
    invalidRequest(message);
  }
  return value;
}

function requireStringArray(value: unknown, message: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    invalidRequest(message);
  }
  return [...value];
}

function assertExactKeys(
  value: Record<string, unknown>,
  allowed: Set<string>,
  message: string,
): void {
  if (Object.keys(value).some((key) => !allowed.has(key))) {
    invalidRequest(message);
  }
}

function normalizeKey(key: string): string[] {
  return key
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

function findReservedKey(value: unknown): string | undefined {
  const values = [value];
  let visited = 0;

  while (values.length > 0) {
    const current = values.pop();
    visited += 1;
    if (visited > MAX_VALIDATION_NODES) {
      invalidRequest("Request body is too complex.");
    }

    if (Array.isArray(current)) {
      if (visited + current.length > MAX_VALIDATION_NODES) {
        invalidRequest("Request body is too complex.");
      }
      for (const nested of current) values.push(nested);
      continue;
    }
    if (!isRecord(current)) continue;

    const keys = Object.keys(current);
    if (visited + keys.length > MAX_VALIDATION_NODES) {
      invalidRequest("Request body is too complex.");
    }
    for (const key of keys) {
      if (normalizeKey(key).some((segment) => RESERVED_SEGMENTS.has(segment))) {
        return key;
      }
      values.push(current[key]);
    }
  }
  return undefined;
}

function assertNoReservedKeys(value: unknown): void {
  if (findReservedKey(value)) {
    invalidRequest("Request contains a reserved field.");
  }
}

function parseContext(value: unknown): ReportContext {
  const context = requireRecord(value, "report.context must be an object.");
  assertExactKeys(context, CONTEXT_KEYS, "report.context contains an unknown field.");

  const parsed: ReportContext = {};
  for (const key of ["git_root", "cwd", "branch", "upstream", "status", "diff_stat"] as const) {
    if (key in context) parsed[key] = requireString(context[key], `report.context.${key} must be a string.`);
  }
  if ("recent_commits" in context) {
    parsed.recent_commits = requireStringArray(
      context.recent_commits,
      "report.context.recent_commits must be a string array.",
    );
  }
  return parsed;
}

function parseVerification(value: unknown): ReportVerification[] {
  if (!Array.isArray(value)) {
    invalidRequest("report.verification must be an array.");
  }
  return value.map((item) => {
    const verification = requireRecord(item, "report.verification items must be objects.");
    assertExactKeys(verification, VERIFICATION_KEYS, "report.verification contains an unknown field.");
    return {
      command: requireString(verification.command, "report.verification.command must be a string."),
      result: requireString(verification.result, "report.verification.result must be a string."),
    };
  });
}

function parseReport(value: unknown): ReportPayload {
  const report = requireRecord(value, "report must be an object.");
  assertExactKeys(report, REPORT_KEYS, "report contains an unknown field.");

  const summary = requireStringArray(report.summary, "report.summary must be a string array.");
  if (summary.length === 0 || summary.some((item) => item.trim() === "")) {
    invalidRequest("report.summary must contain at least one nonempty string.");
  }

  const parsed: ReportPayload = { summary };
  if ("project" in report) parsed.project = requireString(report.project, "report.project must be a string.");
  if ("context" in report) parsed.context = parseContext(report.context);
  for (const key of ["changed_files", "decisions", "blockers", "risks", "next_steps"] as const) {
    if (key in report) parsed[key] = requireStringArray(report[key], `report.${key} must be a string array.`);
  }
  if ("verification" in report) parsed.verification = parseVerification(report.verification);
  return parsed;
}

function parseMetadata(value: unknown): Record<string, unknown> {
  const metadata = requireRecord(value, "metadata must be an object.");
  assertExactKeys(metadata, METADATA_KEYS, "metadata contains an unknown field.");

  const parsed: Record<string, unknown> = {};
  for (const key of ["client", "repository", "branch", "timestamp"] as const) {
    if (key in metadata) parsed[key] = requireString(metadata[key], `metadata.${key} must be a string.`);
  }
  return parsed;
}

export function parseTurnInput(value: unknown): TurnInput {
  const input = requireRecord(value, "Request body must be an object.");
  assertNoReservedKeys(input);

  const turnId = requireString(input.turn_id, "turn_id must be a UUID.");
  if (!TURN_ID.test(turnId)) {
    invalidRequest("turn_id must be a UUID.");
  }

  const kind = requireString(input.kind, "kind must be report or reply.");
  if (kind === "report") {
    assertExactKeys(input, new Set(["turn_id", "kind", "report", "metadata"]), "Request contains an unknown field.");
    return {
      turn_id: turnId,
      kind,
      report: parseReport(input.report),
      metadata: parseMetadata(input.metadata),
    };
  }
  if (kind === "reply") {
    assertExactKeys(input, new Set(["turn_id", "kind", "reply"]), "Request contains an unknown field.");
    return { turn_id: turnId, kind, reply: requireString(input.reply, "reply must be a string.") };
  }
  return invalidRequest("kind must be report or reply.");
}

type CanonicalValue =
  | string
  | number
  | boolean
  | null
  | CanonicalValue[]
  | { [key: string]: CanonicalValue };

function canonicalValue(value: unknown): CanonicalValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalValue(value[key])]),
    );
  }
  throw new TypeError("Validated turn input must contain JSON values.");
}

export function canonicalizeTurnInput(input: TurnInput): string {
  return JSON.stringify(canonicalValue(input));
}

export function digestTurnInput(input: TurnInput): string {
  return createHash("sha256")
    .update(canonicalizeTurnInput(input), "utf8")
    .digest("hex");
}

function isCaptainStatus(value: unknown): value is CaptainStatus {
  return typeof value === "string" && CAPTAIN_STATUSES.has(value as CaptainStatus);
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value)
    && value.length <= MAX_CAPTAIN_RESULT_ITEMS
    && value.every(
      (item) => typeof item === "string" && item.length <= MAX_CAPTAIN_RESULT_STRING_LENGTH,
    )
  );
}

function parseClickUpUpdates(value: unknown): ClickUpUpdate[] | undefined {
  if (!Array.isArray(value) || value.length > MAX_CAPTAIN_RESULT_ITEMS) return undefined;
  const updates: ClickUpUpdate[] = [];
  for (const item of value) {
    if (!isRecord(item) || Object.keys(item).some((key) => !CLICKUP_UPDATE_KEYS.has(key))) {
      return undefined;
    }
    if (
      typeof item.action !== "string"
      || item.action.length > MAX_CAPTAIN_RESULT_STRING_LENGTH
      || typeof item.task_id !== "string"
      || item.task_id.length > MAX_CAPTAIN_RESULT_STRING_LENGTH
    ) return undefined;
    updates.push({ action: item.action, task_id: item.task_id });
  }
  return updates;
}

function malformedCaptainResult(reportId: string): CaptainResult {
  return {
    report_id: reportId,
    status: "unknown_outcome",
    clickup_updates: [],
    captain_feedback: "Captain returned a malformed result.",
    questions: [],
    warnings: ["Captain result was malformed."],
  };
}

export function normalizeCaptainResult(reportId: string, value: unknown): CaptainResult {
  if (!isRecord(value) || Object.keys(value).some((key) => !RESULT_KEYS.has(key))) {
    return malformedCaptainResult(reportId);
  }
  if (
    value.report_id !== reportId
    || !isCaptainStatus(value.status)
    || typeof value.captain_feedback !== "string"
    || value.captain_feedback.length > MAX_CAPTAIN_RESULT_STRING_LENGTH
    || !isStringArray(value.questions)
    || !isStringArray(value.warnings)
  ) {
    return malformedCaptainResult(reportId);
  }
  const clickupUpdates = parseClickUpUpdates(value.clickup_updates);
  if (!clickupUpdates) return malformedCaptainResult(reportId);

  return {
    report_id: reportId,
    status: value.status,
    clickup_updates: clickupUpdates,
    captain_feedback: value.captain_feedback,
    questions: [...value.questions],
    warnings: [...value.warnings],
  };
}
