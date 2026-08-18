import { mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { afterEach, describe, expect, it } from "vitest";

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

function createDatabasePath(): string {
  const directory = mkdtempSync(join(tmpdir(), "captain-remote-store-"));
  temporaryDirectories.push(directory);
  return join(directory, "data", "captain.sqlite");
}

function openStore(databasePath = createDatabasePath()): CaptainRemoteStore {
  const store = new CaptainRemoteStore(databasePath);
  store.initialize();
  stores.push(store);
  return store;
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
  it("lists member identity without exposing token material", () => {
    const store = openStore();
    const alice = store.createMember("Alice", "alice@example.com", issueMemberToken());

    expect(alice.name).toBe("Alice");
    expect(alice.email).toBe("alice@example.com");
    expect(store.listMembers()).toEqual([alice]);
    expect(store.listMembers()[0]).not.toHaveProperty("token");
    expect(store.listMembers()[0]).not.toHaveProperty("secret");
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
