import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { Worker } from "node:worker_threads";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  canonicalizeTurnInput,
  digestTurnInput,
  type CaptainResult,
  type TurnInput,
} from "../src/contracts.js";
import { issueMemberToken } from "../src/security.js";
import {
  CaptainRemoteStore,
  type ReserveTurnInput,
  type StoredMember,
} from "../src/store.js";

const temporaryDirectories: string[] = [];
const stores: CaptainRemoteStore[] = [];

interface WorkerOperation {
  action: "reserve" | "claim";
  input?: ReserveTurnInput;
  maxRunning?: number;
}

interface WorkerResult {
  ok: boolean;
  value?: string | null;
  code?: string;
  message?: string;
}

const STORE_WORKER = `
  const { parentPort, workerData } = require("node:worker_threads");

  (async () => {
    const { CaptainRemoteStore } = await import(workerData.storeModuleUrl);
    const gate = new Int32Array(workerData.gate);
    const store = new CaptainRemoteStore(workerData.databasePath);
    Atomics.add(gate, 0, 1);
    Atomics.notify(gate, 0);
    Atomics.wait(gate, 1, 0);
    Atomics.add(gate, 2, 1);
    Atomics.notify(gate, 2);

    try {
      store.initialize();
      if (workerData.operation.action === "reserve") {
        const reserved = store.reserveTurn(workerData.operation.input);
        parentPort.postMessage({ ok: true, value: reserved.status });
      } else {
        const claimed = store.claimNextTurn(workerData.operation.maxRunning);
        parentPort.postMessage({ ok: true, value: claimed?.turnId ?? null });
      }
    } catch (error) {
      parentPort.postMessage({
        ok: false,
        code: error && typeof error === "object" && "code" in error ? String(error.code) : undefined,
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      store.close();
    }
  })().catch((error) => parentPort.postMessage({ ok: false, message: String(error) }));
`;

const LOCK_WORKER = `
  const { DatabaseSync } = require("node:sqlite");
  const { parentPort, workerData } = require("node:worker_threads");
  const gate = new Int32Array(workerData.gate);
  const database = new DatabaseSync(workerData.databasePath, { timeout: 5000 });
  database.exec("BEGIN IMMEDIATE");
  Atomics.store(gate, 3, 1);
  Atomics.notify(gate, 3);
  Atomics.wait(gate, 4, 0, 5000);
  database.exec("COMMIT");
  database.close();
  parentPort.postMessage("released");
`;

function createDatabasePath(): string {
  const directory = mkdtempSync(join(tmpdir(), "captain-remote-store-"));
  temporaryDirectories.push(directory);
  return join(directory, "data", "captain.sqlite");
}

function compileWorkerStore(): string {
  const directory = mkdtempSync(join(tmpdir(), "captain-remote-worker-"));
  temporaryDirectories.push(directory);
  writeFileSync(join(directory, "package.json"), '{"type":"module"}\n');
  const sources = ["contracts", "security", "store"]
    .map((name) => fileURLToPath(new URL(`../src/${name}.ts`, import.meta.url)));
  execFileSync(process.execPath, [
    fileURLToPath(new URL("../node_modules/typescript/lib/tsc.js", import.meta.url)),
    "--ignoreConfig",
    "--noCheck",
    "--target", "ES2022",
    "--module", "NodeNext",
    "--moduleResolution", "NodeNext",
    "--skipLibCheck",
    "--outDir", directory,
    ...sources,
  ]);
  return pathToFileURL(join(directory, "store.js")).href;
}

