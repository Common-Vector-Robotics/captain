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
  LimitEventAggregator,
  PollLimiter,
} from "./security.js";
import { CaptainRemoteStore, type StoredTurn } from "./store.js";

const SUBMIT = /^\/captain\/v1\/reports\/([A-Za-z0-9._-]{1,128})\/turns$/;
const POLL = /^\/captain\/v1\/reports\/([A-Za-z0-9._-]{1,128})\/turns\/([0-9a-f-]{36})$/i;
const AUDIT_TURN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const APPLICATION_JSON = /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?$/i;
const DEFAULT_MAX_REQUEST_BYTES = 262_144;
const BEARER_CHALLENGE = 'Bearer realm="captain"';

export interface HttpDependencies {
  store: CaptainRemoteStore;
  authenticator: CaptainAuthenticator;
  pollLimiter: PollLimiter;
  maxRequestBytes?: number;
  events?: LimitEventAggregator;
  wakeWorker(): void;
}

interface ErrorBody {
  error: {
    code: string;
    message: string;
  };
}

interface JsonResponseOptions {
  retryAfterSeconds?: number;
  allow?: "GET" | "POST";
  authenticate?: boolean;
}

const PUBLIC_PROBLEMS: Readonly<Record<string, ErrorBody["error"]>> = {
  "400:INVALID_JSON": {
    code: "INVALID_JSON",
    message: "Request body must be valid JSON.",
  },
  "400:INVALID_REQUEST": {
    code: "INVALID_REQUEST",
    message: "Request is invalid.",
  },
  "401:UNAUTHORIZED": {
    code: "UNAUTHORIZED",
    message: "Authentication required.",
  },
  "404:NOT_FOUND": {
    code: "NOT_FOUND",
    message: "Captain resource not found.",
  },
  "409:TURN_CONFLICT": {
    code: "TURN_CONFLICT",
    message: "Turn ID already has different content.",
  },
  "413:PAYLOAD_TOO_LARGE": {
    code: "PAYLOAD_TOO_LARGE",
    message: "Request body is too large.",
  },
  "415:UNSUPPORTED_MEDIA_TYPE": {
    code: "UNSUPPORTED_MEDIA_TYPE",
    message: "Content-Type must be application/json.",
  },
  "429:GLOBAL_ACTIVE_LIMIT": {
    code: "GLOBAL_ACTIVE_LIMIT",
    message: "Global active-turn limit reached.",
  },
  "429:MEMBER_ACTIVE_LIMIT": {
    code: "MEMBER_ACTIVE_LIMIT",
    message: "Member already has active work.",
  },
  "429:RATE_LIMITED": {
    code: "RATE_LIMITED",
    message: "Too many requests.",
  },
};

