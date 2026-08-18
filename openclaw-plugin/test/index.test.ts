import { mkdtempSync, rmSync } from "node:fs";
import { createServer, IncomingMessage, ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type {
  OpenClawPluginApi,
  OpenClawPluginHttpRouteHandler,
  OpenClawPluginService,
} from "openclaw/plugin-sdk/plugin-entry";
import { afterEach, describe, expect, it, vi } from "vitest";

import plugin from "../src/index.js";
import { CaptainTurnWorker } from "../src/runtime.js";
import { issueMemberToken, LimitEventAggregator } from "../src/security.js";
import { CaptainRemoteStore } from "../src/store.js";

interface RecordedResponse {
  statusCode: number;
  headers: Record<string, string>;
  body: string;
  writableEnded: boolean;
  destroyed: boolean;
  setHeader(name: string, value: string): void;
  end(value?: string): void;
}

const temporaryDirectories: string[] = [];

afterEach(() => {
  vi.restoreAllMocks();
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function databasePath(): string {
  const directory = mkdtempSync(join(tmpdir(), "captain-index-test-"));
  temporaryDirectories.push(directory);
  return join(directory, "captain.sqlite3");
}

function recorder(): RecordedResponse {
  return {
    statusCode: 200,
    headers: {},
    body: "",
    writableEnded: false,
    destroyed: false,
    setHeader(name, value) {
      this.headers[name.toLowerCase()] = value;
    },
    end(value = "") {
      this.body += value;
      this.writableEnded = true;
    },
  };
}

function createApi(options: {
  path?: string;
  agents?: Array<{ id: string }>;
  runEmbeddedAgent?: (...args: unknown[]) => Promise<unknown>;
  pluginConfig?: Record<string, unknown>;
} = {}) {
  let route: Parameters<OpenClawPluginApi["registerHttpRoute"]>[0] | undefined;
  let service: OpenClawPluginService | undefined;
  let cliOptions: Parameters<OpenClawPluginApi["registerCli"]>[1];
  const logger = {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
  const runEmbeddedAgent = vi.fn(options.runEmbeddedAgent ?? (async () => ({
    meta: { durationMs: 1, livenessState: "working", stopReason: "stop" },
    payloads: [],
  })));
  const resolveAgentWorkspaceDir = vi.fn(() => "/captain/workspace");
  const api = {
    config: { agents: { list: options.agents ?? [{ id: "captain" }] } },
    pluginConfig: options.pluginConfig ?? (options.path ? { databasePath: options.path } : {}),
    logger,
    runtime: {
      agent: { runEmbeddedAgent, resolveAgentWorkspaceDir },
    },
    registerHttpRoute: vi.fn((value) => {
      route = value;
    }),
    registerService: vi.fn((value) => {
      service = value;
    }),
    registerCli: vi.fn((_registrar, value) => {
      cliOptions = value;
    }),
  } as unknown as OpenClawPluginApi;
  plugin.register(api);
  return {
    api,
    logger,
    runEmbeddedAgent,
    resolveAgentWorkspaceDir,
    get route() {
      if (!route) throw new Error("Route was not registered.");
      return route;
    },
    get service() {
      if (!service) throw new Error("Service was not registered.");
      return service;
    },
    get cliOptions() {
      return cliOptions;
    },
  };
}

async function listen(handler: OpenClawPluginHttpRouteHandler) {
  const server = createServer((request, response) => {
    void handler(request, response);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Test server did not bind TCP.");
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    }),
  };
}

async function waitFor(check: () => boolean): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (!check()) {
    if (Date.now() >= deadline) throw new Error("Timed out waiting for condition.");
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

async function callRoute(handler: OpenClawPluginHttpRouteHandler): Promise<RecordedResponse> {
  const response = recorder();
  await handler({} as IncomingMessage, response as unknown as ServerResponse);
  return response;
}

function serviceContext(api: OpenClawPluginApi) {
  return {
    config: api.config,
    stateDir: "/unused/state",
    logger: api.logger,
  };
}

describe("Captain remote plugin entry", () => {
  it("registers exactly one private prefix route, one service, and one root CLI", () => {
    const fixture = createApi({ path: databasePath() });

    expect(fixture.api.registerHttpRoute).toHaveBeenCalledTimes(1);
    expect(fixture.route).toMatchObject({
      path: "/captain/v1",
      match: "prefix",
      auth: "plugin",
    });
    expect(fixture.api.registerService).toHaveBeenCalledTimes(1);
    expect(fixture.service.id).toBe("captain-remote");
    expect(fixture.api.registerCli).toHaveBeenCalledTimes(1);
    expect(fixture.cliOptions).toMatchObject({ commands: ["captain"] });
  });

  it("returns one fixed unavailable response before readiness and after stop", async () => {
    const fixture = createApi({ path: databasePath() });

    const before = await callRoute(fixture.route.handler);
    expect(before).toEqual(expect.objectContaining({
      statusCode: 503,
      headers: expect.objectContaining({
        "cache-control": "no-store",
        "content-type": "application/json; charset=utf-8",
        "retry-after": "1",
      }),
      body: JSON.stringify({
        error: {
          code: "SERVICE_UNAVAILABLE",
          message: "Captain remote service is unavailable.",
        },
      }),
    }));

    await fixture.service.start(serviceContext(fixture.api));
    await fixture.service.stop?.(serviceContext(fixture.api));
    const after = await callRoute(fixture.route.handler);
    expect(after).toEqual(expect.objectContaining({
      statusCode: 503,
      headers: expect.objectContaining({ "retry-after": "1" }),
      body: before.body,
    }));
  });

  it("fails closed before opening SQLite when exact captain is not configured", async () => {
    const fixture = createApi({ path: databasePath(), agents: [{ id: "Captain" }] });
    const initialize = vi.spyOn(CaptainRemoteStore.prototype, "initialize");

    await expect(fixture.service.start(serviceContext(fixture.api)))
      .rejects.toThrow('requires the configured agent "captain"');

    expect(initialize).not.toHaveBeenCalled();
    expect((await callRoute(fixture.route.handler)).statusCode).toBe(503);
  });

  it("cleans partially initialized resources if startup fails", async () => {
    const fixture = createApi({ path: databasePath() });
    const closeStore = vi.spyOn(CaptainRemoteStore.prototype, "close");
    const closeEvents = vi.spyOn(LimitEventAggregator.prototype, "close");
    vi.spyOn(CaptainRemoteStore.prototype, "initialize").mockImplementation(() => {
      throw new Error("database startup failed");
    });

    await expect(fixture.service.start(serviceContext(fixture.api)))
      .rejects.toThrow("database startup failed");

    expect(closeEvents).toHaveBeenCalledTimes(1);
    expect(closeStore).toHaveBeenCalledTimes(1);
    expect((await callRoute(fixture.route.handler)).statusCode).toBe(503);
  });

  it("rejects unbounded plugin config before opening SQLite", async () => {
    const fixture = createApi({
      path: databasePath(),
      pluginConfig: { maxGlobalRunningTurns: 5 },
    });
    const initialize = vi.spyOn(CaptainRemoteStore.prototype, "initialize");

    await expect(fixture.service.start(serviceContext(fixture.api)))
      .rejects.toThrow("maxGlobalRunningTurns");

    expect(initialize).not.toHaveBeenCalled();
    expect((await callRoute(fixture.route.handler)).statusCode).toBe(503);
  });

  it("starts recovery before readiness and stops worker, aggregation, then SQLite once", async () => {
    const fixture = createApi({ path: databasePath() });
    const recover = vi.spyOn(CaptainRemoteStore.prototype, "recoverStartedTurns");
    const workerStart = vi.spyOn(CaptainTurnWorker.prototype, "start");
    const workerStop = vi.spyOn(CaptainTurnWorker.prototype, "stop");
    const flush = vi.spyOn(LimitEventAggregator.prototype, "flush");
    const closeEvents = vi.spyOn(LimitEventAggregator.prototype, "close");
    const closeStore = vi.spyOn(CaptainRemoteStore.prototype, "close");

    await fixture.service.start(serviceContext(fixture.api));

    expect(workerStart).toHaveBeenCalledTimes(1);
    expect(recover).toHaveBeenCalledTimes(1);
    expect((await callRoute(fixture.route.handler)).statusCode).toBe(404);

    await fixture.service.stop?.(serviceContext(fixture.api));
    await fixture.service.stop?.(serviceContext(fixture.api));

    expect(workerStop).toHaveBeenCalledTimes(1);
    expect(closeEvents).toHaveBeenCalledTimes(1);
    expect(flush).toHaveBeenCalledTimes(1);
    expect(closeStore).toHaveBeenCalledTimes(1);
    expect(workerStop.mock.invocationCallOrder[0]).toBeLessThan(closeEvents.mock.invocationCallOrder[0]);
    expect(closeEvents.mock.invocationCallOrder[0]).toBeLessThan(flush.mock.invocationCallOrder[0]);
    expect(flush.mock.invocationCallOrder[0]).toBeLessThan(closeStore.mock.invocationCallOrder[0]);
  });

  it("flushes one fixed limit summary without letting logger failure block shutdown", async () => {
    const fixture = createApi({ path: databasePath() });
    await fixture.service.start(serviceContext(fixture.api));
    const server = await listen(fixture.route.handler);
    try {
      const response = await fetch(
        `${server.baseUrl}/captain/v1/reports/user-controlled-marker/turns/00000000-0000-4000-8000-000000000001`,
      );
      expect(response.status).toBe(401);
      fixture.logger.warn.mockImplementation(() => {
        throw new Error("logger unavailable");
      });

      await expect(fixture.service.stop?.(serviceContext(fixture.api))).resolves.toBeUndefined();

      expect(fixture.logger.warn).toHaveBeenCalledTimes(1);
      expect(fixture.logger.warn).toHaveBeenCalledWith(JSON.stringify({
        event: "captain_remote_limits",
        auth_failed: 1,
        auth_rate_limited: 0,
        poll_rate_limited: 0,
      }));
      expect(JSON.stringify(fixture.logger.warn.mock.calls)).not.toContain("user-controlled-marker");
      expect((await callRoute(fixture.route.handler)).statusCode).toBe(503);
    } finally {
      await server.close();
    }
  });

  it("adapts only fixed server-owned runtime fields", async () => {
    const path = databasePath();
    const issued = issueMemberToken();
    const store = new CaptainRemoteStore(path);
    store.initialize();
    store.createMember("Sam Lee", "sam@example.com", issued);
    store.close();
    const fixture = createApi({
      path,
      runEmbeddedAgent: async () => ({
        meta: { durationMs: 1, livenessState: "working", stopReason: "stop" },
        payloads: [{ text: JSON.stringify({
          report_id: "adapter-report",
          status: "updated",
          clickup_updates: [],
          captain_feedback: "Recorded.",
          questions: [],
          warnings: [],
        }) }],
      }),
    });
    await fixture.service.start(serviceContext(fixture.api));
    const server = await listen(fixture.route.handler);
    try {
      const response = await fetch(`${server.baseUrl}/captain/v1/reports/adapter-report/turns`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${issued.token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({
          turn_id: "00000000-0000-4000-8000-000000000001",
          kind: "report",
          report: {
            summary: ["Run the configured Captain workflow."],
            context: { cwd: "/client/workspace-marker" },
          },
          metadata: { client: "client-provider-marker" },
        }),
      });
      expect(response.status).toBe(202);
      await waitFor(() => fixture.runEmbeddedAgent.mock.calls.length === 1);

      const params = fixture.runEmbeddedAgent.mock.calls[0][0] as Record<string, unknown>;
      expect(fixture.resolveAgentWorkspaceDir).toHaveBeenCalledWith(fixture.api.config, "captain");
      expect(params.agentId).toBe("captain");
      expect(params.config).toBe(fixture.api.config);
      expect(params.workspaceDir).toBe("/captain/workspace");
      expect(params.sessionTarget).toEqual({
        agentId: "captain",
        sessionId: params.sessionId,
      });
      expect(params.sessionKey).toBe(params.sessionId);
      expect(JSON.stringify(params.sessionTarget)).not.toContain("client-");
      expect(JSON.stringify(params.config)).not.toContain("client-");
      expect(params.workspaceDir).not.toContain("client-");
      for (const forbidden of [
        "provider", "model", "tools", "clientTools", "toolsAllow", "thinkLevel", "thinking",
      ]) {
        expect(params).not.toHaveProperty(forbidden);
      }
      expect(String(params.prompt)).not.toContain("client-provider-marker");
    } finally {
      await fixture.service.stop?.(serviceContext(fixture.api));
      await server.close();
    }
  });
});
