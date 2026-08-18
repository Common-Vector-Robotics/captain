import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CaptainResult, TurnInput } from "../src/contracts.js";
import { createCaptainHttpHandler } from "../src/http.js";
import {
  CaptainAuthenticator,
  issueMemberToken,
  PollLimiter,
  type IssuedToken,
} from "../src/security.js";
import { CaptainRemoteStore, type StoredMember } from "../src/store.js";

const TURN_ID = "018f6f72-7c8a-7d8d-91a5-0b8d9f2f3a4b";
const OTHER_TURN_ID = "018f6f72-7c8a-7d8d-91a5-0b8d9f2f3a4c";
const DEFAULT_MAX_REQUEST_BYTES = 262_144;

interface MemberFixture {
  member: StoredMember;
  issued: IssuedToken;
}

interface HttpFixture {
  baseUrl: string;
  store: CaptainRemoteStore;
  alice: MemberFixture;
  bob: MemberFixture;
  wakeWorker: ReturnType<typeof vi.fn>;
}

const servers: Server[] = [];
const stores: CaptainRemoteStore[] = [];
const temporaryDirectories: string[] = [];

afterEach(async () => {
  for (const server of servers.splice(0)) {
    server.closeAllConnections();
    server.close();
    await once(server, "close");
  }
  for (const store of stores.splice(0)) store.close();
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function createMember(store: CaptainRemoteStore, name: string): MemberFixture {
  const issued = issueMemberToken();
  return {
    issued,
    member: store.createMember(name, `${name.toLowerCase()}@example.com`, issued),
  };
}

async function startFixture(options: {
  maxRequestBytes?: number;
  pollLimiter?: PollLimiter;
} = {}): Promise<HttpFixture> {
  const directory = mkdtempSync(join(tmpdir(), "captain-http-test-"));
  temporaryDirectories.push(directory);
  const store = new CaptainRemoteStore(join(directory, "captain.sqlite"));
  store.initialize();
  stores.push(store);

  const alice = createMember(store, "Alice");
  const bob = createMember(store, "Bob");
  const wakeWorker = vi.fn();
  const handler = createCaptainHttpHandler({
    store,
    authenticator: new CaptainAuthenticator(store),
    pollLimiter: options.pollLimiter ?? new PollLimiter(),
    maxRequestBytes: options.maxRequestBytes,
    wakeWorker,
  });
  const server = createServer(handler);
  servers.push(server);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Test server did not bind TCP.");

  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    store,
    alice,
    bob,
    wakeWorker,
  };
}

function reportTurn(turnId = TURN_ID, summary = "Implemented the HTTP boundary."): TurnInput {
  return {
    turn_id: turnId,
    kind: "report",
    report: { summary: [summary] },
    metadata: { client: "vitest" },
  };
}

function authorization(member: MemberFixture): Record<string, string> {
  return { authorization: `Bearer ${member.issued.token}` };
}

function submitUrl(baseUrl: string, reportId = "report-1"): string {
  return `${baseUrl}/captain/v1/reports/${reportId}/turns`;
}

function pollUrl(baseUrl: string, turnId = TURN_ID, reportId = "report-1"): string {
  return `${submitUrl(baseUrl, reportId)}/${turnId}`;
}

async function postTurn(
  fixture: HttpFixture,
  member: MemberFixture,
  input: unknown = reportTurn(),
  options: { contentType?: string; reportId?: string } = {},
): Promise<Response> {
  return fetch(submitUrl(fixture.baseUrl, options.reportId), {
    method: "POST",
    headers: {
      ...authorization(member),
      "content-type": options.contentType ?? "application/json",
    },
    body: JSON.stringify(input),
  });
}

function expectJsonHeaders(response: Response): void {
  expect(response.headers.get("cache-control")).toBe("no-store");
  expect(response.headers.get("content-type")).toBe("application/json; charset=utf-8");
}

async function expectProblem(
  response: Response,
  status: number,
  code: string,
): Promise<Record<string, unknown>> {
  expect(response.status).toBe(status);
  expectJsonHeaders(response);
  const body = await response.json() as Record<string, unknown>;
  expect(body).toMatchObject({ error: { code } });
  expect(JSON.stringify(body).length).toBeLessThan(512);
  return body;
}

describe("Captain HTTP submit", () => {
  it("durably queues a validated turn and wakes the worker once", async () => {
    const fixture = await startFixture();

    const response = await postTurn(fixture, fixture.alice);

    expect(response.status).toBe(202);
    expectJsonHeaders(response);
    expect(await response.json()).toEqual({
      report_id: "report-1",
      turn_id: TURN_ID,
      turn_status: "queued",
    });
    expect(fixture.store.getTurn({
      memberId: fixture.alice.member.memberId,
      reportId: "report-1",
      turnId: TURN_ID,
    })).toMatchObject({ state: "queued", payload: reportTurn() });
    expect(fixture.wakeWorker).toHaveBeenCalledTimes(1);
  });

  it("returns the saved envelope for a replay at capacity without another wake", async () => {
    const fixture = await startFixture();
    const first = await postTurn(fixture, fixture.alice);
    expect(first.status).toBe(202);
    const firstEnvelope = await first.json();

    const replay = await postTurn(fixture, fixture.alice);

    expect(replay.status).toBe(202);
    expect(await replay.json()).toEqual(firstEnvelope);
    expect(fixture.wakeWorker).toHaveBeenCalledTimes(1);
  });

  it("returns a saved terminal result for a replay and conflicts on changed content", async () => {
    const fixture = await startFixture();
    expect((await postTurn(fixture, fixture.alice)).status).toBe(202);
    fixture.store.claimNextTurn(1);
    const result: CaptainResult = {
      report_id: "report-1",
      status: "updated",
      clickup_updates: [],
      captain_feedback: "Recorded.",
      questions: [],
      warnings: [],
    };
    fixture.store.finishTurn({
      memberId: fixture.alice.member.memberId,
      reportId: "report-1",
      turnId: TURN_ID,
    }, "succeeded", result);

    const replay = await postTurn(fixture, fixture.alice);
    expect(replay.status).toBe(200);
    expect(await replay.json()).toEqual({
      report_id: "report-1",
      turn_id: TURN_ID,
      turn_status: "succeeded",
      result,
    });

    const conflict = await postTurn(
      fixture,
      fixture.alice,
      reportTurn(TURN_ID, "Changed payload."),
    );
    await expectProblem(conflict, 409, "TURN_CONFLICT");
    expect(fixture.wakeWorker).toHaveBeenCalledTimes(1);
  });

  it("authenticates before reading a streaming request body", async () => {
    const fixture = await startFixture();
    let closeBody: (() => void) | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("{"));
        closeBody = () => controller.close();
      },
    });

    try {
      const response = await Promise.race([
        fetch(submitUrl(fixture.baseUrl), {
          method: "POST",
          headers: { "content-type": "application/json" },
          body,
          duplex: "half",
        } as RequestInit & { duplex: "half" }),
        new Promise<never>((_resolve, reject) => {
          setTimeout(() => reject(new Error("Handler waited for the unauthorized body.")), 1_000);
        }),
      ]);

      await expectProblem(response, 401, "UNAUTHORIZED");
      expect(fixture.wakeWorker).not.toHaveBeenCalled();
    } finally {
      closeBody?.();
    }
  });

  it("accepts only the application/json media type and strict turn schema", async () => {
    const fixture = await startFixture();

    const compatible = await postTurn(
      fixture,
      fixture.alice,
      reportTurn(),
      { contentType: "Application/JSON; charset=UTF-8" },
    );
    expect(compatible.status).toBe(202);

    for (const contentType of [
      "text/json",
      "application/problem+json",
      "application/json-patch",
    ]) {
      const response = await postTurn(
        fixture,
        fixture.bob,
        reportTurn(OTHER_TURN_ID),
        { contentType },
      );
      await expectProblem(response, 415, "UNSUPPORTED_MEDIA_TYPE");
    }

    const malformed = await fetch(submitUrl(fixture.baseUrl), {
      method: "POST",
      headers: {
        ...authorization(fixture.bob),
        "content-type": "application/json",
      },
      body: "{",
    });
    await expectProblem(malformed, 400, "INVALID_JSON");

    for (const invalid of [
      { ...reportTurn(OTHER_TURN_ID), unexpected: true },
      { ...reportTurn(OTHER_TURN_ID), model: "client-selected" },
    ]) {
      const response = await postTurn(fixture, fixture.bob, invalid);
      await expectProblem(response, 400, "INVALID_REQUEST");
    }
    expect(fixture.wakeWorker).toHaveBeenCalledTimes(1);
  });

  it("returns Retry-After when new work exceeds the member capacity", async () => {
    const fixture = await startFixture();
    expect((await postTurn(fixture, fixture.alice)).status).toBe(202);

    const response = await postTurn(
      fixture,
      fixture.alice,
      reportTurn(OTHER_TURN_ID),
      { reportId: "report-2" },
    );

    await expectProblem(response, 429, "MEMBER_ACTIVE_LIMIT");
    expect(response.headers.get("retry-after")).toBe("1");
    expect(fixture.store.getTurn({
      memberId: fixture.alice.member.memberId,
      reportId: "report-2",
      turnId: OTHER_TURN_ID,
    })).toBeNull();
    expect(fixture.wakeWorker).toHaveBeenCalledTimes(1);
  });

  it("caps a streamed body at 262145 bytes and inserts nothing", async () => {
    const fixture = await startFixture();
    const prefix = JSON.stringify(reportTurn()).slice(0, -2) + ',"padding":"';
    const suffix = '"}';
    const paddingLength = DEFAULT_MAX_REQUEST_BYTES + 1 - Buffer.byteLength(prefix + suffix);
    const oversized = prefix + "x".repeat(paddingLength) + suffix;
    expect(Buffer.byteLength(oversized)).toBe(DEFAULT_MAX_REQUEST_BYTES + 1);
    const chunks = [oversized.slice(0, 100_000), oversized.slice(100_000)];
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
        controller.close();
      },
    });

    const response = await fetch(submitUrl(fixture.baseUrl), {
      method: "POST",
      headers: {
        ...authorization(fixture.alice),
        "content-type": "application/json",
      },
      body,
      duplex: "half",
    } as RequestInit & { duplex: "half" });

    await expectProblem(response, 413, "PAYLOAD_TOO_LARGE");
    expect(fixture.store.getTurn({
      memberId: fixture.alice.member.memberId,
      reportId: "report-1",
      turnId: TURN_ID,
    })).toBeNull();
    expect(fixture.wakeWorker).not.toHaveBeenCalled();
  });

  it("rejects missing and revoked tokens with the same response", async () => {
    const fixture = await startFixture();
    const missing = await fetch(submitUrl(fixture.baseUrl), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(reportTurn()),
    });
    fixture.store.revokeMember(fixture.alice.member.memberId);
    const revoked = await postTurn(fixture, fixture.alice);

    const missingBody = await expectProblem(missing, 401, "UNAUTHORIZED");
    const revokedBody = await expectProblem(revoked, 401, "UNAUTHORIZED");
    expect(revokedBody).toEqual(missingBody);
    expect(fixture.wakeWorker).not.toHaveBeenCalled();
  });
});

