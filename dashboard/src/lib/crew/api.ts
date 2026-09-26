// Crew mode REST client (spec §6). Typed wrappers over /api/v1 for what the
// dashboard reads and the human-only actions it offers. Errors are parsed
// from the server's `CrewError` body into CrewApiError (code, blockers,
// retry_after_s, step-up), so screens can say exactly why an action failed.
//
// `createCrewApi` takes its transport (fetch, base URL, credentials) so tests
// and the live check can drive it without a browser; `crewApi` is the
// dashboard instance, authenticated like every other dashboard request
// (dashboard JWT first, then an API key).

import { ApiError, api, getApiBaseUrl } from '../api';
import type {
  Blocker,
  CheckpointView,
  CollisionView,
  CrewDetail,
  CrewEventsPage,
  CrewListResponse,
  CrewResolveResponse,
  CrewSnapshot,
  DecisionView,
  InboxItemView,
  MessageKind,
  MessageView,
  ReportView,
  SessionView,
} from './types';

/** A failed crew request: the HTTP status plus the server's CrewError fields. */
export class CrewApiError extends ApiError {
  /** CrewError.error (e.g. `claim_cap`, `step_up_required`, `human_only`, `not_found`), or `http_<status>`. */
  readonly code: string;
  readonly blockers: Blocker[];
  readonly retryAfterS: number | null;
  /** Every other field of the error body (e.g. `current_version` on a 412). */
  readonly details: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    code: string,
    extra: { blockers?: Blocker[]; retryAfterS?: number | null; details?: Record<string, unknown> } = {},
  ) {
    super(message, status);
    this.name = 'CrewApiError';
    this.code = code;
    this.blockers = extra.blockers ?? [];
    this.retryAfterS = extra.retryAfterS ?? null;
    this.details = extra.details ?? {};
  }

  /** The action needs a fresh dashboard login (401 `step_up_required`, §5.9). */
  get stepUpRequired(): boolean {
    return this.status === 401 && this.code === 'step_up_required';
  }

  /** The credential is not a human principal (403 `human_only`, D27). */
  get humanOnly(): boolean {
    return this.status === 403 && this.code === 'human_only';
  }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface CrewCredentials {
  jwt?: string | null;
  apiKey?: string | null;
}

export interface CrewApiTransport {
  fetch: FetchLike;
  /** Origin of the API (no trailing slash); '' = same origin. */
  baseUrl: string;
  credentials: () => CrewCredentials;
  /** Idempotency keys for mutations (default: crypto.randomUUID). */
  newKey?: () => string;
}

export interface CrewRequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  /** Mutations always carry one; pass a stable key to retry the same action safely. */
  idempotencyKey?: string;
  ifMatch?: string | number;
  ifNoneMatch?: string | null;
  signal?: AbortSignal;
}

export interface CrewResponse<T> {
  status: number;
  /** null on 304 Not Modified. */
  data: T | null;
  etag: string | null;
}

function defaultKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Parse any error body the API returns (CrewError under `detail`, FastAPI validation lists, slowapi). */
export function parseCrewError(status: number, body: unknown): CrewApiError {
  let err: Record<string, unknown> = {};
  if (isRecord(body)) {
    const detail = body.detail;
    if (isRecord(detail)) err = detail;
    else if (Array.isArray(detail)) {
      const message = detail.map((d) => (isRecord(d) && typeof d.msg === 'string' ? d.msg : 'Validation error')).join(', ');
      return new CrewApiError(message || `Request failed (${status})`, status, 'validation_error');
    } else if (typeof detail === 'string') err = { message: detail };
    else err = body;
  }
  const code = typeof err.error === 'string' ? err.error : `http_${status}`;
  const message =
    typeof err.message === 'string' && err.message
      ? err.message
      : typeof err.error === 'string'
        ? err.error
        : status === 0
          ? 'Could not reach the Remembra server'
          : `Request failed (${status})`;
  const blockers = Array.isArray(err.blockers) ? (err.blockers as Blocker[]) : [];
  const retry = typeof err.retry_after_s === 'number' ? err.retry_after_s : null;
  const details: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(err)) {
    if (!['error', 'message', 'blockers', 'retry_after_s'].includes(k)) details[k] = v;
  }
  return new CrewApiError(message, status, code, { blockers, retryAfterS: retry, details });
}

const MUTATIONS = new Set(['POST', 'PATCH', 'PUT', 'DELETE']);

