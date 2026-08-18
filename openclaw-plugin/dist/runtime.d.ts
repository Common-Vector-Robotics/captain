import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import { type TurnInput } from "./contracts.js";
import { CaptainRemoteStore, type StoredMember } from "./store.js";
export type EmbeddedAgentRunResult = Awaited<ReturnType<OpenClawPluginApi["runtime"]["agent"]["runEmbeddedAgent"]>>;
export interface EmbeddedCaptainRuntime {
    resolveWorkspace(): string;
    run(params: {
        sessionId: string;
        sessionKey: string;
        agentId: "captain";
        workspaceDir: string;
        prompt: string;
        timeoutMs: number;
        runTimeoutOverrideMs: number;
        runId: string;
        trigger: "user";
        abortSignal: AbortSignal;
    }): Promise<unknown>;
}
export interface CaptainTurnWorkerOptions {
    store: CaptainRemoteStore;
    runtime: EmbeddedCaptainRuntime;
    timeoutMs: number;
    maxGlobalRunningTurns?: number;
}
export declare function buildCaptainPrompt(member: StoredMember, reportId: string, input: TurnInput): string;
export declare function collectEmbeddedText(result: EmbeddedAgentRunResult): string;
export declare class CaptainTurnWorker {
    private readonly store;
    private readonly runtime;
    private readonly timeoutMs;
    private readonly maxGlobalRunningTurns;
    private readonly active;
    private readonly controllers;
    private started;
    private stopped;
    private drainScheduled;
    private draining;
    private stopPromise;
    constructor(options: CaptainTurnWorkerOptions);
    start(): void;
    wake(): void;
    stop(): Promise<void>;
    private scheduleDrain;
    private drain;
    private runTurn;
    private executeTurn;
}
