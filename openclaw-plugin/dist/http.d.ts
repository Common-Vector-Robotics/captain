import type { IncomingMessage } from 'node:http';
import type { OpenClawPluginHttpRouteHandler } from 'openclaw/plugin-sdk/plugin-entry';
import { CaptainAuthenticator, LimitEventAggregator, PollLimiter } from './security.js';
import { CaptainRemoteStore } from './store.js';
/** Collaborators required by the Captain HTTP handler. */
export interface HttpDependencies {
    store: CaptainRemoteStore;
    authenticator: CaptainAuthenticator;
    pollLimiter: PollLimiter;
    maxRequestBytes?: number;
    events?: LimitEventAggregator;
    wakeWorker(): void;
}
/** Reads a request body, rejecting once it exceeds the byte budget. */
export declare function readBoundedBody(req: IncomingMessage, maxRequestBytes: number): Promise<Buffer>;
/** Creates the authenticated Captain submit-and-poll HTTP handler. */
export declare function createCaptainHttpHandler(deps: HttpDependencies): OpenClawPluginHttpRouteHandler;
