import { randomUUID } from "node:crypto";
import { chmodSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";

import {
  HttpProblem,
  type CaptainResult,
  type TurnInput,
  type TurnState,
} from "./contracts.js";
import type { IssuedToken } from "./security.js";

export interface StoredMember {
  memberId: string;
  name: string;
  email: string;
  createdAt: string;
  rotatedAt: string | null;
  revokedAt: string | null;
}

export interface StoredMemberAuth extends StoredMember {
  lookupId: string;
  digest: Buffer;
}

export interface TurnKey {
  memberId: string;
  reportId: string;
  turnId: string;
}

export interface StoredReport {
  memberId: string;
  reportId: string;
  sessionId: string;
  createdAt: string;
  updatedAt: string;
}

export type TerminalTurnState = Exclude<TurnState, "queued" | "started">;

const TERMINAL_TURN_STATES = new Set<TerminalTurnState>([
  "succeeded",
  "failed",
  "timed_out",
  "unknown_outcome",
]);
const SQLITE_BUSY_TIMEOUT_MS = 5_000;
const SQLITE_BUSY_RETRY_MS = 10;
const SQLITE_BUSY_WAIT = new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
const DEFAULT_MAX_GLOBAL_ACTIVE_TURNS = 32;
const MAX_GLOBAL_ACTIVE_TURNS = 32;

export interface CaptainRemoteStoreOptions {
  maxGlobalActiveTurns?: number;
}

export interface StoredTurnError {
  code: string;
  message: string;
}

export interface StoredTurn extends TurnKey {
  kind: TurnInput["kind"];
  requestDigest: string;
  payload: TurnInput;
  state: TurnState;
  runId: string | null;
  result: CaptainResult | null;
  error: StoredTurnError | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface ClaimedTurn extends StoredTurn {
  member: StoredMember;
  report: StoredReport;
}

export interface ReserveTurnInput extends TurnKey {
  requestDigest: string;
  payloadJson: string;
}

export type ReserveTurnResult = {
  status: "created" | "existing";
  report: StoredReport;
  turn: StoredTurn;
};

interface MemberRow {
  member_id: string;
  name: string;
  email: string;
  token_lookup_id: string;
  token_digest: Uint8Array;
  created_at: string;
  rotated_at: string | null;
  revoked_at: string | null;
}

interface ReportRow {
  member_id: string;
  report_id: string;
  session_id: string;
  created_at: string;
  updated_at: string;
}

interface TurnRow {
  member_id: string;
  report_id: string;
  turn_id: string;
  kind: TurnInput["kind"];
  request_digest: string;
  payload_json: string;
  state: TurnState;
  run_id: string | null;
  result_json: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

function storedMember(row: MemberRow): StoredMember {
  return {
    memberId: row.member_id,
    name: row.name,
    email: row.email,
    createdAt: row.created_at,
    rotatedAt: row.rotated_at,
    revokedAt: row.revoked_at,
  };
}

function storedReport(row: ReportRow): StoredReport {
  return {
    memberId: row.member_id,
    reportId: row.report_id,
    sessionId: row.session_id,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function storedTurn(row: TurnRow): StoredTurn {
  return {
    memberId: row.member_id,
    reportId: row.report_id,
    turnId: row.turn_id,
    kind: row.kind,
    requestDigest: row.request_digest,
    payload: JSON.parse(row.payload_json) as TurnInput,
    state: row.state,
    runId: row.run_id,
    result: row.result_json ? JSON.parse(row.result_json) as CaptainResult : null,
    error: row.error_code !== null && row.error_message !== null
      ? { code: row.error_code, message: row.error_message }
      : null,
    createdAt: row.created_at,
    startedAt: row.started_at,
    finishedAt: row.finished_at,
  };
}

function required(value: string, field: string): string {
  const normalized = value.trim();
  if (!normalized) throw new TypeError(`${field} is required.`);
  return normalized;
}

function isLockedDatabase(error: unknown): boolean {
  return error instanceof Error
    && error.message === "database is locked"
    && "code" in error
    && error.code === "ERR_SQLITE_ERROR";
}

function enableWal(database: DatabaseSync): void {
  const deadline = Date.now() + SQLITE_BUSY_TIMEOUT_MS;
  while (true) {
    try {
      const journal = database.prepare("PRAGMA journal_mode = WAL").get() as {
        journal_mode?: unknown;
      };
      if (journal.journal_mode !== "wal") {
        throw new Error("Captain remote database requires WAL mode.");
      }
      return;
    } catch (error) {
      if (!isLockedDatabase(error) || Date.now() >= deadline) {
        throw new Error("Captain remote database requires WAL mode.");
      }
      Atomics.wait(SQLITE_BUSY_WAIT, 0, 0, SQLITE_BUSY_RETRY_MS);
    }
  }
}

export class CaptainRemoteStore {
  private database: DatabaseSync | null = null;
  private readonly maxGlobalActiveTurns: number;

  constructor(
    private readonly databasePath: string,
    options: CaptainRemoteStoreOptions = {},
  ) {
    const maxGlobalActiveTurns = options.maxGlobalActiveTurns
      ?? DEFAULT_MAX_GLOBAL_ACTIVE_TURNS;
    if (
      !Number.isSafeInteger(maxGlobalActiveTurns)
      || maxGlobalActiveTurns < 1
      || maxGlobalActiveTurns > MAX_GLOBAL_ACTIVE_TURNS
    ) {
      throw new TypeError(
        `maxGlobalActiveTurns must be between 1 and ${MAX_GLOBAL_ACTIVE_TURNS}.`,
      );
    }
    this.maxGlobalActiveTurns = maxGlobalActiveTurns;
  }

  initialize(): void {
    if (this.database) return;

    const directory = dirname(this.databasePath);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    chmodSync(directory, 0o700);

    const database = new DatabaseSync(this.databasePath, { timeout: SQLITE_BUSY_TIMEOUT_MS });
    try {
      chmodSync(this.databasePath, 0o600);
      database.exec(`
        PRAGMA foreign_keys = ON;
        PRAGMA busy_timeout = 5000;
      `);

      enableWal(database);

      database.exec(`
        CREATE TABLE IF NOT EXISTS members (
          member_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          email TEXT NOT NULL,
          token_lookup_id TEXT NOT NULL UNIQUE,
          token_digest BLOB NOT NULL,
          created_at TEXT NOT NULL,
          rotated_at TEXT,
          revoked_at TEXT
        ) STRICT;

        CREATE TABLE IF NOT EXISTS reports (
          member_id TEXT NOT NULL,
          report_id TEXT NOT NULL,
          session_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (member_id, report_id),
          FOREIGN KEY (member_id) REFERENCES members(member_id)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS turns (
          member_id TEXT NOT NULL,
          report_id TEXT NOT NULL,
          turn_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          request_digest TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          state TEXT NOT NULL CHECK (
            state IN ('queued', 'started', 'succeeded', 'failed', 'timed_out', 'unknown_outcome')
          ),
          run_id TEXT,
          result_json TEXT,
          error_code TEXT,
          error_message TEXT,
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          PRIMARY KEY (member_id, report_id, turn_id),
          FOREIGN KEY (member_id, report_id) REFERENCES reports(member_id, report_id)
        ) STRICT;

        CREATE INDEX IF NOT EXISTS turns_state_created
          ON turns(state, created_at, member_id, report_id, turn_id);
      `);
    } catch (error) {
      database.close();
      throw error;
    }
    this.database = database;
  }

  close(): void {
    this.database?.close();
    this.database = null;
  }

  createMember(name: string, email: string, issued: IssuedToken): StoredMember {
    return this.createMemberWithId(randomUUID(), name, email, issued);
  }

  createMemberWithId(
    memberId: string,
    name: string,
    email: string,
    issued: IssuedToken,
  ): StoredMember {
    const database = this.getDatabase();
    const createdAt = new Date().toISOString();
    database.prepare(`
      INSERT INTO members (
        member_id, name, email, token_lookup_id, token_digest, created_at
      ) VALUES (?, ?, ?, ?, ?, ?)
    `).run(
      memberId,
      required(name, "Member name"),
      required(email, "Member email"),
      issued.lookupId,
      issued.digest,
      createdAt,
    );
    return this.getMember(memberId);
  }

  listMembers(): StoredMember[] {
    const rows = this.getDatabase().prepare(`
      SELECT * FROM members ORDER BY created_at, member_id
    `).all() as unknown as MemberRow[];
    return rows.map(storedMember);
  }

  findMemberForAuth(lookupId: string): StoredMemberAuth | null {
    const row = this.getDatabase().prepare(`
      SELECT * FROM members WHERE token_lookup_id = ?
    `).get(lookupId) as unknown as MemberRow | undefined;
    if (!row) return null;
    return {
      ...storedMember(row),
      lookupId: row.token_lookup_id,
      digest: Buffer.from(row.token_digest),
    };
  }

  rotateMember(memberId: string, issued: IssuedToken): StoredMember {
    const rotatedAt = new Date().toISOString();
    const result = this.getDatabase().prepare(`
      UPDATE members
      SET token_lookup_id = ?, token_digest = ?, rotated_at = ?, revoked_at = NULL
      WHERE member_id = ?
    `).run(issued.lookupId, issued.digest, rotatedAt, memberId);
    if (result.changes !== 1) throw new Error("Member not found.");
    return this.getMember(memberId);
  }

  revokeMember(memberId: string): StoredMember {
    const result = this.getDatabase().prepare(`
      UPDATE members SET revoked_at = ? WHERE member_id = ?
    `).run(new Date().toISOString(), memberId);
    if (result.changes !== 1) throw new Error("Member not found.");
    return this.getMember(memberId);
  }

  reserveTurn(input: ReserveTurnInput): ReserveTurnResult {
    return this.inImmediateTransaction(() => {
      const existing = this.selectTurn(input);
      if (existing) {
        if (existing.requestDigest !== input.requestDigest) {
          throw new HttpProblem(409, "TURN_CONFLICT", "Turn ID already has different content.");
        }
        return {
          status: "existing",
          report: this.selectReport(input),
          turn: existing,
        };
      }

      const memberActive = this.countMemberActive(input.memberId);
      if (memberActive >= 1) {
        throw new HttpProblem(429, "MEMBER_ACTIVE_LIMIT", "Member already has active work.");
      }
      if (this.countGlobalActive() >= this.maxGlobalActiveTurns) {
        throw new HttpProblem(429, "GLOBAL_ACTIVE_LIMIT", "Global active-turn limit reached.");
      }

      const now = new Date().toISOString();
      this.getDatabase().prepare(`
        INSERT INTO reports (
          member_id, report_id, session_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (member_id, report_id) DO NOTHING
      `).run(input.memberId, input.reportId, randomUUID(), now, now);
      const payload = JSON.parse(input.payloadJson) as TurnInput;
      this.getDatabase().prepare(`
        INSERT INTO turns (
          member_id, report_id, turn_id, kind, request_digest,
          payload_json, state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
      `).run(
        input.memberId,
        input.reportId,
        input.turnId,
        payload.kind,
        input.requestDigest,
        input.payloadJson,
        now,
      );
      this.touchReport(input, now);
      const turn = this.selectTurn(input);
      if (!turn) throw new Error("Reserved turn was not persisted.");
      return { status: "created", report: this.selectReport(input), turn };
    });
  }

  getTurn(key: TurnKey): StoredTurn | null {
    return this.selectTurn(key);
  }

  claimNextTurn(maxRunning: number): ClaimedTurn | null {
    if (!Number.isSafeInteger(maxRunning) || maxRunning <= 0) {
      throw new TypeError("maxRunning must be a positive safe integer.");
    }

    return this.inImmediateTransaction(() => {
      const running = this.getDatabase().prepare(`
        SELECT COUNT(*) AS count FROM turns WHERE state = 'started'
      `).get() as { count: number };
      if (running.count >= maxRunning) return null;

      const row = this.getDatabase().prepare(`
        SELECT * FROM turns
        WHERE state = 'queued'
        ORDER BY created_at, rowid
        LIMIT 1
      `).get() as unknown as TurnRow | undefined;
      if (!row) return null;

      const runId = randomUUID();
      const startedAt = new Date().toISOString();
      const updated = this.getDatabase().prepare(`
        UPDATE turns
        SET state = 'started', run_id = ?, started_at = ?
        WHERE member_id = ? AND report_id = ? AND turn_id = ? AND state = 'queued'
      `).run(runId, startedAt, row.member_id, row.report_id, row.turn_id);
      if (updated.changes !== 1) throw new Error("Queued turn could not be claimed.");

      const key = {
        memberId: row.member_id,
        reportId: row.report_id,
        turnId: row.turn_id,
      };
      const turn = this.selectTurn(key);
      if (!turn) throw new Error("Claimed turn was not persisted.");
      return {
        ...turn,
        member: this.getMember(key.memberId),
        report: this.selectReport(key),
      };
    });
  }

  finishTurn(
    key: TurnKey,
    state: TerminalTurnState,
    result?: CaptainResult,
    error?: StoredTurnError,
  ): void {
    if (!TERMINAL_TURN_STATES.has(state)) {
      throw new HttpProblem(400, "INVALID_TURN_STATE", "Turn state must be terminal.");
    }

    this.inImmediateTransaction(() => {
      const finishedAt = new Date().toISOString();
      const updated = this.getDatabase().prepare(`
        UPDATE turns
        SET state = ?, result_json = ?, error_code = ?, error_message = ?, finished_at = ?
        WHERE member_id = ? AND report_id = ? AND turn_id = ? AND state = 'started'
      `).run(
        state,
        result ? JSON.stringify(result) : null,
        error?.code ?? null,
        error?.message ?? null,
        finishedAt,
        key.memberId,
        key.reportId,
        key.turnId,
      );
      if (updated.changes !== 1) {
        throw new HttpProblem(409, "TURN_NOT_STARTED", "Turn is not in the started state.");
      }
      this.touchReport(key, finishedAt);
    });
  }

  recoverStartedTurns(): number {
    const finishedAt = new Date().toISOString();
    const result = this.getDatabase().prepare(`
      UPDATE turns
      SET state = 'unknown_outcome',
          error_code = 'UNKNOWN_OUTCOME',
          error_message = 'Captain turn outcome is unknown after restart.',
          finished_at = ?
      WHERE state = 'started'
    `).run(finishedAt);
    return Number(result.changes);
  }

  private getMember(memberId: string): StoredMember {
    const row = this.getDatabase().prepare(`
      SELECT * FROM members WHERE member_id = ?
    `).get(memberId) as unknown as MemberRow | undefined;
    if (!row) throw new Error("Member not found.");
    return storedMember(row);
  }

  private selectReport(key: Pick<TurnKey, "memberId" | "reportId">): StoredReport {
    const row = this.getDatabase().prepare(`
      SELECT * FROM reports WHERE member_id = ? AND report_id = ?
    `).get(key.memberId, key.reportId) as unknown as ReportRow | undefined;
    if (!row) throw new Error("Report not found.");
    return storedReport(row);
  }

  private selectTurn(key: TurnKey): StoredTurn | null {
    const row = this.getDatabase().prepare(`
      SELECT * FROM turns
      WHERE member_id = ? AND report_id = ? AND turn_id = ?
    `).get(key.memberId, key.reportId, key.turnId) as unknown as TurnRow | undefined;
    return row ? storedTurn(row) : null;
  }

  private countMemberActive(memberId: string): number {
    const row = this.getDatabase().prepare(`
      SELECT COUNT(*) AS count FROM turns
      WHERE state IN ('queued', 'started') AND member_id = ?
    `).get(memberId) as { count: number };
    return row.count;
  }

  private countGlobalActive(): number {
    const row = this.getDatabase().prepare(`
      SELECT COUNT(*) AS count FROM turns WHERE state IN ('queued', 'started')
    `).get() as { count: number };
    return row.count;
  }

  private touchReport(key: Pick<TurnKey, "memberId" | "reportId">, updatedAt: string): void {
    this.getDatabase().prepare(`
      UPDATE reports SET updated_at = ? WHERE member_id = ? AND report_id = ?
    `).run(updatedAt, key.memberId, key.reportId);
  }

  private inImmediateTransaction<T>(operation: () => T): T {
    const database = this.getDatabase();
    database.exec("BEGIN IMMEDIATE");
    try {
      const result = operation();
      database.exec("COMMIT");
      return result;
    } catch (error) {
      try {
        database.exec("ROLLBACK");
      } catch {
        // Preserve the operation error if SQLite already ended the transaction.
      }
      throw error;
    }
  }

  private getDatabase(): DatabaseSync {
    if (!this.database) throw new Error("Captain remote store is not initialized.");
    return this.database;
  }
}