export function createCrewApi(transport: CrewApiTransport) {
  const newKey = transport.newKey ?? defaultKey;

  async function request<T>(path: string, options: CrewRequestOptions = {}): Promise<CrewResponse<T>> {
    const method = options.method ?? 'GET';
    const creds = transport.credentials();
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (creds.jwt) headers.Authorization = `Bearer ${creds.jwt}`;
    else if (creds.apiKey) headers['X-API-Key'] = creds.apiKey;
    else throw new CrewApiError('Not authenticated', 401, 'not_authenticated');
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (MUTATIONS.has(method)) headers['Idempotency-Key'] = options.idempotencyKey ?? newKey();
    if (options.ifMatch !== undefined) headers['If-Match'] = String(options.ifMatch);
    if (options.ifNoneMatch) headers['If-None-Match'] = options.ifNoneMatch;

    const search = new URLSearchParams();
    for (const [k, v] of Object.entries(options.query ?? {})) {
      if (v !== undefined && v !== null && v !== '') search.set(k, String(v));
    }
    const qs = search.toString();
    const url = `${transport.baseUrl}/api/v1${path}${qs ? `?${qs}` : ''}`;

    let response: Response;
    try {
      response = await transport.fetch(url, {
        method,
        headers,
        body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
        signal: options.signal,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') throw err;
      throw new CrewApiError('Could not reach the Remembra server', 0, 'unreachable');
    }
    const etag = response.headers.get('etag');
    if (response.status === 304) return { status: 304, data: null, etag };
    const text = await response.text().catch(() => '');
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = null;
      }
    }
    if (!response.ok) throw parseCrewError(response.status, body);
    if (body === null && text) {
      throw new CrewApiError(
        'Unexpected API response (expected JSON). Check that the dashboard points at the right API URL.',
        response.status,
        'bad_response',
      );
    }
    return { status: response.status, data: body as T, etag };
  }

  async function json<T>(path: string, options: CrewRequestOptions = {}): Promise<T> {
    const res = await request<T>(path, options);
    if (res.data === null) throw new CrewApiError('Empty response', res.status, 'bad_response');
    return res.data;
  }

  const enc = encodeURIComponent;
  const post = <T>(path: string, body?: unknown, extra: CrewRequestOptions = {}) => json<T>(path, { ...extra, method: 'POST', body });

  return {
    /** Low-level access for endpoints without a wrapper (same auth, errors and idempotency). */
    request,

    // -- crews -----------------------------------------------------------------
    listCrews: () => json<CrewListResponse>('/crews'),
    /** The crew of a project, or null when the project has no crew (or is not visible). Read-only. */
    async resolveCrew(projectId: string): Promise<CrewResolveResponse | null> {
      try {
        return await post<CrewResolveResponse>('/crews/resolve', { project_id: projectId });
      } catch (err) {
        if (err instanceof CrewApiError && err.status === 404) return null;
        throw err;
      }
    },
    getCrew: (crewId: string) => json<CrewDetail>(`/crews/${enc(crewId)}`),
    /** (H, step-up) Patch settings; `version` is the current settings_version (If-Match). */
    patchSettings: (crewId: string, settings: Record<string, unknown>, version: number) =>
      json<{ crew: CrewDetail['crew']; settings: Record<string, unknown>; settings_version: number; changed_keys: string[]; seq: number | null }>(
        `/crews/${enc(crewId)}`,
        { method: 'PATCH', body: { settings }, ifMatch: version },
      ),
    /** Snapshot; pass the last ETag to get `data: null` (304) when nothing changed. */
    snapshot: (crewId: string, etag?: string | null) =>
      request<CrewSnapshot>(`/crews/${enc(crewId)}/snapshot`, { ifNoneMatch: etag ?? null }),
    /** Polling fallback: events with seq > sinceSeq (≤200 per page); `data: null` on 304. */
    events: (crewId: string, sinceSeq: number, options: { limit?: number; etag?: string | null; signal?: AbortSignal } = {}) =>
      request<CrewEventsPage>(`/crews/${enc(crewId)}/events`, {
        query: { since_seq: sinceSeq, limit: options.limit },
        ifNoneMatch: options.etag ?? null,
        signal: options.signal,
      }),
    sessions: (crewId: string, state?: string) =>
      json<{ crew_id: string; sessions: SessionView[] }>(`/crews/${enc(crewId)}/sessions`, { query: { state } }),
    agentPage: (crewId: string, agentId: string) => json<Record<string, unknown>>(`/crews/${enc(crewId)}/agents/${enc(agentId)}`),
    agentTimeline: (crewId: string, agentId: string, window: { from?: string; to?: string } = {}) =>
      json<Record<string, unknown>>(`/crews/${enc(crewId)}/agents/${enc(agentId)}/timeline`, { query: window }),
    batons: (crewId: string, taskId?: string) => json<Record<string, unknown>>(`/crews/${enc(crewId)}/batons`, { query: { task_id: taskId } }),
    members: (crewId: string) => json<Record<string, unknown>>(`/crews/${enc(crewId)}/members`),

    // -- sessions (H) ------------------------------------------------------------
    pauseSession: (sessionId: string, reason: string) => post<Record<string, unknown>>(`/sessions/${enc(sessionId)}/pause`, { reason }),
    resumeSession: (sessionId: string, reason: string) => post<Record<string, unknown>>(`/sessions/${enc(sessionId)}/resume`, { reason }),
    requestCheckpoint: (sessionId: string, reason: string) =>
      post<Record<string, unknown>>(`/sessions/${enc(sessionId)}/request-checkpoint`, { reason }),
    releaseAllClaims: (sessionId: string, reason: string) =>
      post<Record<string, unknown>>(`/sessions/${enc(sessionId)}/release-all`, { reason }),

    // -- zones -----------------------------------------------------------------------
    /** (H) Freeze a zone: a human exclusive claim ("Mani is editing POS himself"). */
    freezeZone: (zoneId: string, reason: string, until?: string | null) =>
      post<Record<string, unknown>>(`/zones/${enc(zoneId)}/freeze`, until ? { reason, until } : { reason }),
    /** (H, step-up) */
    unfreezeZone: (zoneId: string, reason: string) => post<Record<string, unknown>>(`/zones/${enc(zoneId)}/unfreeze`, { reason }),
    zoneChanges: (crewId: string, state = 'pending') =>
      json<Record<string, unknown>>(`/crews/${enc(crewId)}/zone-changes`, { query: { state } }),
    /** (H, step-up) */
    approveZoneChange: (changeId: string) => post<Record<string, unknown>>(`/zone-changes/${enc(changeId)}/approve`),
    /** (H) */
    rejectZoneChange: (changeId: string) => post<Record<string, unknown>>(`/zone-changes/${enc(changeId)}/reject`),
    /** Which zones (and holders) cover these repo-relative paths. */
    match: (crewId: string, paths: string[], extra: { command_tokens?: string[]; mcp_tool?: string } = {}) =>
      post<Record<string, unknown>>(`/crews/${enc(crewId)}/match`, { paths, ...extra }),

    // -- claims, bypass codes, collisions ---------------------------------------------
    /** (H, step-up) revoke | transfer (to a session) | hold. */
    overrideClaim: (claimId: string, body: { action: 'revoke' | 'transfer' | 'hold'; to?: string | null; reason: string }) =>
      post<Record<string, unknown>>(`/claims/${enc(claimId)}/override`, body),
    /** (H, step-up) A single-use code (≤15 min) that lets one session pass the gate (D34). */
    issueBypassCode: (crewId: string, body: { session_id: string; scope: string; minutes: number }) =>
      post<{ code_id: string; code: string; session_id: string; scope: string; expires_at: string }>(`/crews/${enc(crewId)}/bypass-codes`, body),
    collisions: (crewId: string, state?: string) =>
      json<{ collisions: CollisionView[] }>(`/crews/${enc(crewId)}/collisions`, { query: { state } }),
    ackCollision: (collisionId: string) => post<Record<string, unknown>>(`/collisions/${enc(collisionId)}/ack`),
    resolveCollision: (collisionId: string, resolution?: string) =>
      post<Record<string, unknown>>(`/collisions/${enc(collisionId)}/resolve`, resolution ? { resolution } : {}),
    /** (H) */
    dismissCollision: (collisionId: string, reason?: string) =>
      post<Record<string, unknown>>(`/collisions/${enc(collisionId)}/dismiss`, reason ? { reason } : {}),

    // -- tasks and reports --------------------------------------------------------------
    tasks: (crewId: string) => json<Record<string, unknown>>(`/crews/${enc(crewId)}/tasks`),
    task: (taskId: string) => json<Record<string, unknown>>(`/tasks/${enc(taskId)}`),
    createTask: (
      crewId: string,
      body: { title: string; zone_ids: string[]; acceptance: unknown[]; depends_on: string[]; body?: string; phase?: string; priority?: number; reviewer?: string },
    ) => post<Record<string, unknown>>(`/crews/${enc(crewId)}/tasks`, body),
    /** Criteria after the lock are human-only; `done` is refused (409 report_required). */
    patchTask: (taskId: string, patch: Record<string, unknown>, version: number) =>
      json<Record<string, unknown>>(`/tasks/${enc(taskId)}`, { method: 'PATCH', body: patch, ifMatch: version }),
    /** (H) Hand a task (a stalled baton included) to a session; records an offer. */
    assignTask: (taskId: string, to: string) => post<Record<string, unknown>>(`/tasks/${enc(taskId)}/assign`, { to }),
    /** (H) */
    reviewTask: (taskId: string, decision: 'approve' | 'reject', note?: string) =>
      post<Record<string, unknown>>(`/tasks/${enc(taskId)}/review`, note ? { decision, note } : { decision }),
    /** (H, step-up) Waive one criterion or "all" with a reason (D17). */
    waiveTask: (taskId: string, criterionId: string, reason: string) =>
      post<Record<string, unknown>>(`/tasks/${enc(taskId)}/waive`, { criterion_id: criterionId, reason }),
    reopenTask: (taskId: string) => post<Record<string, unknown>>(`/tasks/${enc(taskId)}/reopen`),
    taskReports: (taskId: string) => json<{ reports?: ReportView[]; [key: string]: unknown }>(`/tasks/${enc(taskId)}/reports`),
    checkpoints: (crewId: string, filter: { session_id?: string; task_id?: string } = {}) =>
      json<{ checkpoints?: CheckpointView[]; [key: string]: unknown }>(`/crews/${enc(crewId)}/checkpoints`, { query: filter }),

    // -- channel and decisions -------------------------------------------------------------
    messages: (crewId: string, filter: { thread?: string; since_seq?: number; before?: number; limit?: number } = {}) =>
      json<{ items: MessageView[]; last_seq: number | null }>(`/crews/${enc(crewId)}/messages`, { query: filter }),
    /** Post as the signed-in human. `clientMsgId` makes a retry idempotent. */
    postMessage: (
      crewId: string,
      body: { kind?: MessageKind; body: string; thread_root_id?: string; reply_to_id?: string; refs?: string[]; clientMsgId?: string },
    ) => {
      const { clientMsgId, kind, ...rest } = body;
      const payload: Record<string, unknown> = { kind: kind ?? 'chat', client_msg_id: clientMsgId ?? newKey(), ...rest };
      for (const key of Object.keys(payload)) if (payload[key] === undefined) delete payload[key];
      return post<{ message?: MessageView; seq?: number; [key: string]: unknown }>(`/crews/${enc(crewId)}/messages`, payload);
    },
    /** (H) */
    pinMessage: (messageId: string, pinned = true) => post<Record<string, unknown>>(`/messages/${enc(messageId)}/pin`, { pinned }),
    /** (H) */
    redactMessage: (messageId: string) => post<Record<string, unknown>>(`/messages/${enc(messageId)}/redact`),
    decisions: (crewId: string) => json<{ items?: DecisionView[]; [key: string]: unknown }>(`/crews/${enc(crewId)}/decisions`),
    /** A human-created decision is in force immediately (§5.7). */
    createDecision: (crewId: string, body: { title: string; decision: string; rationale?: string; task_id?: string; zone_id?: string }) =>
      post<Record<string, unknown>>(`/crews/${enc(crewId)}/decisions`, body),
    /** (H) */
    confirmDecision: (decisionId: string) => post<Record<string, unknown>>(`/decisions/${enc(decisionId)}/confirm`),
    /** (H) */
    rejectDecision: (decisionId: string) => post<Record<string, unknown>>(`/decisions/${enc(decisionId)}/reject`),

    // -- inboxes -----------------------------------------------------------------------------
    inbox: (crewId: string, audience: 'project' | 'crew' | 'me' = 'project', limit?: number) =>
      json<{ audience: string; recipient: string | null; items: InboxItemView[]; counts: Record<string, number> }>(
        `/crews/${enc(crewId)}/inbox`,
        { query: { audience, limit } },
      ),
    inboxOverview: (limit?: number) => json<Record<string, unknown>>('/crews/inbox/overview', { query: { limit } }),
    inboxItem: (itemId: string, action: 'seen' | 'claim' | 'resolve' | 'dismiss') =>
      post<Record<string, unknown>>(`/inbox/items/${enc(itemId)}/${action}`),
    markRead: (crewId: string, stream: string, seq: number) => post<Record<string, unknown>>(`/crews/${enc(crewId)}/read`, { stream, seq }),

    // -- notifications ----------------------------------------------------------------------
    notifications: (filter: { crew_id?: string; limit?: number } = {}) => json<Record<string, unknown>>('/notifications', { query: filter }),
    markNotificationsRead: (body: { all: true } | { crew_id: string; upto_seq?: number }) =>
      json<Record<string, unknown>>('/notifications', { method: 'PATCH', body }),
    notificationRules: () => json<Record<string, unknown>>('/notifications/rules'),
    /** (H) Real-time email or signed https webhook target (§9.11). */
    addNotifyTarget: (kind: 'email' | 'webhook', target: string) =>
      post<Record<string, unknown>>('/notifications/targets', { kind, target }),
  };
}

export type CrewApi = ReturnType<typeof createCrewApi>;

/** The dashboard's crew client: same origin/API URL and credentials as `api`. */
export const crewApi: CrewApi = createCrewApi({
  fetch: (input, init) => fetch(input, init),
  baseUrl: getApiBaseUrl(),
  credentials: () => ({ jwt: api.getJwtToken(), apiKey: api.getApiKey() }),
});
