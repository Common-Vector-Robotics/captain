import type { OpenClawPluginApi } from 'openclaw/plugin-sdk/plugin-entry';
import { type TurnInput } from './contracts.js';
import { CaptainRemoteStore, type StoredMember } from './store.js';
/** Result shape produced by the host's embedded agent runner. */
export type EmbeddedAgentRunResult = Awaited<ReturnType<OpenClawPluginApi['runtime']['agent']['runEmbeddedAgent']>>;
/** Minimal runtime surface the turn worker needs from the host. */
export interface EmbeddedCaptainRuntime {
    resolveWorkspace(): string;
    run(params: {
        sessionId: string;
        sessionKey: string;
        agentId: 'captain';
        workspaceDir: string;
        prompt: string;
        timeoutMs: number;
        runTimeoutOverrideMs: number;
        runId: string;
        trigger: 'user';
        abortSignal: AbortSignal;
    }): Promise<unknown>;
}
/** Construction options for the Captain turn worker. */
export interface CaptainTurnWorkerOptions {
    store: CaptainRemoteStore;
    runtime: EmbeddedCaptainRuntime;
    timeoutMs: number;
    maxGlobalRunningTurns?: number;
}
/** Builds the fixed prompt for one authenticated Captain turn. */
export declare function buildCaptainPrompt(member: StoredMember, reportId: string, input: TurnInput): string;
/** Collects the visible assistant text from an embedded run result. */
export declare function collectEmbeddedText(result: EmbeddedAgentRunResult): string;
/** Drains queued turns and executes them on the embedded Captain agent. */
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
