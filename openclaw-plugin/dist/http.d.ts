import type { OpenClawPluginHttpRouteHandler } from "openclaw/plugin-sdk/plugin-entry";
import { CaptainAuthenticator, PollLimiter } from "./security.js";
import { CaptainRemoteStore } from "./store.js";
export interface HttpDependencies {
    store: CaptainRemoteStore;
    authenticator: CaptainAuthenticator;
    pollLimiter: PollLimiter;
    maxRequestBytes?: number;
    wakeWorker(): void;
}
export declare function createCaptainHttpHandler(deps: HttpDependencies): OpenClawPluginHttpRouteHandler;
