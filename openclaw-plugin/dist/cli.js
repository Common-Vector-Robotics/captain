import { join } from "node:path";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import { issueMemberToken } from "./security.js";
import { CaptainRemoteStore } from "./store.js";
const EMAIL = /^[\w.!#$%&'*+/=?^`{|}~-]+@[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?(?:\.[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?)*$/i;
const MEMBER_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
function databasePath(pluginConfig) {
    const configured = pluginConfig?.databasePath;
    if (configured === undefined) {
        return join(resolveStateDir(), "captain-remote", "captain-remote.sqlite3");
    }
    if (typeof configured !== "string" || configured.trim() === "") {
        throw new TypeError("Captain remote database path is invalid.");
    }
    return configured;
}
function withStore(path, operation) {
    const store = new CaptainRemoteStore(path);
    try {
        store.initialize();
        return operation(store);
    }
    finally {
        store.close();
    }
}
function fail(command, message, code) {
    return command.error(message, { exitCode: 1, code });
}
export function registerCaptainCli(api) {
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
            .action((options) => {
            const name = options.name.trim();
            const email = options.email.trim();
            if (!name)
                fail(add, "Member name is required.", "captain.members.name");
            if (!EMAIL.test(email)) {
                fail(add, "Member email is invalid.", "captain.members.email");
            }
            try {
                const issued = issueMemberToken();
                const member = withStore(databasePath(api.pluginConfig), (store) => (store.createMember(name, email, issued)));
                process.stdout.write(`Member added: ${member.memberId}\nToken: ${issued.token}\n`);
            }
            catch {
                fail(add, "Could not add Captain member.", "captain.members.add");
            }
        });
        const list = members
            .command("list")
            .description("List Captain remote members")
            .action(() => {
            try {
                const stored = withStore(databasePath(api.pluginConfig), (store) => store.listMembers());
                for (const member of stored) {
                    const status = member.revokedAt ? "revoked" : "active";
                    process.stdout.write(`${member.memberId}\t${member.name}\t${member.email}\t${status}\n`);
                }
            }
            catch {
                fail(list, "Could not list Captain members.", "captain.members.list");
            }
        });
        const rotate = members
            .command("rotate <member-id>")
            .description("Rotate a Captain remote member token")
            .action((memberId) => {
            if (!MEMBER_ID.test(memberId)) {
                fail(rotate, "Member ID is invalid.", "captain.members.member-id");
            }
            try {
                const issued = issueMemberToken();
                withStore(databasePath(api.pluginConfig), (store) => {
                    store.rotateMember(memberId, issued);
                });
                process.stdout.write(`Token: ${issued.token}\n`);
            }
            catch {
                fail(rotate, "Could not rotate Captain member.", "captain.members.rotate");
            }
        });
        const revoke = members
            .command("revoke <member-id>")
            .description("Revoke a Captain remote member")
            .action((memberId) => {
            if (!MEMBER_ID.test(memberId)) {
                fail(revoke, "Member ID is invalid.", "captain.members.member-id");
            }
            try {
                withStore(databasePath(api.pluginConfig), (store) => {
                    store.revokeMember(memberId);
                });
                process.stdout.write(`Member revoked: ${memberId}\n`);
            }
            catch {
                fail(revoke, "Could not revoke Captain member.", "captain.members.revoke");
            }
        });
    }, {
        commands: ["captain"],
        descriptors: [{
                name: "captain",
                description: "Manage Captain remote access",
                hasSubcommands: true,
            }],
    });
}
