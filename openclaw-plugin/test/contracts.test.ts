import { describe, expect, it } from "vitest";
import {
  HttpProblem,
  canonicalizeTurnInput,
  digestTurnInput,
  normalizeCaptainResult,
  parseTurnInput,
} from "../src/contracts.js";

const report = {
  project: "Captain",
  context: { branch: "codex/captain-remote-adapter" },
  summary: ["Added remote access."],
  changed_files: ["openclaw-plugin/src/index.ts"],
  verification: [{ command: "npm test", result: "pass" }],
  decisions: [], blockers: [], risks: [], next_steps: [],
};

it("accepts the strict report and reply unions", () => {
  expect(parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report", report, metadata: { client: "codex" },
  }).kind).toBe("report");
  expect(parseTurnInput({
    turn_id: "204156a1-c515-41f6-8f2f-a1d24a312704",
    kind: "reply", reply: "Yes, Friday is correct.",
  }).kind).toBe("reply");
});

it("rejects runtime selection and unknown fields", () => {
  expect(() => parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report", report, metadata: {}, tools: ["exec"],
  })).toThrowError(HttpProblem);
});

it("rejects nested identity fields", () => {
  expect(() => parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report", report,
    metadata: { nested: { authenticatedEmail: "fake@example.com" } },
  })).toThrow(/reserved/i);
});

it("rejects every client-controlled execution field at any depth", () => {
  for (const key of [
    "auth", "authentication", "authorization", "identity", "claims", "token",
    "agent", "session", "model", "workspace", "tool", "thinking", "runtime",
  ]) {
    expect(() => parseTurnInput({
      turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
      kind: "report",
      report,
      metadata: { safe: { [key]: "client-controlled" } },
    })).toThrow(/reserved/i);
  }
});

it("rejects a client-supplied host session identifier", () => {
  expect(() => parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report", report, metadata: { host_session_id: "client-selected" },
  })).toThrow(/reserved/i);
});

it("rejects malformed identifiers and report shapes", () => {
  expect(() => parseTurnInput({
    turn_id: "not-a-uuid", kind: "reply", reply: "Yes",
  })).toThrowError(HttpProblem);
  expect(() => parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report", report: { ...report, summary: [] }, metadata: {},
  })).toThrowError(HttpProblem);
  expect(() => parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report", report: { ...report, unexpected: true }, metadata: {},
  })).toThrowError(HttpProblem);
});

it("canonicalizes key order and hashes semantic duplicates identically", () => {
  const a = parseTurnInput({ turn_id: "204156a1-c515-41f6-8f2f-a1d24a312704", kind: "reply", reply: "Yes" });
  const b = parseTurnInput({ reply: "Yes", kind: "reply", turn_id: "204156a1-c515-41f6-8f2f-a1d24a312704" });
  expect(canonicalizeTurnInput(a)).toBe(canonicalizeTurnInput(b));
  expect(digestTurnInput(a)).toBe(digestTurnInput(b));
});

it("preserves array order in the canonical request", () => {
  const first = parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report",
    report: { ...report, summary: ["first", "second"] },
    metadata: {},
  });
  const second = parseTurnInput({
    turn_id: "b73db2fe-ec74-4f44-a74c-fbe44eb11e46",
    kind: "report",
    report: { ...report, summary: ["second", "first"] },
    metadata: {},
  });
  expect(digestTurnInput(first)).not.toBe(digestTurnInput(second));
});

it("normalizes the public Captain result and bounds malformed output", () => {
  expect(normalizeCaptainResult("report-1", {
    report_id: "report-1", status: "updated", clickup_updates: [],
    captain_feedback: "Updated the task.", questions: [], warnings: [],
  }).status).toBe("updated");
  const malformed = normalizeCaptainResult("report-1", "x".repeat(5000));
  expect(malformed.status).toBe("unknown_outcome");
  expect(malformed.warnings[0].length).toBeLessThanOrEqual(260);
});
