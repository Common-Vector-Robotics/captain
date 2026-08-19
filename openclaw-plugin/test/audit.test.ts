import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { afterEach, describe, expect, it } from "vitest";

import { AuditLog, type AuditEvent, type AuditSink } from "../src/audit.js";
import {
  canonicalizeTurnInput,
  digestTurnInput,
  type CaptainResult,
  type TurnInput,
} from "../src/contracts.js";
import { issueMemberToken } from "../src/security.js";
import { CaptainRemoteStore } from "../src/store.js";

const temporaryDirectories: string[] = [];
const stores: CaptainRemoteStore[] = [];

afterEach(() => {
  for (const store of stores.splice(0)) store.close();
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function temporaryPath(name: string): string {
  const directory = mkdtempSync(join(tmpdir(), "captain-audit-test-"));
  temporaryDirectories.push(directory);
  return join(directory, name);
}

function readEvents(path: string): Array<Record<string, unknown>> {
  return readFileSync(path, "utf8")
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line) as Record<string, unknown>);
}

const AUDIT_KEYS = [
  "event_id",
  "timestamp",
  "event",
  "member_id",
  "operation",
  "route",
  "report_id",
  "turn_id",
  "from_state",
  "to_state",
  "duration_ms",
  "code",
  "count",
].sort();

class ToggleAuditProjection implements AuditSink {
  fail = false;
  readonly appended: AuditEvent[] = [];

  initialize(): void {}

  append(value: AuditEvent): void {
    if (this.fail) throw new Error("audit projection unavailable");
    this.appended.push(value);
  }

  close(): void {}
}

function storeWithProjection(projection: ToggleAuditProjection): {
  path: string;
  store: CaptainRemoteStore;
} {
  const path = temporaryPath("captain.sqlite3");
  const store = new CaptainRemoteStore(path, {
    auditLog: projection,
  });
  store.initialize();
  stores.push(store);
  return { path, store };
}

function pendingAuditEvents(path: string): number {
  const database = new DatabaseSync(path);
  try {
    return (database.prepare(`
      SELECT COUNT(*) AS count FROM audit_outbox WHERE delivered_at IS NULL
    `).get() as { count: number }).count;
  } finally {
    database.close();
  }
}

function rejectAuditInserts(path: string, reject: boolean): void {
  const database = new DatabaseSync(path);
  try {
    if (reject) {
      database.exec(`
        CREATE TRIGGER reject_audit_insert
        BEFORE INSERT ON audit_outbox
        BEGIN
          SELECT RAISE(FAIL, 'audit outbox unavailable');
        END;
      `);
    } else {
      database.exec("DROP TRIGGER reject_audit_insert");
    }
  } finally {
    database.close();
  }
}

function queuedReport(store: CaptainRemoteStore, memberId: string, turnId: string) {
  const input: TurnInput = {
    turn_id: turnId,
    kind: "report",
    report: { summary: ["private-report-marker"] },
    metadata: {},
  };
  return store.reserveTurn({
    memberId,
    reportId: "report-1",
    turnId,
    requestDigest: digestTurnInput(input),
    payloadJson: canonicalizeTurnInput(input),
  });
}

