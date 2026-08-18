import type { IncomingMessage, ServerResponse } from "node:http";
import type { OpenClawPluginHttpRouteHandler } from "openclaw/plugin-sdk/plugin-entry";

import {
  HttpProblem,
  canonicalizeTurnInput,
  digestTurnInput,
  parseTurnInput,
  type TurnEnvelope,
} from "./contracts.js";
import {
  CaptainAuthenticator,
  PollLimiter,
} from "./security.js";
import { CaptainRemoteStore, type StoredTurn } from "./store.js";

const SUBMIT = /^\/captain\/v1\/reports\/([A-Za-z0-9._-]{1,128})\/turns$/;
const POLL = /^\/captain\/v1\/reports\/([A-Za-z0-9._-]{1,128})\/turns\/([0-9a-f-]{36})$/i;
const APPLICATION_JSON = /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?$/i;
const DEFAULT_MAX_REQUEST_BYTES = 262_144;

export interface HttpDependencies {
  store: CaptainRemoteStore;
  authenticator: CaptainAuthenticator;
  pollLimiter: PollLimiter;
  maxRequestBytes?: number;
  wakeWorker(): void;
}

interface ErrorBody {
  error: {
    code: string;
    message: string;
  };
}

function writeJson(
  res: ServerResponse,
  status: number,
  body: TurnEnvelope | ErrorBody,
  retryAfterSeconds?: number,
): void {
  res.statusCode = status;
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  if (retryAfterSeconds !== undefined) {
    res.setHeader("Retry-After", String(Math.max(1, Math.ceil(retryAfterSeconds))));
  }
  res.end(JSON.stringify(body));
}

function problem(status: number, code: string, message: string): HttpProblem {
  return new HttpProblem(status, code, message);
}

function contentTypeIsJson(req: IncomingMessage): boolean {
  const values = req.headersDistinct?.["content-type"];
  if (values && values.length !== 1) return false;
  const value = values?.[0] ?? req.headers["content-type"];
  return typeof value === "string" && APPLICATION_JSON.test(value.trim());
}

function readBoundedBody(req: IncomingMessage, maxRequestBytes: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let bytesRead = 0;
    let settled = false;

    const cleanup = () => {
      req.off("data", onData);
      req.off("end", onEnd);
      req.off("aborted", onAborted);
      req.off("error", onError);
    };
    const settle = (operation: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      operation();
    };
    const onData = (value: Buffer | string) => {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
      const remaining = maxRequestBytes + 1 - bytesRead;
      if (chunk.length >= remaining) {
        if (remaining > 0) chunks.push(chunk.subarray(0, remaining));
        bytesRead += Math.max(0, remaining);
        settle(() => reject(problem(
          413,
          "PAYLOAD_TOO_LARGE",
          "Request body is too large.",
        )));
        // Drain without retaining bytes so the server can safely reuse the connection.
        req.resume();
        return;
      }
      chunks.push(chunk);
      bytesRead += chunk.length;
    };
    const onEnd = () => settle(() => resolve(Buffer.concat(chunks, bytesRead)));
    const onAborted = () => settle(() => reject(problem(
      400,
      "INVALID_REQUEST",
      "Request body was incomplete.",
    )));
    const onError = () => settle(() => reject(problem(
      400,
      "INVALID_REQUEST",
      "Request body could not be read.",
    )));

    req.on("data", onData);
    req.on("end", onEnd);
    req.on("aborted", onAborted);
    req.on("error", onError);
  });
}

function envelope(turn: StoredTurn): TurnEnvelope {
  const value: TurnEnvelope = {
    report_id: turn.reportId,
    turn_id: turn.turnId,
    turn_status: turn.state,
  };
  if (turn.result) value.result = turn.result;
  if (turn.error) value.error = turn.error;
  return value;
}

function retryAfter(error: HttpProblem): number | undefined {
  if (error.status !== 429) return undefined;
  const value = "retryAfterSeconds" in error
    ? (error as HttpProblem & { retryAfterSeconds?: unknown }).retryAfterSeconds
    : undefined;
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.ceil(value)
    : 1;
}

function writeProblem(res: ServerResponse, error: unknown): void {
  if (error instanceof HttpProblem) {
    writeJson(res, error.status, {
      error: { code: error.code, message: error.message },
    }, retryAfter(error));
    return;
  }
  writeJson(res, 500, {
    error: { code: "INTERNAL_ERROR", message: "Internal server error." },
  });
}

function validateMaxRequestBytes(value: number | undefined): number {
  const resolved = value ?? DEFAULT_MAX_REQUEST_BYTES;
  if (!Number.isSafeInteger(resolved) || resolved <= 0) {
    throw new TypeError("maxRequestBytes must be a positive safe integer.");
  }
  return resolved;
}

export function createCaptainHttpHandler(
  deps: HttpDependencies,
): OpenClawPluginHttpRouteHandler {
  const maxRequestBytes = validateMaxRequestBytes(deps.maxRequestBytes);

  return async (req, res) => {
    try {
      const target = req.url ?? "";
      if (target.includes("?")) {
        throw problem(404, "NOT_FOUND", "Captain resource not found.");
      }

      const submit = SUBMIT.exec(target);
      const poll = POLL.exec(target);
      if (!submit && !poll) {
        throw problem(404, "NOT_FOUND", "Captain resource not found.");
      }

      if (submit) {
        if (req.method !== "POST") {
          throw problem(405, "METHOD_NOT_ALLOWED", "Method not allowed.");
        }
        const member = deps.authenticator.authenticate(req);
        if (!contentTypeIsJson(req)) {
          throw problem(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.");
        }

        const body = await readBoundedBody(req, maxRequestBytes);
        let decoded: unknown;
        try {
          decoded = JSON.parse(body.toString("utf8"));
        } catch {
          throw problem(400, "INVALID_JSON", "Request body must be valid JSON.");
        }
        const input = parseTurnInput(decoded);
        const reserved = deps.store.reserveTurn({
          memberId: member.memberId,
          reportId: submit[1],
          turnId: input.turn_id,
          requestDigest: digestTurnInput(input),
          payloadJson: canonicalizeTurnInput(input),
        });
        if (reserved.status === "created") deps.wakeWorker();

        const status = reserved.turn.state === "queued" || reserved.turn.state === "started"
          ? 202
          : 200;
        writeJson(res, status, envelope(reserved.turn));
        return true;
      }

      if (req.method !== "GET") {
        throw problem(405, "METHOD_NOT_ALLOWED", "Method not allowed.");
      }
      const member = deps.authenticator.authenticate(req);
      deps.pollLimiter.check(member.memberId);
      const turn = deps.store.getTurn({
        memberId: member.memberId,
        reportId: poll![1],
        turnId: poll![2],
      });
      if (!turn) throw problem(404, "NOT_FOUND", "Captain resource not found.");
      writeJson(res, 200, envelope(turn));
      return true;
    } catch (error) {
      if (!res.writableEnded && !res.destroyed) writeProblem(res, error);
      return true;
    }
  };
}
