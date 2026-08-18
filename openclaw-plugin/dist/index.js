import { join } from "node:path";
import { definePluginEntry, } from "openclaw/plugin-sdk/plugin-entry";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import { registerCaptainCli } from "./cli.js";
import { createCaptainHttpHandler } from "./http.js";
import { CaptainTurnWorker } from "./runtime.js";
import { CaptainAuthenticator, LimitEventAggregator, PollLimiter, } from "./security.js";
import { CaptainRemoteStore } from "./store.js";
const NUMERIC_CONFIG = {
    maxRequestBytes: 262_144,
    executionTimeoutMs: 300_000,
    maxGlobalActiveTurns: 32,
    maxGlobalRunningTurns: 4,
    pollPerMinute: 30,
    pollBurst: 5,
    invalidAuthPerSourcePerMinute: 10,
    invalidAuthPerSourceBurst: 5,
    invalidAuthGlobalPerMinute: 100,
};
const CONFIG_KEYS = new Set(["databasePath", ...Object.keys(NUMERIC_CONFIG)]);
function resolveConfig(pluginConfig) {
    const supplied = pluginConfig ?? {};
    for (const key of Object.keys(supplied)) {
        if (!CONFIG_KEYS.has(key))
            throw new TypeError(`Unknown Captain remote config field: ${key}.`);
    }
    const configuredPath = supplied.databasePath;
    if (configuredPath !== undefined && (typeof configuredPath !== "string" || configuredPath.trim() === "")) {
        throw new TypeError("databasePath must be a nonempty string.");
    }
    const resolved = {
        databasePath: configuredPath
            ?? join(resolveStateDir(), "captain-remote", "captain-remote.sqlite3"),
    };
    for (const [key, maximum] of Object.entries(NUMERIC_CONFIG)) {
        const value = supplied[key] ?? maximum;
        if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
            throw new TypeError(`${key} must be an integer between 1 and ${maximum}.`);
        }
        resolved[key] = value;
    }
    return resolved;
}
function hasCaptainAgent(api) {
    return api.config.agents?.list?.some((agent) => agent.id === "captain") === true;
}
function writeUnavailable(res) {
    res.statusCode = 503;
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.setHeader("Retry-After", "1");
    res.end(JSON.stringify({
        error: {
            code: "SERVICE_UNAVAILABLE",
            message: "Captain remote service is unavailable.",
        },
    }));
    return true;
}
function emitLimitCounts(api, counts) {
    if (!Object.values(counts).some((count) => count > 0))
        return;
    try {
        api.logger.warn(JSON.stringify({ event: "captain_remote_limits", ...counts }));
    }
    catch {
        // Logging must not prevent service shutdown or resource cleanup.
    }
}
async function closeResources(api, resources) {
    try {
        await resources.worker?.stop();
    }
    finally {
        resources.events.close();
        const counts = resources.events.flush();
        try {
            emitLimitCounts(api, counts);
        }
        finally {
            resources.store.close();
        }
    }
}
function registerRuntime(api) {
    const lifecycle = {};
    api.registerHttpRoute({
        path: "/captain/v1",
        match: "prefix",
        auth: "plugin",
        handler: (req, res) => lifecycle.handler?.(req, res) ?? writeUnavailable(res),
    });
    api.registerService({
        id: "captain-remote",
        async start() {
            if (lifecycle.resources)
                return;
            if (!hasCaptainAgent(api)) {
                throw new Error('Captain remote requires the configured agent "captain".');
            }
            const config = resolveConfig(api.pluginConfig);
            const events = new LimitEventAggregator({
                emit: (counts) => emitLimitCounts(api, counts),
            });
            const store = new CaptainRemoteStore(config.databasePath);
            const resources = { store, events };
            try {
                store.initialize();
                const authenticator = new CaptainAuthenticator(store, { events });
                const pollLimiter = new PollLimiter({ events });
                const embeddedRuntime = {
                    resolveWorkspace: () => api.runtime.agent.resolveAgentWorkspaceDir(api.config, "captain"),
                    run: (params) => api.runtime.agent.runEmbeddedAgent({
                        ...params,
                        config: api.config,
                        sessionTarget: { agentId: "captain", sessionId: params.sessionId },
                    }),
                };
                const worker = new CaptainTurnWorker({
                    store,
                    runtime: embeddedRuntime,
                    timeoutMs: config.executionTimeoutMs,
                    maxGlobalRunningTurns: config.maxGlobalRunningTurns,
                });
                resources.worker = worker;
                const handler = createCaptainHttpHandler({
                    store,
                    authenticator,
                    pollLimiter,
                    maxRequestBytes: config.maxRequestBytes,
                    wakeWorker: () => worker.wake(),
                });
                lifecycle.resources = resources;
                worker.start();
                lifecycle.handler = handler;
            }
            catch (error) {
                lifecycle.handler = undefined;
                lifecycle.resources = undefined;
                await closeResources(api, resources);
                throw error;
            }
        },
        async stop() {
            lifecycle.handler = undefined;
            if (lifecycle.stopPromise)
                return lifecycle.stopPromise;
            const resources = lifecycle.resources;
            lifecycle.resources = undefined;
            if (!resources)
                return;
            lifecycle.stopPromise = closeResources(api, resources).finally(() => {
                lifecycle.stopPromise = undefined;
            });
            return lifecycle.stopPromise;
        },
    });
}
const plugin = definePluginEntry({
    id: "captain-remote",
    name: "Captain Remote",
    description: "Authenticated Captain-only report ingress for coding agents.",
    register(api) {
        registerRuntime(api);
        registerCaptainCli(api);
    },
});
export default plugin;