describe("Captain credential-free audit log", () => {
  it("appends durable fixed-shape events in order with owner-only mode", () => {
    const path = temporaryPath("captain-remote.audit.jsonl");
    let timestamp = 0;
    const audit = new AuditLog(path, {
      now: () => new Date(timestamp++).toISOString(),
    });
    audit.initialize();
    audit.record({
      event: "member_created",
      memberId: "00000000-0000-4000-8000-000000000001",
      operation: "member",
      route: "local_cli",
    });
    audit.record({
      event: "turn_started",
      memberId: "00000000-0000-4000-8000-000000000001",
      operation: "turn",
      route: "worker",
      reportId: "report-1",
      turnId: "00000000-0000-4000-8000-000000000002",
      fromState: "queued",
      toState: "started",
      durationMs: 12,
    });
    audit.close();

    const events = readEvents(path);
    expect(events.map((event) => event.event)).toEqual([
      "member_created",
      "turn_started",
    ]);
    expect(events.every((event) => (
      JSON.stringify(Object.keys(event).sort()) === JSON.stringify(AUDIT_KEYS)
    ))).toBe(true);
    expect(events[0]).toMatchObject({
      member_id: "00000000-0000-4000-8000-000000000001",
      operation: "member",
      route: "local_cli",
      report_id: null,
      count: null,
    });
    expect(events[1]).toMatchObject({
      from_state: "queued",
      to_state: "started",
      duration_ms: 12,
    });
    expect(statSync(path).mode & 0o777).toBe(0o600);
  });

  it("aggregates only fixed authentication, polling, and job abuse kinds", () => {
    const path = temporaryPath("captain-remote.audit.jsonl");
    const audit = new AuditLog(path);
    audit.initialize();
    audit.recordLimitSummary({
      auth_failed: 500,
      auth_rate_limited: 450,
      poll_rate_limited: 300,
      job_rate_limited: 200,
      "client-controlled-marker": 9_999,
    });
    audit.close();

    const events = readEvents(path);
    expect(events).toHaveLength(4);
    expect(events.map((event) => [event.code, event.count])).toEqual([
      ["AUTH_FAILED", 500],
      ["AUTH_RATE_LIMITED", 450],
      ["POLL_RATE_LIMITED", 300],
      ["JOB_RATE_LIMITED", 200],
    ]);
    expect(readFileSync(path, "utf8")).not.toContain("client-controlled-marker");
  });

  it("records member lifecycle and turn transitions without credential or content fields", () => {
    const databasePath = temporaryPath("captain.sqlite3");
    const auditPath = `${databasePath}.audit.jsonl`;
    const store = new CaptainRemoteStore(databasePath);
    store.initialize();
    const first = issueMemberToken();
    const member = store.createMember("Alice", "alice@example.com", first);
    const input: TurnInput = {
      turn_id: "00000000-0000-4000-8000-000000000010",
      kind: "report",
      report: { summary: ["private-report-marker"] },
      metadata: {},
    };
    store.reserveTurn({
      memberId: member.memberId,
      reportId: "report-1",
      turnId: input.turn_id,
      requestDigest: digestTurnInput(input),
      payloadJson: canonicalizeTurnInput(input),
    });
    store.claimNextTurn(1);
    const result: CaptainResult = {
      report_id: "report-1",
      status: "updated",
      clickup_updates: [],
      captain_feedback: "private-result-marker",
      questions: [],
      warnings: [],
    };
    store.finishTurn({
      memberId: member.memberId,
      reportId: "report-1",
      turnId: input.turn_id,
    }, "succeeded", result);
    const replacement = store.prepareMemberRotation(member.memberId);
    store.rotateMember(member.memberId, replacement);
    store.revokeMember(member.memberId);
    store.close();

    const events = readEvents(auditPath);
    expect(events.map((event) => event.event)).toEqual([
      "member_created",
      "turn_queued",
      "submit_authenticated",
      "turn_started",
      "turn_succeeded",
      "member_rotated",
      "member_revoked",
    ]);
    expect([events[1], events[3], events[4]].map((event) => [
      event.from_state,
      event.to_state,
    ]))
      .toEqual([
        [null, "queued"],
        ["queued", "started"],
        ["started", "succeeded"],
      ]);
    expect(events[3].duration_ms).toEqual(expect.any(Number));
    expect(events[4].duration_ms).toEqual(expect.any(Number));

    const text = readFileSync(auditPath, "utf8");
    for (const forbidden of [
      first.token,
      first.lookupId,
      first.secret,
      first.digest.toString("hex"),
      replacement.token,
      replacement.lookupId,
      replacement.secret,
      replacement.digest.toString("hex"),
      "Authorization",
      "private-report-marker",
      "private-result-marker",
      "sessionId",
      "runId",
      "sourceIp",
      "reply",
      "stack",
    ]) {
      expect(text).not.toContain(forbidden);
    }
    expect(events[5]).toMatchObject({ member_id: member.memberId, code: null });
    expect(events[6]).toMatchObject({ member_id: member.memberId, code: null });
  });

  it("records restart recovery as started to unknown outcome without run metadata", () => {
    const databasePath = temporaryPath("captain.sqlite3");
    const auditPath = `${databasePath}.audit.jsonl`;
    const store = new CaptainRemoteStore(databasePath);
    store.initialize();
    const member = store.createMember("Alice", "alice@example.com", issueMemberToken());
    const input: TurnInput = {
      turn_id: "00000000-0000-4000-8000-000000000011",
      kind: "reply",
      reply: "private-reply-marker",
    };
    store.reserveTurn({
      memberId: member.memberId,
      reportId: "report-1",
      turnId: input.turn_id,
      requestDigest: digestTurnInput(input),
      payloadJson: canonicalizeTurnInput(input),
    });
    store.claimNextTurn(1);
    expect(store.recoverStartedTurns()).toBe(1);
    store.close();

    const recovered = readEvents(auditPath).at(-1);
    expect(recovered).toMatchObject({
      event: "turn_unknown_outcome",
      from_state: "started",
      to_state: "unknown_outcome",
      code: "RESTART_RECOVERY",
    });
    expect(readFileSync(auditPath, "utf8")).not.toContain("private-reply-marker");
  });

  it("returns a created member when JSONL projection fails after commit", () => {
    const projection = new ToggleAuditProjection();
    projection.fail = true;
    const { path, store } = storeWithProjection(projection);

    const member = store.createMember("Alice", "alice@example.com", issueMemberToken());

    expect(store.listMembers()).toEqual([member]);
    expect(pendingAuditEvents(path)).toBe(1);
  });

  it("keeps rotate and revoke semantics when JSONL projection fails", () => {
    const projection = new ToggleAuditProjection();
    const { path, store } = storeWithProjection(projection);
    const original = issueMemberToken();
    const member = store.createMember("Alice", "alice@example.com", original);
    const replacement = store.prepareMemberRotation(member.memberId);
    projection.fail = true;

    expect(() => store.rotateMember(member.memberId, replacement)).not.toThrow();
    expect(store.findMemberForAuth(original.lookupId)?.digest).toEqual(replacement.digest);
    expect(() => store.revokeMember(member.memberId)).not.toThrow();
    expect(store.findMemberForAuth(original.lookupId)?.revokedAt).not.toBeNull();
    expect(pendingAuditEvents(path)).toBe(2);
  });

  it("keeps reserve and claim semantics when JSONL projection fails", () => {
    const projection = new ToggleAuditProjection();
    const { path, store } = storeWithProjection(projection);
    const member = store.createMember("Alice", "alice@example.com", issueMemberToken());
    projection.fail = true;

    const reserved = queuedReport(
      store,
      member.memberId,
      "00000000-0000-4000-8000-000000000020",
    );
    const claimed = store.claimNextTurn(1);

    expect(reserved.status).toBe("created");
    expect(claimed?.state).toBe("started");
    expect(store.getTurn(reserved.turn)?.state).toBe("started");
    expect(pendingAuditEvents(path)).toBe(3);
  });

  it("keeps finish semantics when JSONL projection fails", () => {
    const projection = new ToggleAuditProjection();
    const { path, store } = storeWithProjection(projection);
    const member = store.createMember("Alice", "alice@example.com", issueMemberToken());
    const reserved = queuedReport(
      store,
      member.memberId,
      "00000000-0000-4000-8000-000000000021",
    );
    store.claimNextTurn(1);
    projection.fail = true;

    expect(() => store.finishTurn(reserved.turn, "failed", undefined, {
      code: "CAPTAIN_FAILED",
      message: "Failed.",
    })).not.toThrow();

    expect(store.getTurn(reserved.turn)?.state).toBe("failed");
    expect(pendingAuditEvents(path)).toBe(1);
  });

  it("keeps restart recovery semantics when JSONL projection fails", () => {
    const projection = new ToggleAuditProjection();
    const { path, store } = storeWithProjection(projection);
    const member = store.createMember("Alice", "alice@example.com", issueMemberToken());
    const reserved = queuedReport(
      store,
      member.memberId,
      "00000000-0000-4000-8000-000000000022",
    );
    store.claimNextTurn(1);
    projection.fail = true;

    expect(store.recoverStartedTurns()).toBe(1);
    expect(store.getTurn(reserved.turn)?.state).toBe("unknown_outcome");
    expect(pendingAuditEvents(path)).toBe(1);
  });

  it("persists route operation and error audits before best-effort projection", () => {
    const projection = new ToggleAuditProjection();
    projection.fail = true;
    const { path, store } = storeWithProjection(projection);

    expect(() => store.recordAudit({
      event: "poll_authenticated",
      memberId: "00000000-0000-4000-8000-000000000001",
      operation: "poll",
      route: "poll",
      reportId: "report-1",
      turnId: "00000000-0000-4000-8000-000000000023",
      toState: "queued",
      code: "FOUND",
    })).not.toThrow();
    expect(() => store.recordAudit({
      event: "http_error",
      memberId: "00000000-0000-4000-8000-000000000001",
      operation: "submit",
      route: "submit",
      reportId: "report-1",
      code: "INVALID_JSON",
    })).not.toThrow();
    expect(pendingAuditEvents(path)).toBe(2);
  });

  it("retries pending projection on startup and orderly close", () => {
    const firstProjection = new ToggleAuditProjection();
    firstProjection.fail = true;
    const { path, store } = storeWithProjection(firstProjection);
    store.createMember("Alice", "alice@example.com", issueMemberToken());
    store.close();
    stores.splice(stores.indexOf(store), 1);

    const startupProjection = new ToggleAuditProjection();
    const reopened = new CaptainRemoteStore(path, {
      auditLog: startupProjection,
    });
    reopened.initialize();
    stores.push(reopened);
    expect(startupProjection.appended).toHaveLength(1);
    expect(pendingAuditEvents(path)).toBe(0);

    startupProjection.fail = true;
    reopened.recordAudit({
      event: "http_error",
      operation: "submit",
      route: "submit",
      code: "INVALID_JSON",
    });
    startupProjection.fail = false;
    reopened.close();
    stores.splice(stores.indexOf(reopened), 1);
    expect(startupProjection.appended).toHaveLength(2);
    expect(pendingAuditEvents(path)).toBe(0);
  });

  it("rolls back member state when its transactional outbox insert fails", () => {
    const projection = new ToggleAuditProjection();
    const { path, store } = storeWithProjection(projection);
    rejectAuditInserts(path, true);

    expect(() => store.createMember(
      "Alice",
      "alice@example.com",
      issueMemberToken(),
    )).toThrow();
    expect(store.listMembers()).toEqual([]);
    expect(pendingAuditEvents(path)).toBe(0);
  });

  it("rolls back every turn transition when its outbox insert fails", () => {
    const projection = new ToggleAuditProjection();
    const { path, store } = storeWithProjection(projection);
    const member = store.createMember("Alice", "alice@example.com", issueMemberToken());
    const turnId = "00000000-0000-4000-8000-000000000024";

    rejectAuditInserts(path, true);
    expect(() => queuedReport(store, member.memberId, turnId)).toThrow();
    expect(store.getTurn({ memberId: member.memberId, reportId: "report-1", turnId }))
      .toBeNull();

    rejectAuditInserts(path, false);
    const reserved = queuedReport(store, member.memberId, turnId);
    rejectAuditInserts(path, true);
    expect(() => store.claimNextTurn(1)).toThrow();
    expect(store.getTurn(reserved.turn)?.state).toBe("queued");

    rejectAuditInserts(path, false);
    store.claimNextTurn(1);
    rejectAuditInserts(path, true);
    expect(() => store.finishTurn(reserved.turn, "failed", undefined, {
      code: "CAPTAIN_FAILED",
      message: "Failed.",
    })).toThrow();
    expect(store.getTurn(reserved.turn)?.state).toBe("started");
    expect(() => store.recoverStartedTurns()).toThrow();
    expect(store.getTurn(reserved.turn)?.state).toBe("started");
  });

  it("rolls back rotate and revoke when their outbox inserts fail", () => {
    const projection = new ToggleAuditProjection();
    const { path, store } = storeWithProjection(projection);
    const original = issueMemberToken();
    const member = store.createMember("Alice", "alice@example.com", original);
    const replacement = store.prepareMemberRotation(member.memberId);

    rejectAuditInserts(path, true);
    expect(() => store.rotateMember(member.memberId, replacement)).toThrow();
    expect(store.findMemberForAuth(original.lookupId)?.digest).toEqual(original.digest);

    rejectAuditInserts(path, false);
    store.rotateMember(member.memberId, replacement);
    rejectAuditInserts(path, true);
    expect(() => store.revokeMember(member.memberId)).toThrow();
    expect(store.findMemberForAuth(original.lookupId)?.revokedAt).toBeNull();
  });

  it("uses a stable event ID when delivery marking fails and projection retries", () => {
    const databasePath = temporaryPath("captain.sqlite3");
    const auditPath = `${databasePath}.audit.jsonl`;
    const store = new CaptainRemoteStore(databasePath);
    store.initialize();
    stores.push(store);
    const database = new DatabaseSync(databasePath);
    database.exec(`
      CREATE TRIGGER reject_audit_delivery
      BEFORE UPDATE OF delivered_at ON audit_outbox
      BEGIN
        SELECT RAISE(FAIL, 'audit delivery mark unavailable');
      END;
    `);
    database.close();

    const member = store.createMember("Alice", "alice@example.com", issueMemberToken());
    expect(member.name).toBe("Alice");
    expect(pendingAuditEvents(databasePath)).toBe(1);
    const first = readEvents(auditPath);
    expect(first).toHaveLength(1);

    const repair = new DatabaseSync(databasePath);
    repair.exec("DROP TRIGGER reject_audit_delivery");
    repair.close();
    store.close();
    stores.splice(stores.indexOf(store), 1);

    const projected = readEvents(auditPath);
    expect(projected).toHaveLength(2);
    expect(projected[0].event_id).toBe(projected[1].event_id);
    expect(pendingAuditEvents(databasePath)).toBe(0);
  });
});