describe("Captain HTTP poll and dispatch", () => {
  it("returns only the authenticated member-owned turn", async () => {
    const fixture = await startFixture();
    expect((await postTurn(fixture, fixture.bob)).status).toBe(202);

    const bobPoll = await fetch(pollUrl(fixture.baseUrl), {
      headers: authorization(fixture.bob),
    });
    expect(bobPoll.status).toBe(200);
    expectJsonHeaders(bobPoll);
    expect(await bobPoll.json()).toEqual({
      report_id: "report-1",
      turn_id: TURN_ID,
      turn_status: "queued",
    });

    const alicePoll = await fetch(pollUrl(fixture.baseUrl), {
      headers: authorization(fixture.alice),
    });
    const missingPoll = await fetch(pollUrl(fixture.baseUrl, OTHER_TURN_ID), {
      headers: authorization(fixture.alice),
    });
    const aliceBody = await expectProblem(alicePoll, 404, "NOT_FOUND");
    const missingBody = await expectProblem(missingPoll, 404, "NOT_FOUND");
    expect(aliceBody).toEqual(missingBody);
  });

  it("consumes the member poll bucket before looking up a record", async () => {
    const fixture = await startFixture({ pollLimiter: new PollLimiter({ now: () => 0 }) });

    for (let attempt = 0; attempt < 5; attempt += 1) {
      const response = await fetch(pollUrl(fixture.baseUrl), {
        headers: authorization(fixture.alice),
      });
      await expectProblem(response, 404, "NOT_FOUND");
    }
    const limited = await fetch(pollUrl(fixture.baseUrl), {
      headers: authorization(fixture.alice),
    });

    await expectProblem(limited, 429, "RATE_LIMITED");
    expect(limited.headers.get("retry-after")).toBe("2");
  });

  it("rejects queries, overmatched paths, invalid IDs, and wrong methods", async () => {
    const fixture = await startFixture();
    const cases: Array<[string, RequestInit, number, string]> = [
      [`${submitUrl(fixture.baseUrl)}?trace=1`, {
        method: "POST",
        headers: { ...authorization(fixture.alice), "content-type": "application/json" },
        body: JSON.stringify(reportTurn()),
      }, 404, "NOT_FOUND"],
      [`${pollUrl(fixture.baseUrl)}?`, { headers: authorization(fixture.alice) }, 404, "NOT_FOUND"],
      [`${fixture.baseUrl}/captain/v1/reports/report%2Fone/turns`, {
        method: "POST",
        headers: { ...authorization(fixture.alice), "content-type": "application/json" },
        body: JSON.stringify(reportTurn()),
      }, 404, "NOT_FOUND"],
      [`${submitUrl(fixture.baseUrl)}/not-a-uuid`, {
        headers: authorization(fixture.alice),
      }, 404, "NOT_FOUND"],
      [submitUrl(fixture.baseUrl), {
        method: "GET",
        headers: authorization(fixture.alice),
      }, 405, "METHOD_NOT_ALLOWED"],
      [pollUrl(fixture.baseUrl), {
        method: "POST",
        headers: { ...authorization(fixture.alice), "content-type": "application/json" },
        body: JSON.stringify(reportTurn()),
      }, 405, "METHOD_NOT_ALLOWED"],
    ];

    for (const [url, init, status, code] of cases) {
      await expectProblem(await fetch(url, init), status, code);
    }
    expect(fixture.wakeWorker).not.toHaveBeenCalled();
  });

  it("returns fixed bounded errors without reflecting secrets or internals", async () => {
    const fixture = await startFixture();
    const token = "cap_v1_0123456789abcdef.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    const unauthorized = await fetch(pollUrl(fixture.baseUrl), {
      headers: { authorization: `Bearer ${token}` },
    });
    const unauthorizedText = JSON.stringify(await expectProblem(
      unauthorized,
      401,
      "UNAUTHORIZED",
    ));
    expect(unauthorizedText).not.toContain(token);

    const databasePath = "/private/var/captain-remote.sqlite3";
    const sessionId = "openclaw:captain:server-session-secret";
    vi.spyOn(fixture.store, "getTurn").mockImplementation(() => {
      throw new Error(`${databasePath} ${sessionId}\n${new Error("private stack").stack}`);
    });
    const internal = await fetch(pollUrl(fixture.baseUrl), {
      headers: authorization(fixture.alice),
    });
    const internalText = JSON.stringify(await expectProblem(
      internal,
      500,
      "INTERNAL_ERROR",
    ));
    expect(internalText).not.toContain(databasePath);
    expect(internalText).not.toContain(sessionId);
    expect(internalText).not.toContain("private stack");
    expect(internalText).not.toContain(fixture.alice.member.memberId);
  });
});
