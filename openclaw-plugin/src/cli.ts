import { randomUUID } from "node:crypto";
import { join } from "node:path";

import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";

import { issueMemberToken } from "./security.js";
import { CaptainRemoteStore, normalizeMemberName } from "./store.js";

const EMAIL = /^[\w.!#$%&'*+/=?^`{|}~-]+@[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?(?:\.[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?)*$/i;
const MEMBER_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

interface ErrorCommand {
  error(message: string, options: { exitCode: number; code: string }): never;
}

function databasePath(pluginConfig: Record<string, unknown> | undefined): string {
  const configured = pluginConfig?.databasePath;
  if (configured === undefined) {
    return join(resolveStateDir(), "captain-remote", "captain-remote.sqlite3");
  }
  if (typeof configured !== "string" || configured.trim() === "") {
    throw new TypeError("Captain remote database path is invalid.");
  }
  return configured;
}

async function withStore<T>(
  path: string,
  operation: (store: CaptainRemoteStore) => T | Promise<T>,
): Promise<T> {
  const store = new CaptainRemoteStore(path);
  try {
    store.initialize();
    return await operation(store);
  } finally {
    store.close();
  }
}

function writeStdout(value: string): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const onError = (error: Error) => finish(error);
    const finish = (error?: Error | null) => {
      if (settled) return;
      settled = true;
      if (error) {
        // Node invokes the write callback before its matching error event.
        setImmediate(() => process.stdout.off("error", onError));
        reject(error);
        return;
      }
      process.stdout.off("error", onError);
      resolve();
    };

    process.stdout.once("error", onError);
    try {
      process.stdout.write(value, finish);
    } catch (error) {
      process.stdout.off("error", onError);
      settled = true;
      reject(error);
    }
  });
}

function fail(command: ErrorCommand, message: string, code: string): never {
  return command.error(message, { exitCode: 1, code });
}

export function registerCaptainCli(api: OpenClawPluginApi): void {
  api.registerCli(({ program }) => {
    const captain = program
      .command("captain")
      .description("Manage Captain remote access");
    const members = captain
      .command("members")
      .description("Manage Captain remote members");

    const add = members
      .command("add")
      .description("Add a Captain remote member")
      .requiredOption("--name <name>", "Member display name")
      .requiredOption("--email <email>", "Member email address")
      .action(async (options: { name: string; email: string }) => {
        let name: string;
        try {
          name = normalizeMemberName(options.name);
        } catch {
          const message = options.name.trim()
            ? "Member name is invalid."
            : "Member name is required.";
          fail(add, message, "captain.members.name");
        }
        const email = options.email.trim();
        if (!EMAIL.test(email)) {
          fail(add, "Member email is invalid.", "captain.members.email");
        }

        try {
          const memberId = randomUUID();
          const issued = issueMemberToken();
          await withStore(databasePath(api.pluginConfig), async (store) => {
            await writeStdout(`Member added: ${memberId}\nToken: ${issued.token}\n`);
            store.createMemberWithId(memberId, name, email, issued);
          });
        } catch {
          fail(add, "Could not add Captain member.", "captain.members.add");
        }
      });

    const list = members
      .command("list")
      .description("List Captain remote members")
      .action(async () => {
        try {
          const stored = await withStore(
            databasePath(api.pluginConfig),
            (store) => store.listMembers(),
          );
          for (const member of stored) {
            const status = member.revokedAt ? "revoked" : "active";
            process.stdout.write(`${member.memberId}\t${member.name}\t${member.email}\t${status}\n`);
          }
        } catch {
          fail(list, "Could not list Captain members.", "captain.members.list");
        }
      });

    const rotate = members
      .command("rotate <member-id>")
      .description("Rotate a Captain remote member token")
      .action(async (memberId: string) => {
        if (!MEMBER_ID.test(memberId)) {
          fail(rotate, "Member ID is invalid.", "captain.members.member-id");
        }
        try {
          await withStore(databasePath(api.pluginConfig), async (store) => {
            if (!store.listMembers().some((member) => member.memberId === memberId)) {
              throw new Error("Member not found.");
            }
            const issued = store.prepareMemberRotation(memberId);
            await writeStdout(`Member: ${memberId}\nToken: ${issued.token}\n`);
            store.rotateMember(memberId, issued);
          });
        } catch {
          fail(rotate, "Could not rotate Captain member.", "captain.members.rotate");
        }
      });

    const revoke = members
      .command("revoke <member-id>")
      .description("Revoke a Captain remote member")
      .action(async (memberId: string) => {
        if (!MEMBER_ID.test(memberId)) {
          fail(revoke, "Member ID is invalid.", "captain.members.member-id");
        }
        try {
          await withStore(databasePath(api.pluginConfig), (store) => {
            store.revokeMember(memberId);
          });
          process.stdout.write(`Member revoked: ${memberId}\n`);
        } catch {
          fail(revoke, "Could not revoke Captain member.", "captain.members.revoke");
        }
      });
  }, {
    descriptors: [{
      name: "captain",
      description: "Manage Captain remote access",
      hasSubcommands: true,
    }],
  });
}