async function waitForCounter(gate: Int32Array, index: number, expected: number): Promise<void> {
  const deadline = Date.now() + 10_000;
  while (Atomics.load(gate, index) < expected) {
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for gate ${index}.`);
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

function workerResult(worker: Worker): Promise<WorkerResult> {
  return new Promise((resolve, reject) => {
    worker.once("message", resolve);
    worker.once("error", reject);
    worker.once("exit", (code) => {
      if (code !== 0) reject(new Error(`Store worker exited with code ${code}.`));
    });
  });
}

async function runOverlappingOperations(
  databasePath: string,
  operations: [WorkerOperation, WorkerOperation],
): Promise<WorkerResult[]> {
  const gateBuffer = new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT * 5);
  const gate = new Int32Array(gateBuffer);
  const storeModuleUrl = compileWorkerStore();
  const operationWorkers = operations.map((operation) => new Worker(STORE_WORKER, {
    eval: true,
    workerData: { databasePath, gate: gateBuffer, operation, storeModuleUrl },
  }));
  const results = operationWorkers.map(workerResult);

  await waitForCounter(gate, 0, 2);
  const lockWorker = new Worker(LOCK_WORKER, {
    eval: true,
    workerData: { databasePath, gate: gateBuffer },
  });
  const lockResult = workerResult(lockWorker);
  await waitForCounter(gate, 3, 1);

  Atomics.store(gate, 1, 1);
  Atomics.notify(gate, 1, 2);
  await waitForCounter(gate, 2, 2);
  Atomics.store(gate, 4, 1);
  Atomics.notify(gate, 4);

  await lockResult;
  return Promise.all(results);
}

function openStore(databasePath = createDatabasePath()): CaptainRemoteStore {
  const store = new CaptainRemoteStore(databasePath);
  store.initialize();
  stores.push(store);
  return store;
}

function closeStore(store: CaptainRemoteStore): void {
  store.close();
  stores.splice(stores.indexOf(store), 1);
}

function useDeleteJournal(databasePath: string): void {
  const database = new DatabaseSync(databasePath, { timeout: 5000 });
  expect(database.prepare("PRAGMA journal_mode = DELETE").get())
    .toEqual({ journal_mode: "delete" });
  database.close();
}

afterEach(() => {
  for (const store of stores.splice(0)) store.close();
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function createMember(store: CaptainRemoteStore, name: string): StoredMember {
  return store.createMember(name, `${name.toLowerCase()}@example.com`, issueMemberToken());
}

function reportTurn(turnId: string, summary = "Implemented durable storage."): TurnInput {
  return {
    turn_id: turnId,
    kind: "report",
    report: { summary: [summary] },
    metadata: { client: "vitest" },
  };
}

function reservation(
  memberId: string,
  reportId: string,
  turnId: string,
  summary?: string,
): ReserveTurnInput {
  const request = reportTurn(turnId, summary);
  return {
    memberId,
    reportId,
    turnId,
    requestDigest: digestTurnInput(request),
    payloadJson: canonicalizeTurnInput(request),
  };
}

const result: CaptainResult = {
  report_id: "report-1",
  status: "updated",
  clickup_updates: [],
  captain_feedback: "Recorded.",
  questions: [],
  warnings: [],
};

describe("CaptainRemoteStore members", () => {
  it("inserts a caller-supplied server UUID without changing createMember", () => {
    const store = openStore();
    const issued = issueMemberToken();
    const memberId = "00000000-0000-4000-8000-000000000123";

    const supplied = store.createMemberWithId(
      memberId,
      "Alice",
      "alice@example.com",
      issued,
    );
    const generated = store.createMember("Bob", "bob@example.com", issueMemberToken());

    expect(supplied.memberId).toBe(memberId);
    expect(store.findMemberForAuth(issued.lookupId)?.memberId).toBe(memberId);
    expect(generated.memberId).toMatch(/^[0-9a-f-]{36}$/);
    expect(generated.memberId).not.toBe(memberId);
  });

  it("lists member identity without exposing token material", () => {
    const store = openStore();
    const alice = store.createMember("Alice", "alice@example.com", issueMemberToken());

    expect(alice.name).toBe("Alice");
    expect(alice.email).toBe("alice@example.com");
    expect(store.listMembers()).toEqual([alice]);
    expect(store.listMembers()[0]).not.toHaveProperty("token");
    expect(store.listMembers()[0]).not.toHaveProperty("secret");
    expect(store.listMembers()[0]).not.toHaveProperty("lookupId");
    expect(store.listMembers()[0]).not.toHaveProperty("digest");
  });

  it("rotates a member lookup and retains a revoked auth record", () => {
    const store = openStore();
    const firstToken = issueMemberToken();
    const alice = store.createMember("Alice", "alice@example.com", firstToken);
    const nextToken = issueMemberToken();

    store.rotateMember(alice.memberId, nextToken);
    expect(store.findMemberForAuth(firstToken.lookupId)).toBeNull();
    expect(store.findMemberForAuth(nextToken.lookupId)?.revokedAt).toBeNull();

    store.revokeMember(alice.memberId);
    expect(store.findMemberForAuth(nextToken.lookupId)?.revokedAt).not.toBeNull();
  });

  it("requires both a display name and email", () => {
    const store = openStore();

    expect(() => store.createMember(" ", "alice@example.com", issueMemberToken()))
      .toThrow("Member name is required.");
    expect(() => store.createMember("Alice", " ", issueMemberToken()))
      .toThrow("Member email is required.");
    expect(() => store.createMember("Line\nBreak", "alice@example.com", issueMemberToken()))
      .toThrow("Member name is invalid.");
    expect(() => store.createMember("x".repeat(101), "alice@example.com", issueMemberToken()))
      .toThrow("Member name is invalid.");
  });

  it("uses owner-only filesystem modes and WAL", () => {
    const databasePath = createDatabasePath();
    const store = openStore(databasePath);
    store.close();
    stores.pop();

    expect(statSync(join(databasePath, "..")).mode & 0o777).toBe(0o700);
    expect(statSync(databasePath).mode & 0o777).toBe(0o600);
    const database = new DatabaseSync(databasePath);
    expect(database.prepare("PRAGMA journal_mode").get()).toEqual({ journal_mode: "wal" });
    database.close();
  });

  it("opens with a five-second timeout and fails closed when WAL is unavailable", async () => {
    const databasePath = createDatabasePath();
    mkdirSync(dirname(databasePath), { recursive: true });
    writeFileSync(databasePath, "");
    let constructorOptions: { timeout?: number } | undefined;

    vi.doMock("node:sqlite", () => ({
      DatabaseSync: class {
        constructor(_path: string, options?: { timeout?: number }) {
          constructorOptions = options;
        }
        close() {}
        exec() {}
        prepare() {
          return { get: () => ({ journal_mode: "delete" }) };
        }
      },
    }));

    try {
      const { CaptainRemoteStore: UnavailableWalStore } = await import("../src/store.js?wal-unavailable");
      const store = new UnavailableWalStore(databasePath);
      expect(() => store.initialize()).toThrow("Captain remote database requires WAL mode.");
      expect(constructorOptions).toEqual({ timeout: 5000 });
    } finally {
      vi.doUnmock("node:sqlite");
      vi.resetModules();
    }
  });
});

describe("CaptainRemoteStore turns", () => {
  it("replays an identical composite turn and rejects changed content", () => {
    const databasePath = createDatabasePath();
    const firstStore = openStore(databasePath);
    const alice = createMember(firstStore, "Alice");
    const input = reservation(alice.memberId, "report-1", "00000000-0000-4000-8000-000000000001");

    const created = firstStore.reserveTurn(input);
    const secondStore = openStore(databasePath);
    const replayed = secondStore.reserveTurn(input);

    expect(created.status).toBe("created");
    expect(replayed.status).toBe("existing");
    expect(replayed.turn).toEqual(created.turn);
    expect(() => secondStore.reserveTurn({
      ...input,
      requestDigest: "changed-digest",
      payloadJson: "{}",
    })).toThrowError(expect.objectContaining({ code: "TURN_CONFLICT" }));
  });

  it("serializes overlapping duplicate reservations across worker connections", async () => {
    const databasePath = createDatabasePath();
    const store = openStore(databasePath);
    const alice = createMember(store, "Alice");
    const input = reservation(
      alice.memberId,
      "concurrent-report",
      "00000000-0000-4000-8000-00000000000b",
    );
    closeStore(store);
    useDeleteJournal(databasePath);

    const outcomes = await runOverlappingOperations(databasePath, [
      { action: "reserve", input },
      { action: "reserve", input },
    ]);

    expect(outcomes.every((outcome) => outcome.ok), JSON.stringify(outcomes)).toBe(true);
    expect(outcomes.map((outcome) => outcome.value).sort()).toEqual(["created", "existing"]);
    expect(openStore(databasePath).getTurn(input)).toMatchObject({ state: "queued" });
  });

  it("scopes identical report and turn IDs to each member", () => {
    const store = openStore();
    const alice = createMember(store, "Alice");
    const bob = createMember(store, "Bob");
    const turnId = "00000000-0000-4000-8000-000000000002";

    const aliceTurn = store.reserveTurn(reservation(alice.memberId, "shared", turnId));
    const bobTurn = store.reserveTurn(reservation(bob.memberId, "shared", turnId));

    expect(aliceTurn.report.sessionId).not.toBe(bobTurn.report.sessionId);
    expect(store.getTurn({ memberId: alice.memberId, reportId: "shared", turnId })?.memberId)
      .toBe(alice.memberId);
    expect(store.getTurn({ memberId: bob.memberId, reportId: "shared", turnId })?.memberId)
      .toBe(bob.memberId);
    expect(store.getTurn({ memberId: alice.memberId, reportId: "missing", turnId })).toBeNull();
  });

  it("allows only one queued or started turn per member", () => {
    const store = openStore();
    const alice = createMember(store, "Alice");

    store.reserveTurn(reservation(
      alice.memberId,
      "report-1",
      "00000000-0000-4000-8000-000000000003",
    ));
    expect(() => store.reserveTurn(reservation(
      alice.memberId,
      "report-2",
      "00000000-0000-4000-8000-000000000004",
    ))).toThrowError(expect.objectContaining({ code: "MEMBER_ACTIVE_LIMIT" }));
  });

  it("allows at most 32 globally active turns", () => {
    const store = openStore();

    for (let index = 0; index < 32; index += 1) {
      const member = createMember(store, `Member${index}`);
      const suffix = (index + 10).toString(16).padStart(12, "0");
      store.reserveTurn(reservation(
        member.memberId,
        `report-${index}`,
        `00000000-0000-4000-8000-${suffix}`,
      ));
    }

    const overflow = createMember(store, "Overflow");
    expect(() => store.reserveTurn(reservation(
      overflow.memberId,
      "overflow",
      "00000000-0000-4000-8000-000000000099",
    ))).toThrowError(expect.objectContaining({ code: "GLOBAL_ACTIVE_LIMIT" }));
  });

  it("honors a lower configured global active-turn limit", () => {
    const path = createDatabasePath();
    const store = new CaptainRemoteStore(path, { maxGlobalActiveTurns: 1 });
    store.initialize();
    stores.push(store);
    const alice = createMember(store, "Alice");
    const bob = createMember(store, "Bob");

    store.reserveTurn(reservation(
      alice.memberId,
      "alice-report",
      "00000000-0000-4000-8000-000000000124",
    ));

    expect(() => store.reserveTurn(reservation(
      bob.memberId,
      "bob-report",
      "00000000-0000-4000-8000-000000000125",
    ))).toThrowError(expect.objectContaining({ code: "GLOBAL_ACTIVE_LIMIT" }));
  });

  it.each([0, -1, 1.5, 33, Number.NaN, Infinity])(
    "rejects invalid global active-turn limit %s",
    (maxGlobalActiveTurns) => {
      expect(() => new CaptainRemoteStore(createDatabasePath(), { maxGlobalActiveTurns }))
        .toThrow("maxGlobalActiveTurns");
    },
  );

  it("claims FIFO while atomically enforcing running capacity", () => {
    const store = openStore();
    const alice = createMember(store, "Alice");
    const bob = createMember(store, "Bob");
    const firstKey = {
      memberId: alice.memberId,
      reportId: "first",
      turnId: "00000000-0000-4000-8000-000000000005",
    };
    const secondKey = {
      memberId: bob.memberId,
      reportId: "second",
      turnId: "00000000-0000-4000-8000-000000000006",
    };
    store.reserveTurn(reservation(firstKey.memberId, firstKey.reportId, firstKey.turnId));
    store.reserveTurn(reservation(secondKey.memberId, secondKey.reportId, secondKey.turnId));

    const first = store.claimNextTurn(1);
    expect(first).toMatchObject({ ...firstKey, state: "started" });
    expect(first?.runId).toMatch(/^[0-9a-f-]{36}$/);
    expect(first?.payload).toEqual(reportTurn(firstKey.turnId));
    expect(first?.member.name).toBe("Alice");
    expect(first?.report.reportId).toBe("first");
    expect(store.claimNextTurn(1)).toBeNull();

    store.finishTurn(firstKey, "succeeded", result);
    expect(store.claimNextTurn(1)).toMatchObject({ ...secondKey, state: "started" });
  });

  it("serializes overlapping claims at the running limit", async () => {
    const databasePath = createDatabasePath();
    const store = openStore(databasePath);
    const alice = createMember(store, "Alice");
    const bob = createMember(store, "Bob");
    const firstTurnId = "00000000-0000-4000-8000-00000000000c";
    const secondTurnId = "00000000-0000-4000-8000-00000000000d";
    store.reserveTurn(reservation(alice.memberId, "first", firstTurnId));
    store.reserveTurn(reservation(bob.memberId, "second", secondTurnId));
    closeStore(store);
    useDeleteJournal(databasePath);

    const outcomes = await runOverlappingOperations(databasePath, [
      { action: "claim", maxRunning: 1 },
      { action: "claim", maxRunning: 1 },
    ]);

    expect(outcomes.every((outcome) => outcome.ok), JSON.stringify(outcomes)).toBe(true);
    expect(outcomes.filter((outcome) => outcome.value === firstTurnId)).toHaveLength(1);
    expect(outcomes.filter((outcome) => outcome.value === null)).toHaveLength(1);
    expect(openStore(databasePath).getTurn({
      memberId: bob.memberId,
      reportId: "second",
      turnId: secondTurnId,
    }))
      .toMatchObject({ state: "queued" });
  });

  it.each([undefined, Number.NaN, Infinity, 0, -1, 1.5])(
    "rejects invalid running capacity %s without claiming",
    (maxRunning) => {
      const store = openStore();
      const alice = createMember(store, "Alice");
      const key = {
        memberId: alice.memberId,
        reportId: "invalid-capacity",
        turnId: "00000000-0000-4000-8000-00000000000e",
      };
      store.reserveTurn(reservation(key.memberId, key.reportId, key.turnId));

      expect(() => store.claimNextTurn(maxRunning as number)).toThrow("maxRunning");
      expect(store.getTurn(key)).toMatchObject({ state: "queued" });
    },
  );

  it("persists terminal results and stable errors", () => {
    const store = openStore();
    const alice = createMember(store, "Alice");
    const successKey = {
      memberId: alice.memberId,
      reportId: "report-1",
      turnId: "00000000-0000-4000-8000-000000000007",
    };
    store.reserveTurn(reservation(successKey.memberId, successKey.reportId, successKey.turnId));
    store.claimNextTurn(1);
    store.finishTurn(successKey, "succeeded", result);
    expect(store.getTurn(successKey)).toMatchObject({ state: "succeeded", result, error: null });

    const failureKey = { ...successKey, turnId: "00000000-0000-4000-8000-000000000008" };
    store.reserveTurn(reservation(failureKey.memberId, failureKey.reportId, failureKey.turnId));
    store.claimNextTurn(1);
    store.finishTurn(failureKey, "failed", undefined, { code: "CAPTAIN_FAILED", message: "Failed." });
    expect(store.getTurn(failureKey)).toMatchObject({
      state: "failed",
      result: null,
      error: { code: "CAPTAIN_FAILED", message: "Failed." },
    });
  });

  it("preserves an empty stable error message", () => {
    const store = openStore();
    const alice = createMember(store, "Alice");
    const key = {
      memberId: alice.memberId,
      reportId: "empty-error",
      turnId: "00000000-0000-4000-8000-00000000000f",
    };
    store.reserveTurn(reservation(key.memberId, key.reportId, key.turnId));
    store.claimNextTurn(1);

    store.finishTurn(key, "failed", undefined, { code: "EMPTY_MESSAGE", message: "" });

    expect(store.getTurn(key)?.error).toEqual({ code: "EMPTY_MESSAGE", message: "" });
  });

  it("rejects nonterminal completion states at runtime", () => {
    const store = openStore();
    const alice = createMember(store, "Alice");
    const key = {
      memberId: alice.memberId,
      reportId: "invalid-state",
      turnId: "00000000-0000-4000-8000-000000000010",
    };
    store.reserveTurn(reservation(key.memberId, key.reportId, key.turnId));
    store.claimNextTurn(1);

    expect(() => store.finishTurn(key, "queued" as never))
      .toThrowError(expect.objectContaining({ code: "INVALID_TURN_STATE" }));
    expect(store.getTurn(key)).toMatchObject({ state: "started" });
  });

  it("enforces valid states in SQLite", () => {
    const databasePath = createDatabasePath();
    const store = openStore(databasePath);
    const alice = createMember(store, "Alice");
    const key = {
      memberId: alice.memberId,
      reportId: "schema-state",
      turnId: "00000000-0000-4000-8000-000000000011",
    };
    store.reserveTurn(reservation(key.memberId, key.reportId, key.turnId));
    const database = new DatabaseSync(databasePath, { timeout: 5000 });

    expect(() => database.prepare(`
      UPDATE turns SET state = 'invalid' WHERE turn_id = ?
    `).run(key.turnId)).toThrow();
    expect(store.getTurn(key)).toMatchObject({ state: "queued" });
    database.close();
  });

  it("recovers only started work as unknown outcome after restart", () => {
    const databasePath = createDatabasePath();
    const firstStore = openStore(databasePath);
    const alice = createMember(firstStore, "Alice");
    const bob = createMember(firstStore, "Bob");
    const startedKey = {
      memberId: alice.memberId,
      reportId: "started",
      turnId: "00000000-0000-4000-8000-000000000009",
    };
    const queuedKey = {
      memberId: bob.memberId,
      reportId: "queued",
      turnId: "00000000-0000-4000-8000-00000000000a",
    };
    firstStore.reserveTurn(reservation(startedKey.memberId, startedKey.reportId, startedKey.turnId));
    firstStore.reserveTurn(reservation(queuedKey.memberId, queuedKey.reportId, queuedKey.turnId));
    firstStore.claimNextTurn(1);
    firstStore.close();
    stores.splice(stores.indexOf(firstStore), 1);

    const restartedStore = openStore(databasePath);
    expect(restartedStore.recoverStartedTurns()).toBe(1);
    expect(restartedStore.getTurn(startedKey)).toMatchObject({
      state: "unknown_outcome",
      error: { code: "UNKNOWN_OUTCOME" },
    });
    expect(restartedStore.getTurn(queuedKey)).toMatchObject({ state: "queued" });
    expect(restartedStore.recoverStartedTurns()).toBe(0);
  });
});
