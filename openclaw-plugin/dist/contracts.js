import { createHash } from "node:crypto";
export class HttpProblem extends Error {
    status;
    code;
    constructor(status, code, message) {
        super(message);
        this.status = status;
        this.code = code;
        this.name = "HttpProblem";
    }
}
const TURN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_VALIDATION_NODES = 1_024;
const MAX_CAPTAIN_RESULT_STRING_LENGTH = 4_096;
const MAX_CAPTAIN_RESULT_ITEMS = 32;
const CAPTAIN_STATUSES = new Set([
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
function invalidRequest(message) {
    throw new HttpProblem(400, "INVALID_REQUEST", message);
}
function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function requireRecord(value, message) {
    if (!isRecord(value)) {
        invalidRequest(message);
    }
    return value;
}
function requireString(value, message) {
    if (typeof value !== "string") {
        invalidRequest(message);
    }
    return value;
}
function requireStringArray(value, message) {
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
        invalidRequest(message);
    }
    return [...value];
}
function assertExactKeys(value, allowed, message) {
    if (Object.keys(value).some((key) => !allowed.has(key))) {
        invalidRequest(message);
    }
}
function normalizeKey(key) {
    return key
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
        .toLowerCase()
        .split(/[^a-z0-9]+/)
        .filter(Boolean);
}
function findReservedKey(value) {
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
            for (const nested of current)
                values.push(nested);
            continue;
        }
        if (!isRecord(current))
            continue;
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
function assertNoReservedKeys(value) {
    if (findReservedKey(value)) {
        invalidRequest("Request contains a reserved field.");
    }
}
function parseContext(value) {
    const context = requireRecord(value, "report.context must be an object.");
    assertExactKeys(context, CONTEXT_KEYS, "report.context contains an unknown field.");
    const parsed = {};
    for (const key of ["git_root", "cwd", "branch", "upstream", "status", "diff_stat"]) {
        if (key in context)
            parsed[key] = requireString(context[key], `report.context.${key} must be a string.`);
    }
    if ("recent_commits" in context) {
        parsed.recent_commits = requireStringArray(context.recent_commits, "report.context.recent_commits must be a string array.");
    }
    return parsed;
}
function parseVerification(value) {
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
function parseReport(value) {
    const report = requireRecord(value, "report must be an object.");
    assertExactKeys(report, REPORT_KEYS, "report contains an unknown field.");
    const summary = requireStringArray(report.summary, "report.summary must be a string array.");
    if (summary.length === 0 || summary.some((item) => item.trim() === "")) {
        invalidRequest("report.summary must contain at least one nonempty string.");
    }
    const parsed = { summary };
    if ("project" in report)
        parsed.project = requireString(report.project, "report.project must be a string.");
    if ("context" in report)
        parsed.context = parseContext(report.context);
    for (const key of ["changed_files", "decisions", "blockers", "risks", "next_steps"]) {
        if (key in report)
            parsed[key] = requireStringArray(report[key], `report.${key} must be a string array.`);
    }
    if ("verification" in report)
        parsed.verification = parseVerification(report.verification);
    return parsed;
}
function parseMetadata(value) {
    const metadata = requireRecord(value, "metadata must be an object.");
    assertExactKeys(metadata, METADATA_KEYS, "metadata contains an unknown field.");
    const parsed = {};
    for (const key of ["client", "repository", "branch", "timestamp"]) {
        if (key in metadata)
            parsed[key] = requireString(metadata[key], `metadata.${key} must be a string.`);
    }
    return parsed;
}
export function parseTurnInput(value) {
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
function canonicalValue(value) {
    if (value === null || typeof value === "string" || typeof value === "boolean")
        return value;
    if (typeof value === "number" && Number.isFinite(value))
        return value;
    if (Array.isArray(value))
        return value.map(canonicalValue);
    if (isRecord(value)) {
        return Object.fromEntries(Object.keys(value)
            .sort()
            .map((key) => [key, canonicalValue(value[key])]));
    }
    throw new TypeError("Validated turn input must contain JSON values.");
}
export function canonicalizeTurnInput(input) {
    return JSON.stringify(canonicalValue(input));
}
export function digestTurnInput(input) {
    return createHash("sha256")
        .update(canonicalizeTurnInput(input), "utf8")
        .digest("hex");
}
function isCaptainStatus(value) {
    return typeof value === "string" && CAPTAIN_STATUSES.has(value);
}
function isStringArray(value) {
    return (Array.isArray(value)
        && value.length <= MAX_CAPTAIN_RESULT_ITEMS
        && value.every((item) => typeof item === "string" && item.length <= MAX_CAPTAIN_RESULT_STRING_LENGTH));
}
function parseClickUpUpdates(value) {
    if (!Array.isArray(value) || value.length > MAX_CAPTAIN_RESULT_ITEMS)
        return undefined;
    const updates = [];
    for (const item of value) {
        if (!isRecord(item) || Object.keys(item).some((key) => !CLICKUP_UPDATE_KEYS.has(key))) {
            return undefined;
        }
        if (typeof item.action !== "string"
            || item.action.length > MAX_CAPTAIN_RESULT_STRING_LENGTH
            || typeof item.task_id !== "string"
            || item.task_id.length > MAX_CAPTAIN_RESULT_STRING_LENGTH)
            return undefined;
        updates.push({ action: item.action, task_id: item.task_id });
    }
    return updates;
}
function malformedCaptainResult(reportId) {
    return {
        report_id: reportId,
        status: "unknown_outcome",
        clickup_updates: [],
        captain_feedback: "Captain returned a malformed result.",
        questions: [],
        warnings: ["Captain result was malformed."],
    };
}
export function normalizeCaptainResult(reportId, value) {
    if (!isRecord(value) || Object.keys(value).some((key) => !RESULT_KEYS.has(key))) {
        return malformedCaptainResult(reportId);
    }
    if (value.report_id !== reportId
        || !isCaptainStatus(value.status)
        || typeof value.captain_feedback !== "string"
        || value.captain_feedback.length > MAX_CAPTAIN_RESULT_STRING_LENGTH
        || !isStringArray(value.questions)
        || !isStringArray(value.warnings)) {
        return malformedCaptainResult(reportId);
    }
    const clickupUpdates = parseClickUpUpdates(value.clickup_updates);
    if (!clickupUpdates)
        return malformedCaptainResult(reportId);
    return {
        report_id: reportId,
        status: value.status,
        clickup_updates: clickupUpdates,
        captain_feedback: value.captain_feedback,
        questions: [...value.questions],
        warnings: [...value.warnings],
    };
}
