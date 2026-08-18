import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Command } from "commander";
import type { IncomingMessage } from "node:http";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import { afterEach, describe, expect, it, vi } from "vitest";

import { registerCaptainCli } from "../src/cli.js";
import { CaptainAuthenticator } from "../src/security.js";
import { CaptainRemoteStore } from "../src/store.js";

const temporaryDirectories: string[] = [];

afterEach(() => {
  vi.restoreAllMocks();
  process.exitCode = undefined;
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function databasePath(): string {
  const directory = mkdtempSync(join(tmpdir(), "captain-cli-test-"));
  temporaryDirectories.push(directory);
  return join(directory, "captain.sqlite3");
}

function logger() {
  return {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
}

function createApi(path: string) {
  let registrar: Parameters<OpenClawPluginApi["registerCli"]>[0] | undefined;
  let options: Parameters<OpenClawPluginApi["registerCli"]>[1];
  const api = {
    pluginConfig: { databasePath: path },
    logger: logger(),
    registerCli: vi.fn((nextRegistrar, nextOptions) => {
      registrar = nextRegistrar;
      options = nextOptions;
    }),
  } as unknown as OpenClawPluginApi;
  registerCaptainCli(api);
  return {
    api,
    get registrar() {
      if (!registrar) throw new Error("CLI registrar was not registered.");
      return registrar;
    },
    get options() {
      return options;
    },
  };
}

async function runCli(
  apiFixture: ReturnType<typeof createApi>,
  args: string[],
): Promise<{ stdout: string; stderr: string; error?: unknown }> {
  const program = new Command();
  program.exitOverride();
  let stdout = "";
  let stderr = "";
  program.configureOutput({
    writeOut: (value) => {
      stdout += value;
    },
    writeErr: (value) => {
      stderr += value;
    },
  });
  const stdoutWrite = vi.spyOn(process.stdout, "write").mockImplementation((value) => {
    stdout += String(value);
    return true;
  });
  const stderrWrite = vi.spyOn(process.stderr, "write").mockImplementation((value) => {
    stderr += String(value);
    return true;
  });

  await apiFixture.registrar({
    program,
    parentPath: [],
    config: {} as never,
    logger: apiFixture.api.logger,
  });
  let error: unknown;
  try {
    await program.parseAsync(["node", "openclaw", ...args]);
  } catch (caught) {
    error = caught;
  } finally {
    stdoutWrite.mockRestore();
    stderrWrite.mockRestore();
  }
  return { stdout, stderr, error };
}

function authenticate(store: CaptainRemoteStore, token: string): boolean {
  const request = {
    headers: { authorization: `Bearer ${token}` },
    rawHeaders: ["Authorization", `Bearer ${token}`],
    socket: { remoteAddress: "127.0.0.1" },
  } as unknown as IncomingMessage;
  try {
    new CaptainAuthenticator(store).authenticate(request);
    return true;
  } catch {
    return false;
  }
}

describe("Captain member CLI", () => {
  it("registers only the root captain command metadata", () => {
    const fixture = createApi(databasePath());

    expect(fixture.api.registerCli).toHaveBeenCalledTimes(1);
    expect(fixture.options).toMatchObject({ commands: ["captain"] });
  });

  it("adds a member and prints its UUID and raw token once", async () => {
    const path = databasePath();
    const fixture = createApi(path);

    const result = await runCli(fixture, [
      "captain", "members", "add",
      "--name", "Sam Lee",
      "--email", "sam@example.com",
    ]);

    expect(result.error).toBeUndefined();
    const memberId = result.stdout.match(/[0-9a-f]{8}-[0-9a-f-]{27}/i)?.[0];
    const token = result.stdout.match(/cap_v1_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}/)?.[0];
    expect(memberId).toBeDefined();
    expect(token).toBeDefined();
    expect(result.stdout.match(new RegExp(memberId!, "g"))).toHaveLength(1);
    expect(result.stdout.match(new RegExp(token!.replace(".", "\\."), "g"))).toHaveLength(1);
    expect(result.stderr).toBe("");

    const store = new CaptainRemoteStore(path);
    store.initialize();
    expect(store.listMembers()).toEqual([
      expect.objectContaining({
        memberId,
        name: "Sam Lee",
        email: "sam@example.com",
        revokedAt: null,
      }),
    ]);
    expect(authenticate(store, token!)).toBe(true);
    store.close();
    expect(fixture.api.logger.debug).not.toHaveBeenCalled();
    expect(fixture.api.logger.info).not.toHaveBeenCalled();
    expect(fixture.api.logger.warn).not.toHaveBeenCalled();
    expect(fixture.api.logger.error).not.toHaveBeenCalled();
  });

  it("lists public member fields without token, digest, lookup, or secret material", async () => {
    const path = databasePath();
    const fixture = createApi(path);
    const added = await runCli(fixture, [
      "captain", "members", "add",
      "--name", "Sam Lee",
      "--email", "sam@example.com",
    ]);
    const token = added.stdout.match(/cap_v1_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}/)?.[0];

    const listed = await runCli(fixture, ["captain", "members", "list"]);

    expect(listed.error).toBeUndefined();
    expect(listed.stdout).toContain("Sam Lee");
    expect(listed.stdout).toContain("sam@example.com");
    expect(listed.stdout).not.toContain(token!);
    expect(listed.stdout).not.toMatch(/cap_v1_|digest|lookup|secret/i);
  });

  it("rotates once, invalidates the prior token, then revokes immediately", async () => {
    const path = databasePath();
    const fixture = createApi(path);
    const added = await runCli(fixture, [
      "captain", "members", "add",
      "--name", "Sam Lee",
      "--email", "sam@example.com",
    ]);
    const memberId = added.stdout.match(/[0-9a-f]{8}-[0-9a-f-]{27}/i)?.[0];
    const oldToken = added.stdout.match(/cap_v1_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}/)?.[0];

    const rotated = await runCli(fixture, ["captain", "members", "rotate", memberId!]);
    const replacement = rotated.stdout.match(/cap_v1_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}/)?.[0];
    expect(rotated.error).toBeUndefined();
    expect(replacement).toBeDefined();
    expect(replacement).not.toBe(oldToken);
    expect(rotated.stdout.match(new RegExp(replacement!.replace(".", "\\."), "g"))).toHaveLength(1);
    expect(rotated.stdout).not.toContain(oldToken!);

    const store = new CaptainRemoteStore(path);
    store.initialize();
    expect(authenticate(store, oldToken!)).toBe(false);
    expect(authenticate(store, replacement!)).toBe(true);
    store.close();

    const revoked = await runCli(fixture, ["captain", "members", "revoke", memberId!]);
    expect(revoked.error).toBeUndefined();
    expect(revoked.stdout).not.toMatch(/cap_v1_|digest|lookup|secret/i);

    const reopened = new CaptainRemoteStore(path);
    reopened.initialize();
    expect(authenticate(reopened, replacement!)).toBe(false);
    reopened.close();
  });

  it("validates nonempty names and email syntax before creating a store", async () => {
    const emptyNamePath = join(databasePath(), "empty.sqlite3");
    const invalidEmailPath = join(databasePath(), "invalid.sqlite3");

    const emptyName = await runCli(createApi(emptyNamePath), [
      "captain", "members", "add",
      "--name", "   ",
      "--email", "sam@example.com",
    ]);
    const invalidEmail = await runCli(createApi(invalidEmailPath), [
      "captain", "members", "add",
      "--name", "Sam Lee",
      "--email", "not-an-email",
    ]);

    expect(emptyName.error).toBeDefined();
    expect(emptyName.stderr).toContain("Member name is required.");
    expect(invalidEmail.error).toBeDefined();
    expect(invalidEmail.stderr).toContain("Member email is invalid.");
    expect(existsSync(emptyNamePath)).toBe(false);
    expect(existsSync(invalidEmailPath)).toBe(false);
  });

  it("opens and closes one store per command and redacts internal failures", async () => {
    const path = databasePath();
    const fixture = createApi(path);
    const close = vi.spyOn(CaptainRemoteStore.prototype, "close");
    vi.spyOn(CaptainRemoteStore.prototype, "createMember").mockImplementation(() => {
      throw new Error(`cap_v1_PRIVATE_TOKEN ${path}`);
    });

    const result = await runCli(fixture, [
      "captain", "members", "add",
      "--name", "Sam Lee",
      "--email", "sam@example.com",
    ]);

    expect(result.error).toBeDefined();
    expect(close).toHaveBeenCalledTimes(1);
    expect(result.stdout).toBe("");
    expect(result.stderr).toContain("Could not add Captain member.");
    expect(result.stderr).not.toContain("cap_v1_PRIVATE_TOKEN");
    expect(result.stderr).not.toContain(path);
    expect(fixture.api.logger.error).not.toHaveBeenCalled();
  });
});
