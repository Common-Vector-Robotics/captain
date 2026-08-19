import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { AuditLog } from "../src/audit.js";
import {
  canonicalizeTurnInput,
  digestTurnInput,
  type CaptainResult,
  type TurnInput,
} from "../src/contracts.js";
import { issueMemberToken } from "../src/security.js";
import { CaptainRemoteStore } from "../src/store.js";

const temporaryDirectories: string[] = [];

afterEach(() => {
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
    const replacement = issueMemberToken();
    store.rotateMember(member.memberId, replacement);
    store.revokeMember(member.memberId);
    store.close();

    const events = readEvents(auditPath);
    expect(events.map((event) => event.event)).toEqual([
      "member_created",
      "turn_queued",
      "turn_started",
      "turn_succeeded",
      "member_rotated",
      "member_revoked",
    ]);
    expect(events.slice(1, 4).map((event) => [event.from_state, event.to_state]))
      .toEqual([
        [null, "queued"],
        ["queued", "started"],
        ["started", "succeeded"],
      ]);
    expect(events[2].duration_ms).toEqual(expect.any(Number));
    expect(events[3].duration_ms).toEqual(expect.any(Number));

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
    expect(events[4]).toMatchObject({ member_id: member.memberId, code: null });
    expect(events[5]).toMatchObject({ member_id: member.memberId, code: null });
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
});
