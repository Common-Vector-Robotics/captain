import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  canonicalizeTurnInput,
  digestTurnInput,
  type CaptainResult,
  type TurnInput,
} from "../src/contracts.js";
import {
  CaptainTurnWorker,
  collectEmbeddedText,
  type EmbeddedAgentRunResult,
  type EmbeddedCaptainRuntime,
} from "../src/runtime.js";
import { issueMemberToken } from "../src/security.js";
import { CaptainRemoteStore, type StoredMember } from "../src/store.js";

type RunParams = Parameters<EmbeddedCaptainRuntime["run"]>[0];

interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
}

const temporaryDirectories: string[] = [];
const stores: CaptainRemoteStore[] = [];
const workers: CaptainTurnWorker[] = [];

afterEach(async () => {
  await Promise.all(workers.splice(0).map((worker) => worker.stop()));
  for (const store of stores.splice(0)) store.close();
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function openStore(): CaptainRemoteStore {
  const directory = mkdtempSync(join(tmpdir(), "captain-runtime-test-"));
  temporaryDirectories.push(directory);
  const store = new CaptainRemoteStore(join(directory, "captain.sqlite"));
  store.initialize();
  stores.push(store);
  return store;
}

function createMember(store: CaptainRemoteStore, name: string): StoredMember {
  const email = `${name.toLowerCase().replaceAll(" ", ".")}@example.com`;
  return store.createMember(name, email, issueMemberToken());
}

function turnId(index: number): string {
  return `00000000-0000-4000-8000-${index.toString(16).padStart(12, "0")}`;
}

function reportTurn(index: number, summary = `Report ${index}`): TurnInput {
  return {
    turn_id: turnId(index),
    kind: "report",
    report: { summary: [summary] },
    metadata: { client: "client-secret-marker" },
  };
}

function reserve(
  store: CaptainRemoteStore,
  member: StoredMember,
  reportId: string,
  input: TurnInput,
): void {
  store.reserveTurn({
    memberId: member.memberId,
    reportId,
    turnId: input.turn_id,
    requestDigest: digestTurnInput(input),
    payloadJson: canonicalizeTurnInput(input),
  });
}

function captainResult(reportId: string, status: CaptainResult["status"] = "updated"): CaptainResult {
  return {
    report_id: reportId,
    status,
    clickup_updates: [],
    captain_feedback: status === "failed" ? "Could not match the task." : "Recorded.",
    questions: [],
    warnings: [],
  };
}

function embeddedResult(
  text?: string,
  meta: EmbeddedAgentRunResult["meta"] = { durationMs: 1 },
): EmbeddedAgentRunResult {
  return {
    meta,
    payloads: text === undefined ? [] : [{ text }],
  };
}

function createRuntime(
  run: EmbeddedCaptainRuntime["run"],
  resolveWorkspace = () => "/captain/workspace",
): EmbeddedCaptainRuntime {
  return { resolveWorkspace, run };
}

function createWorker(
  store: CaptainRemoteStore,
  runtime: EmbeddedCaptainRuntime,
  maxGlobalRunningTurns = 4,
): CaptainTurnWorker {
  const worker = new CaptainTurnWorker({
    store,
    runtime,
    timeoutMs: 300_000,
    maxGlobalRunningTurns,
  });
  workers.push(worker);
  return worker;
}

async function waitFor(check: () => boolean, message = "condition"): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (!check()) {
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for ${message}.`);
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 1));
  }
}

describe("Captain embedded runtime boundary", () => {
  it("runs an authenticated report only through the fixed Captain parameters", async () => {
    const store = openStore();
    const alice = createMember(store, "Alice Admin");
    const input = reportTurn(1, "Implemented durable execution.");
    const reportId = "report-fixed";
    reserve(store, alice, reportId, input);
    const captured: RunParams[] = [];
    const expected = captainResult(reportId);
    const runtime = createRuntime(async (params) => {
      captured.push(params);
      return embeddedResult(JSON.stringify(expected));
    });
    const worker = createWorker(store, runtime);

    worker.start();
    await waitFor(
      () => store.getTurn({ memberId: alice.memberId, reportId, turnId: input.turn_id })?.state === "succeeded",
      "successful report turn",
    );

    expect(captured).toHaveLength(1);
    expect(captured[0].agentId).toBe("captain");
    const storedReport = store.reserveTurn({
      memberId: alice.memberId,
      reportId,
      turnId: input.turn_id,
      requestDigest: digestTurnInput(input),
      payloadJson: canonicalizeTurnInput(input),
    }).report;
    expect(captured[0].sessionId).toBe(storedReport.sessionId);
    expect(captured[0].sessionKey).toBe(storedReport.sessionId);
    expect(captured[0].workspaceDir).toBe("/captain/workspace");
    expect(captured[0].timeoutMs).toBe(300_000);
    expect(captured[0].runTimeoutOverrideMs).toBe(300_000);
    expect(captured[0].runId).toMatch(/^[0-9a-f-]{36}$/);
    expect(captured[0].trigger).toBe("user");
    expect(captured[0].abortSignal).toBeInstanceOf(AbortSignal);
    expect(captured[0]).not.toHaveProperty("provider");
    expect(captured[0]).not.toHaveProperty("model");
    expect(captured[0]).not.toHaveProperty("toolsAllow");
    expect(captured[0]).not.toHaveProperty("clientTools");
    expect(captured[0]).not.toHaveProperty("thinkLevel");

    expect(captured[0].prompt).toContain("Authenticated employee update");
    expect(captured[0].prompt).toContain("Alice Admin");
    expect(captured[0].prompt).toContain("alice.admin@example.com");
    expect(captured[0].prompt).toContain(JSON.stringify(input.report));
    expect(captured[0].prompt).toContain("captain_session_report");
    expect(captured[0].prompt).toContain("/captain");
    expect(captured[0].prompt).toContain("canonical Captain result JSON");
    expect(captured[0].prompt).not.toContain("client-secret-marker");

    expect(store.getTurn({ memberId: alice.memberId, reportId, turnId: input.turn_id }))
      .toMatchObject({ state: "succeeded", result: expected, error: null });
  });

  it("labels and includes an authenticated reply exactly", async () => {
    const store = openStore();
    const bob = createMember(store, "Bob");
    const reply = "Done.\nKeep the existing due date <Friday>.";
    const input: TurnInput = { turn_id: turnId(2), kind: "reply", reply };
    reserve(store, bob, "reply-report", input);
    let prompt = "";
    const runtime = createRuntime(async (params) => {
      prompt = params.prompt;
      return embeddedResult(JSON.stringify(captainResult("reply-report")));
    });

    createWorker(store, runtime).start();
    await waitFor(
      () => store.getTurn({ memberId: bob.memberId, reportId: "reply-report", turnId: input.turn_id })?.state === "succeeded",
      "successful reply turn",
    );

    expect(prompt).toContain("Authenticated employee reply");
    expect(prompt).toContain(reply);
    expect(prompt).toContain("Bob");
    expect(prompt).toContain("bob@example.com");
  });

  it("collects only visible assistant text from an embedded result", () => {
    const result: EmbeddedAgentRunResult = {
      meta: { durationMs: 1 },
      payloads: [
        { text: "reasoning", isReasoning: true },
        { text: "commentary", isCommentary: true },
        { text: "error", isError: true },
        { text: "first" },
        { mediaUrl: "https://example.invalid/file" },
        { text: "second" },
      ],
    };

    expect(collectEmbeddedText(result)).toBe("first\nsecond");
  });

  it("caps global execution at four running promises", async () => {
    const store = openStore();
    const gates: Deferred<unknown>[] = [];
    let running = 0;
    let highestRunning = 0;
    const runtime = createRuntime(async () => {
      running += 1;
      highestRunning = Math.max(highestRunning, running);
      const gate = deferred<unknown>();
      gates.push(gate);
      try {
        return await gate.promise;
      } finally {
        running -= 1;
      }
    });

    const keys = Array.from({ length: 6 }, (_, index) => {
      const member = createMember(store, `Member ${index}`);
      const input = reportTurn(index + 10);
      reserve(store, member, "shared-report", input);
      return { memberId: member.memberId, reportId: "shared-report", turnId: input.turn_id };
    });

    createWorker(store, runtime).start();
    await waitFor(() => gates.length === 4, "four concurrent runs");

    expect(highestRunning).toBe(4);
    expect(keys.filter((key) => store.getTurn(key)?.state === "started")).toHaveLength(4);
    expect(keys.filter((key) => store.getTurn(key)?.state === "queued")).toHaveLength(2);

    gates[0].resolve(embeddedResult(JSON.stringify(captainResult("shared-report"))));
    await waitFor(() => gates.length === 5, "next queued run");
    expect(highestRunning).toBe(4);

    for (const gate of gates) {
      gate.resolve(embeddedResult(JSON.stringify(captainResult("shared-report"))));
    }
    await waitFor(() => gates.length === 6, "last queued run");
    gates[5].resolve(embeddedResult(JSON.stringify(captainResult("shared-report"))));
    await waitFor(
      () => keys.every((key) => store.getTurn(key)?.state === "succeeded"),
      "all concurrent turns",
    );
  });

  it("keeps queued FIFO work waiting for capacity and drains it after completion", async () => {
    const store = openStore();
    const firstMember = createMember(store, "First");
    const secondMember = createMember(store, "Second");
    const first = reportTurn(20, "First queued report");
    const second = reportTurn(21, "Second queued report");
    reserve(store, firstMember, "first-report", first);
    reserve(store, secondMember, "second-report", second);
    const firstGate = deferred<unknown>();
    const prompts: string[] = [];
    const runtime = createRuntime(async (params) => {
      prompts.push(params.prompt);
      if (prompts.length === 1) return firstGate.promise;
      return embeddedResult(JSON.stringify(captainResult("second-report")));
    });

    createWorker(store, runtime, 1).start();
    await waitFor(() => prompts.length === 1, "first FIFO run");

    expect(prompts[0]).toContain("First queued report");
    expect(store.getTurn({ memberId: secondMember.memberId, reportId: "second-report", turnId: second.turn_id })?.state)
      .toBe("queued");

    firstGate.resolve(embeddedResult(JSON.stringify(captainResult("first-report"))));
    await waitFor(() => prompts.length === 2, "second FIFO run");
    expect(prompts[1]).toContain("Second queued report");
  });

  it("coalesces synchronous wake calls, contains drain errors, and never claims after stop", async () => {
    const store = openStore();
    const runtime = createRuntime(vi.fn());
    const claim = vi.spyOn(store, "claimNextTurn");
    const worker = createWorker(store, runtime);

    worker.start();
    await waitFor(() => claim.mock.calls.length === 1, "startup drain");

    for (let index = 0; index < 20; index += 1) worker.wake();
    await waitFor(() => claim.mock.calls.length === 2, "coalesced wake drain");
    expect(claim).toHaveBeenCalledTimes(2);

    claim.mockImplementationOnce(() => {
      throw new Error("transient store failure");
    });
    expect(() => worker.wake()).not.toThrow();
    await waitFor(() => claim.mock.calls.length === 3, "contained failed drain");

    await worker.stop();
    const member = createMember(store, "Stopped");
    const input = reportTurn(22);
    reserve(store, member, "stopped-report", input);
    worker.wake();
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));

    expect(claim).toHaveBeenCalledTimes(3);
    expect(store.getTurn({ memberId: member.memberId, reportId: "stopped-report", turnId: input.turn_id })?.state)
      .toBe("queued");
  });

  it("persists a structurally recognized embedded timeout", async () => {
    const store = openStore();
    const member = createMember(store, "Timeout");
    const input = reportTurn(30);
    reserve(store, member, "timeout-report", input);
    const run = vi.fn(async () => embeddedResult(undefined, {
      durationMs: 300_000,
      aborted: true,
      stopReason: "timeout",
      timeoutPhase: "provider",
    }));

    createWorker(store, createRuntime(run)).start();
    const key = { memberId: member.memberId, reportId: "timeout-report", turnId: input.turn_id };
    await waitFor(() => store.getTurn(key)?.state === "timed_out", "timed-out turn");

    expect(run).toHaveBeenCalledTimes(1);
    expect(store.getTurn(key)).toMatchObject({
      state: "timed_out",
      result: null,
      error: { code: "TIMED_OUT", message: "Captain turn timed out." },
    });
  });

  it("persists a thrown runner exception as an unknown outcome without exposing it", async () => {
    const store = openStore();
    const member = createMember(store, "Thrown");
    const input = reportTurn(31);
    reserve(store, member, "thrown-report", input);
    const run = vi.fn(async () => {
      throw new Error("sensitive provider stack and token");
    });

    createWorker(store, createRuntime(run)).start();
    const key = { memberId: member.memberId, reportId: "thrown-report", turnId: input.turn_id };
    await waitFor(() => store.getTurn(key)?.state === "unknown_outcome", "unknown thrown turn");

    expect(run).toHaveBeenCalledTimes(1);
    expect(store.getTurn(key)).toMatchObject({
      state: "unknown_outcome",
      result: null,
      error: { code: "UNKNOWN_OUTCOME", message: "Captain turn outcome is unknown." },
    });
    expect(JSON.stringify(store.getTurn(key))).not.toContain("sensitive provider");
  });

  it("persists malformed output as an unknown outcome", async () => {
    const store = openStore();
    const member = createMember(store, "Malformed");
    const input = reportTurn(32);
    reserve(store, member, "malformed-report", input);
    const run = vi.fn(async () => embeddedResult("not canonical JSON"));

    createWorker(store, createRuntime(run)).start();
    const key = { memberId: member.memberId, reportId: "malformed-report", turnId: input.turn_id };
    await waitFor(() => store.getTurn(key)?.state === "unknown_outcome", "unknown malformed turn");

    expect(run).toHaveBeenCalledTimes(1);
    expect(store.getTurn(key)).toMatchObject({
      state: "unknown_outcome",
      result: null,
      error: { code: "UNKNOWN_OUTCOME", message: "Captain turn outcome is unknown." },
    });
  });

  it("persists a canonical Captain failure as failed", async () => {
    const store = openStore();
    const member = createMember(store, "Failed");
    const input = reportTurn(33);
    const result = captainResult("failed-report", "failed");
    reserve(store, member, "failed-report", input);
    const run = vi.fn(async () => embeddedResult(JSON.stringify(result)));

    createWorker(store, createRuntime(run)).start();
    const key = { memberId: member.memberId, reportId: "failed-report", turnId: input.turn_id };
    await waitFor(() => store.getTurn(key)?.state === "failed", "definitive failed turn");

    expect(run).toHaveBeenCalledTimes(1);
    expect(store.getTurn(key)).toMatchObject({
      state: "failed",
      result,
      error: { code: "CAPTAIN_FAILED", message: "Captain could not complete the turn." },
    });
  });

  it("does not rerun a turn when completion persistence fails", async () => {
    const store = openStore();
    const member = createMember(store, "Persistence");
    const input = reportTurn(34);
    reserve(store, member, "persistence-report", input);
    const run = vi.fn(async () => embeddedResult(JSON.stringify(captainResult("persistence-report"))));
    const finish = vi.spyOn(store, "finishTurn").mockImplementationOnce(() => {
      throw new Error("disk unavailable");
    });

    const worker = createWorker(store, createRuntime(run), 1);
    worker.start();
    await waitFor(() => finish.mock.calls.length === 1, "failed completion persistence");
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));

    const key = { memberId: member.memberId, reportId: "persistence-report", turnId: input.turn_id };
    expect(store.getTurn(key)?.state).toBe("started");
    expect(run).toHaveBeenCalledTimes(1);
    worker.wake();
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("aborts and settles active turns on idempotent stop without closing the store", async () => {
    const store = openStore();
    const member = createMember(store, "Shutdown");
    const input = reportTurn(35);
    reserve(store, member, "shutdown-report", input);
    let signal: AbortSignal | undefined;
    const run = vi.fn((params: RunParams) => {
      signal = params.abortSignal;
      return new Promise((_resolve, reject) => {
        params.abortSignal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      });
    });
    const worker = createWorker(store, createRuntime(run));
    worker.start();
    await waitFor(() => signal !== undefined, "active abort signal");

    const firstStop = worker.stop();
    const secondStop = worker.stop();
    expect(secondStop).toBe(firstStop);
    await firstStop;

    const key = { memberId: member.memberId, reportId: "shutdown-report", turnId: input.turn_id };
    expect(signal?.aborted).toBe(true);
    expect(store.getTurn(key)).toMatchObject({
      state: "unknown_outcome",
      error: { code: "UNKNOWN_OUTCOME", message: "Captain turn outcome is unknown." },
    });
    expect(run).toHaveBeenCalledTimes(1);
    expect(store.listMembers()).toHaveLength(1);
  });

  it("recovers abandoned started work before draining durable queued work", async () => {
    const store = openStore();
    const abandonedMember = createMember(store, "Abandoned");
    const queuedMember = createMember(store, "Queued");
    const abandoned = reportTurn(36, "Already started before restart");
    const queued = reportTurn(37, "Still queued at restart");
    reserve(store, abandonedMember, "abandoned-report", abandoned);
    expect(store.claimNextTurn(4)?.turnId).toBe(abandoned.turn_id);
    reserve(store, queuedMember, "queued-report", queued);
    const run = vi.fn(async () => embeddedResult(JSON.stringify(captainResult("queued-report"))));

    createWorker(store, createRuntime(run)).start();
    const abandonedKey = {
      memberId: abandonedMember.memberId,
      reportId: "abandoned-report",
      turnId: abandoned.turn_id,
    };
    const queuedKey = {
      memberId: queuedMember.memberId,
      reportId: "queued-report",
      turnId: queued.turn_id,
    };
    await waitFor(() => store.getTurn(queuedKey)?.state === "succeeded", "queued restart turn");

    expect(store.getTurn(abandonedKey)).toMatchObject({
      state: "unknown_outcome",
      error: {
        code: "UNKNOWN_OUTCOME",
        message: "Captain turn outcome is unknown after restart.",
      },
    });
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("never invokes the runner twice for repeated wakes on one started turn", async () => {
    const store = openStore();
    const member = createMember(store, "Once");
    const input = reportTurn(38);
    reserve(store, member, "once-report", input);
    const gate = deferred<unknown>();
    const run = vi.fn(async () => gate.promise);
    const worker = createWorker(store, createRuntime(run));

    worker.start();
    await waitFor(() => run.mock.calls.length === 1, "single active run");
    for (let index = 0; index < 20; index += 1) worker.wake();
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));
    expect(run).toHaveBeenCalledTimes(1);

    gate.resolve(embeddedResult(JSON.stringify(captainResult("once-report"))));
    const key = { memberId: member.memberId, reportId: "once-report", turnId: input.turn_id };
    await waitFor(() => store.getTurn(key)?.state === "succeeded", "single completed run");
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("does not allow configured concurrency above the server maximum", () => {
    const store = openStore();
    const runtime = createRuntime(vi.fn());

    expect(() => new CaptainTurnWorker({
      store,
      runtime,
      timeoutMs: 300_000,
      maxGlobalRunningTurns: 5,
    })).toThrow("maxGlobalRunningTurns");
  });
});
