/**
 * Shared types for the orchestrator API client.
 *
 * ApiErrorEnvelope mirrors the orchestrator's structured error contract:
 * { error, code, request_id?, details? }. Callers branch on `code`, never on
 * the `error` prose string.
 */

export interface ApiErrorEnvelope {
  error: string;
  code: string;
  request_id?: string | null;
  details?: Array<Record<string, unknown>> | null;
}

export type OrchestratorResult<T> =
  | { ok: true; data: T; duration_ms: number; requestId?: string }
  | {
      ok: false;
      envelope: ApiErrorEnvelope;
      httpStatus: number;
      duration_ms: number;
      requestId?: string;
    };
