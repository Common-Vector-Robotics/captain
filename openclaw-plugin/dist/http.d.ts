import type { IncomingMessage } from "node:http";
import type { OpenClawPluginHttpRouteHandler } from "openclaw/plugin-sdk/plugin-entry";
import { CaptainAuthenticator, LimitEventAggregator, PollLimiter } from "./security.js";
import { CaptainRemoteStore } from "./store.js";
export interface HttpDependencies {
    store: CaptainRemoteStore;
    authenticator: CaptainAuthenticator;
    pollLimiter: PollLimiter;
    maxRequestBytes?: number;
    events?: LimitEventAggregator;
    wakeWorker(): void;
}
export declare function readBoundedBody(req: IncomingMessage, maxRequestBytes: number): Promise<Buffer>;
export declare function createCaptainHttpHandler(deps: HttpDependencies): OpenClawPluginHttpRouteHandler;