function writeJson(
  res: ServerResponse,
  status: number,
  body: TurnEnvelope | ErrorBody,
  options: JsonResponseOptions = {},
): void {
  res.statusCode = status;
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  if (options.retryAfterSeconds !== undefined) {
    res.setHeader("Retry-After", String(Math.max(1, Math.ceil(options.retryAfterSeconds))));
  }
  if (options.allow) res.setHeader("Allow", options.allow);
  if (options.authenticate) res.setHeader("WWW-Authenticate", BEARER_CHALLENGE);
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

export function readBoundedBody(
  req: IncomingMessage,
  maxRequestBytes: number,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let bytesRead = 0;
    let settled = false;

    const cleanup = () => {
      req.off("data", onData);
      req.off("end", onEnd);
      req.off("close", onClose);
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
    const incompleteBody = () => problem(
      400,
      "INVALID_REQUEST",
      "Request body was incomplete.",
    );
    const onEnd = () => settle(() => {
      if (!req.complete) {
        reject(incompleteBody());
        return;
      }
      resolve(Buffer.concat(chunks, bytesRead));
    });
    const onClose = () => settle(() => reject(
      req.complete
        ? problem(400, "INVALID_REQUEST", "Request body could not be read.")
        : incompleteBody(),
    ));
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
    req.on("close", onClose);
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
    const fixed = PUBLIC_PROBLEMS[`${error.status}:${error.code}`];
    if (fixed) {
      writeJson(res, error.status, { error: fixed }, {
        retryAfterSeconds: retryAfter(error),
        authenticate: error.status === 401,
      });
      return;
    }
  }
  writeJson(res, 500, {
    error: { code: "INTERNAL_ERROR", message: "Internal server error." },
  });
}

function stableAuditCode(error: unknown): string {
  if (error instanceof HttpProblem) {
    return PUBLIC_PROBLEMS[`${error.status}:${error.code}`]?.code ?? "INTERNAL_ERROR";
  }
  return "INTERNAL_ERROR";
}

function isAggregatedLimit(error: unknown): boolean {
  return error instanceof HttpProblem && (
    error.code === "MEMBER_ACTIVE_LIMIT"
    || error.code === "GLOBAL_ACTIVE_LIMIT"
    || error.code === "RATE_LIMITED"
  );
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
    let memberId: string | undefined;
    let operation: "submit" | "poll" | undefined;
    let reportId: string | undefined;
    let turnId: string | undefined;
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
          writeJson(res, 405, {
            error: { code: "METHOD_NOT_ALLOWED", message: "Method not allowed." },
          }, { allow: "POST" });
          return true;
        }
        operation = "submit";
        reportId = submit[1];
        const member = deps.authenticator.authenticate(req);
        memberId = member.memberId;
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
        turnId = input.turn_id;
        const reserved = deps.store.reserveTurn({
          memberId: member.memberId,
          reportId: submit[1],
          turnId: input.turn_id,
          requestDigest: digestTurnInput(input),
          payloadJson: canonicalizeTurnInput(input),
        });
        if (reserved.status === "created") {
          try {
            deps.wakeWorker();
          } catch {
            // The durable queued response remains authoritative if notification fails.
          }
        }

        const status = reserved.turn.state === "queued" || reserved.turn.state === "started"
          ? 202
          : 200;
        writeJson(res, status, envelope(reserved.turn));
        return true;
      }

      if (req.method !== "GET") {
        writeJson(res, 405, {
          error: { code: "METHOD_NOT_ALLOWED", message: "Method not allowed." },
        }, { allow: "GET" });
        return true;
      }
      operation = "poll";
      reportId = poll![1];
      turnId = AUDIT_TURN_ID.test(poll![2]) ? poll![2] : undefined;
      const member = deps.authenticator.authenticate(req);
      memberId = member.memberId;
      deps.pollLimiter.check(member.memberId);
      const turn = deps.store.getTurn({
        memberId: member.memberId,
        reportId: poll![1],
        turnId: poll![2],
      });
      if (!turn) throw problem(404, "NOT_FOUND", "Captain resource not found.");
      deps.store.recordAudit({
        event: "poll_authenticated",
        memberId,
        operation: "poll",
        route: "poll",
        reportId,
        turnId,
        toState: turn.state,
        code: "FOUND",
      });
      writeJson(res, 200, envelope(turn));
      return true;
    } catch (error) {
      if (memberId && operation) {
        if (
          error instanceof HttpProblem
          && ["MEMBER_ACTIVE_LIMIT", "GLOBAL_ACTIVE_LIMIT"].includes(error.code)
        ) {
          deps.events?.record("job_rate_limited");
        }
        if (!isAggregatedLimit(error)) {
          try {
            deps.store.recordAudit({
              event: "http_error",
              memberId,
              operation,
              route: operation,
              reportId,
              turnId,
              code: stableAuditCode(error),
            });
          } catch {
            // Audit I/O must not disclose or replace the fixed public error.
          }
        }
      }
      if (!res.writableEnded && !res.destroyed) writeProblem(res, error);
      return true;
    }
  };
}
