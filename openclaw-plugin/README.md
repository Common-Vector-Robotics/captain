# Captain Remote OpenClaw plugin

This native OpenClaw plugin gives authenticated team members a Captain-only
HTTPS route. It accepts durable coding-session reports and exact follow-up
replies, then runs the server's `captain` agent with server-owned sessions and
runtime settings.

Members do not receive SSH, OpenClaw Gateway credentials, member-management
access, or a way to select another agent, model, workspace, session, or tool.
Possession of a member token grants the same communication authority the
operator would give that person when messaging Captain over Slack.

## Requirements

- OpenClaw `2026.7.2-beta.5` or a compatible later host.
- An installed OpenClaw agent whose exact ID is `captain`.
- Node.js in the range declared by `package.json` when building from source.
- Nginx with its HTTP SSL, request-limit, and connection-limit modules.
- An operator-managed hostname, TLS certificate, and private key.

The OpenClaw Gateway must remain bound to loopback. Nginx is the public
listener and proxies only `/captain/v1/` to the Gateway.

## Build and install a pinned package

Review and check out a specific Captain tag or commit before building. From the
repository root, run:

```bash
npm --prefix openclaw-plugin ci
npm --prefix openclaw-plugin run check
npm --prefix openclaw-plugin test
npm --prefix openclaw-plugin run build

CAPTAIN_PLUGIN_PACK_DIR="$(mktemp -d)"
npm pack ./openclaw-plugin --pack-destination "$CAPTAIN_PLUGIN_PACK_DIR"
```

Inspect the generated tarball, then install that exact artifact using the
`npm-pack:` package source printed by the previous command:

```bash
openclaw plugins install \
  npm-pack:/path/to/common-vector-robotics-captain-remote-0.1.0.tgz \
  --force
openclaw gateway restart
openclaw plugins inspect captain-remote --runtime --json
```

The runtime inspection must show plugin ID `captain-remote`, the
`/captain/v1` prefix route with plugin authentication, the `captain-remote`
service, and the `captain` CLI command. Stop if the plugin is not loaded or the
host reports an incompatible plugin API.

Remove the temporary package directory after the install is verified.

## Keep the Gateway on loopback

Check the configured bind and live Gateway before opening any public ingress:

```bash
openclaw config get gateway.bind
openclaw gateway status --deep --require-rpc
```

`gateway.bind` must be `loopback`. Do not continue if the Gateway listens on a
LAN, tailnet, custom public address, or `0.0.0.0`. Port `18789` must be reachable
only from the Captain host.

The plugin does not use or distribute the Gateway authentication token. Member
tokens authenticate only the Captain plugin route.

## Configure the HTTPS ingress

Copy `examples/nginx-captain-remote.conf` into Nginx's HTTP configuration.
Replace these example values:

- `captain.example.com`
- `/etc/nginx/captain-remote.crt`
- `/etc/nginx/captain-remote.key`

The example enforces the v1 public boundary:

- HTTPS on port 443.
- Only `/captain/v1/` proxies to `127.0.0.1:18789`.
- Every other path returns `404` without reaching OpenClaw.
- 300 requests per minute per source with a burst of 30.
- 20 active requests per source and 100 total for the virtual server.
- A 256 KiB request body and 16 KiB of large-header buffers.
- 10-second header reads, 15-second body reads, 30-second writes, and
  30-second idle connections.
- Buffered request bodies and a 30-second upstream read timeout.
- One proxy-observed `X-Captain-Client-IP` value. Client forwarding headers are
  removed before proxying.
- An access-log format that omits the Authorization header and query string.

Validate the complete target-host configuration before reload:

```bash
sudo nginx -t
sudo nginx -s reload
```

Do not add another proxy location, catch-all, or HTTP listener that forwards to
port `18789`. If another trusted ingress sits in front of Nginx, it must retain
equivalent source-rate, connection, header-size, and slow-client limits.

## Add and manage members

Member administration runs only on the Captain host. A display name and email
are both required:

```bash
openclaw captain members add --name "Sam Lee" --email sam@example.com
openclaw captain members list
openclaw captain members rotate <member-id>
openclaw captain members revoke <member-id>
```

`add` prints the member UUID and raw token once. Deliver the token using your
team's credential-sharing method, then clear any temporary copy. Do not place a
member token in a URL, query string, ticket, chat log, shell-history command, or
proxy log.

`list` shows member identity and lifecycle metadata without token material.
`rotate` invalidates the old token and prints one replacement. `revoke`
immediately denies submissions, replies, and polls while retaining durable
records for operator review.

The database stores a token lookup ID and SHA-256 digest, not the raw token.
The default database is in the OpenClaw state directory at
`captain-remote/captain-remote.sqlite3`. An operator may set the plugin's
`databasePath` to another owner-only location.

## Configure a coding-agent member

Install the repository's `agent-plugin` package on the member's computer. Set
both remote values in the coding platform's supported secret or environment
configuration:

```text
CAPTAIN_REMOTE_URL=https://captain.example.com
CAPTAIN_MEMBER_TOKEN=<the member's one-time token>
```

The two values are a pair. With both present, the coding-agent package uses the
Captain HTTPS API. With neither present, it keeps the existing local OpenClaw
CLI path. With only one present, it returns `needs_configuration`.

Run `/captain`, or `/captain:captain` in Claude Code, after completing a small
test task. The coding agent submits asynchronously and polls the same durable
turn ID. If Captain asks a question, a clear later user-authored answer can be
forwarded verbatim without invoking `/captain` again.

## Verify the boundary

From a computer other than the Captain host, verify the negative paths before
admitting members:

```bash
curl -i https://captain.example.com/
curl -i https://captain.example.com/v1/responses
curl -i https://captain.example.com/hooks/test
curl -i https://captain.example.com/tools
curl -i https://captain.example.com/api/admin
curl -i -X POST https://captain.example.com/captain/v1/reports/check/turns
```

The first five requests must return `404`. The Captain request without a member
token must return `401`. Confirm that port `18789` is unreachable from the
remote computer. Review Nginx and OpenClaw logs and confirm that no raw test
token appears.

After a successful test report and poll, rotate the member token and confirm the
old value receives `401`. Revoke the test member and confirm reports, replies,
and polls are denied.

## Restart, recovery, and `unknown_outcome`

Accepted `queued` turns are durable and may start after a restart. A turn that
already reached `started` is never run automatically again after process
recovery. The plugin marks that turn `unknown_outcome` because Captain may have
acted before the interruption.

When a client receives `unknown_outcome`, inspect Captain's audited records and
ClickUp before deciding whether to create a new turn. Retrying the old turn does
not rerun it. A client polling timeout also keeps the same turn ID and must not
create replacement work.

## Backup and upgrade

The SQLite database contains member identities, token digests, report ownership,
server session IDs, and turn results. Treat it as private operational data.
Before backup or restore, stop the Gateway so the plugin closes SQLite. Copy the
entire `captain-remote` state directory, including any SQLite companion files,
to an owner-restricted backup. Restart the Gateway and repeat runtime inspection.

For an upgrade, build or obtain a newly pinned package, inspect its contents,
back up the database, install the exact artifact, restart, and run the boundary
checks again. Never use a production upgrade as the first syntax test for the
Nginx example.
