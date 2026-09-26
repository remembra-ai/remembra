import { describe, expect, it } from 'vitest';
import { CrewApiError, createCrewApi, parseCrewError, type CrewApi } from '../api';

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function recorder(respond: (call: Call) => Response = () => jsonResponse(200, { ok: true })) {
  const calls: Call[] = [];
  let n = 0;
  const api = createCrewApi({
    baseUrl: 'https://api.example',
    credentials: () => ({ jwt: 'jwt-token', apiKey: 'ignored-when-jwt' }),
    newKey: () => `key-${++n}`,
    fetch: async (url, init) => {
      const call: Call = {
        url,
        method: init?.method ?? 'GET',
        headers: (init?.headers ?? {}) as Record<string, string>,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      calls.push(call);
      return respond(call);
    },
  });
  return { api, calls };
}

// ---------------------------------------------------------------------------
// Contract: every call the client makes is a route of docs/crew/openapi.json
// (WP-0a), with a body its request schema accepts.
// ---------------------------------------------------------------------------

interface OpenApiSchema {
  properties?: Record<string, unknown>;
  required?: string[];
  additionalProperties?: boolean;
}
interface OpenApi {
  paths: Record<string, Record<string, { requestBody?: { content: { 'application/json': { schema: { $ref?: string } } } } }>>;
  components: { schemas: Record<string, OpenApiSchema> };
}

const openapiFiles = import.meta.glob('../../../../../docs/crew/openapi.json', { eager: true, import: 'default' }) as Record<string, OpenApi>;
const OPENAPI = Object.values(openapiFiles)[0];

function findOperation(method: string, url: string) {
  const path = new URL(url).pathname;
  for (const [template, ops] of Object.entries(OPENAPI.paths)) {
    const re = new RegExp(`^${template.replace(/\{[^}]+\}/g, '[^/]+')}$`);
    if (re.test(path) && ops[method.toLowerCase()]) return { template, op: ops[method.toLowerCase()] };
  }
  return null;
}

type Invocation = [string, (api: CrewApi) => Promise<unknown>];

const INVOCATIONS: Invocation[] = [
  ['listCrews', (a) => a.listCrews()],
  ['resolveCrew', (a) => a.resolveCrew('yaadbooks')],
  ['getCrew', (a) => a.getCrew('crw_1')],
  ['patchSettings', (a) => a.patchSettings('crw_1', { enforcement: 'observe' }, 3)],
  ['snapshot', (a) => a.snapshot('crw_1')],
  ['events', (a) => a.events('crw_1', 5)],
  ['sessions', (a) => a.sessions('crw_1', 'live')],
  ['agentPage', (a) => a.agentPage('crw_1', 'claude-code')],
  ['agentTimeline', (a) => a.agentTimeline('crw_1', 'claude-code')],
  ['batons', (a) => a.batons('crw_1', 'tsk_1')],
  ['members', (a) => a.members('crw_1')],
  ['pauseSession', (a) => a.pauseSession('cs_1', 'r')],
  ['resumeSession', (a) => a.resumeSession('cs_1', 'r')],
  ['requestCheckpoint', (a) => a.requestCheckpoint('cs_1', 'r')],
  ['releaseAllClaims', (a) => a.releaseAllClaims('cs_1', 'r')],
  ['freezeZone', (a) => a.freezeZone('zn_1', 'r', '2026-09-26T00:00:00Z')],
  ['unfreezeZone', (a) => a.unfreezeZone('zn_1', 'r')],
  ['zoneChanges', (a) => a.zoneChanges('crw_1')],
  ['approveZoneChange', (a) => a.approveZoneChange('zch_1')],
  ['rejectZoneChange', (a) => a.rejectZoneChange('zch_1')],
  ['match', (a) => a.match('crw_1', ['src/a.ts'])],
  ['overrideClaim', (a) => a.overrideClaim('clm_1', { action: 'transfer', to: 'cs_2', reason: 'r' })],
  ['issueBypassCode', (a) => a.issueBypassCode('crw_1', { session_id: 'cs_1', scope: 'push', minutes: 10 })],
  ['collisions', (a) => a.collisions('crw_1', 'open')],
  ['ackCollision', (a) => a.ackCollision('col_1')],
  ['resolveCollision', (a) => a.resolveCollision('col_1', 'fixed')],
  ['dismissCollision', (a) => a.dismissCollision('col_1', 'noise')],
  ['tasks', (a) => a.tasks('crw_1')],
  ['task', (a) => a.task('tsk_1')],
  ['createTask', (a) => a.createTask('crw_1', { title: 'T', zone_ids: [], acceptance: [], depends_on: [] })],
  ['patchTask', (a) => a.patchTask('tsk_1', { title: 'T2' }, 2)],
  ['assignTask', (a) => a.assignTask('tsk_1', 'cs_2')],
  ['reviewTask', (a) => a.reviewTask('tsk_1', 'approve', 'ok')],
  ['waiveTask', (a) => a.waiveTask('tsk_1', 'all', 'r')],
  ['reopenTask', (a) => a.reopenTask('tsk_1')],
  ['taskReports', (a) => a.taskReports('tsk_1')],
  ['checkpoints', (a) => a.checkpoints('crw_1', { session_id: 'cs_1' })],
  ['messages', (a) => a.messages('crw_1', { thread: 'msg_1', limit: 50 })],
  ['postMessage', (a) => a.postMessage('crw_1', { body: 'hi', clientMsgId: 'c1' })],
  ['pinMessage', (a) => a.pinMessage('msg_1')],
  ['redactMessage', (a) => a.redactMessage('msg_1')],
  ['decisions', (a) => a.decisions('crw_1')],
  ['createDecision', (a) => a.createDecision('crw_1', { title: 'GCT', decision: 'half-up' })],
  ['confirmDecision', (a) => a.confirmDecision('dec_1')],
  ['rejectDecision', (a) => a.rejectDecision('dec_1')],
  ['inbox', (a) => a.inbox('crw_1', 'crew')],
  ['inboxOverview', (a) => a.inboxOverview()],
  ['inboxItem', (a) => a.inboxItem('inb_1', 'resolve')],
  ['markRead', (a) => a.markRead('crw_1', 'inapp', 4)],
  ['notifications', (a) => a.notifications({ limit: 5 })],
  ['markNotificationsRead', (a) => a.markNotificationsRead({ all: true })],
  ['notificationRules', (a) => a.notificationRules()],
  ['addNotifyTarget', (a) => a.addNotifyTarget('webhook', 'https://hooks.example/crew')],
  ['confirmNotifyTarget', (a) => a.confirmNotifyTarget('ntt_1', 'ABCD1234')],
];

describe('crew API client contract (docs/crew/openapi.json)', () => {
  it('covers every client method', () => {
    const { api } = recorder();
    const methods = Object.keys(api).filter((k) => k !== 'request').sort();
    expect(INVOCATIONS.map(([name]) => name).sort()).toEqual(methods);
  });

  it.each(INVOCATIONS)('%s calls a contract route with a valid body', async (_name, invoke) => {
    const { api, calls } = recorder();
    await invoke(api);
    expect(calls).toHaveLength(1);
    const call = calls[0];
    expect(call.url.startsWith('https://api.example/api/v1/')).toBe(true);
    const found = findOperation(call.method, call.url);
    expect(found, `${call.method} ${call.url}`).not.toBeNull();
    const ref = found!.op.requestBody?.content['application/json'].schema.$ref;
    if (ref && call.body !== undefined) {
      const schema = OPENAPI.components.schemas[ref.split('/').at(-1)!];
      const keys = Object.keys(call.body as object);
      if (schema.additionalProperties === false && schema.properties) {
        for (const key of keys) expect(Object.keys(schema.properties), `${ref}: unknown key ${key}`).toContain(key);
      }
      for (const key of schema.required ?? []) expect(keys, `${ref}: missing ${key}`).toContain(key);
    }
    expect(call.headers.Authorization).toBe('Bearer jwt-token');
    expect(call.headers['X-API-Key']).toBeUndefined();
    if (call.method === 'GET') expect(call.headers['Idempotency-Key']).toBeUndefined();
    else expect(call.headers['Idempotency-Key']).toMatch(/^key-\d+$/);
  });
});

describe('crew API client behaviour', () => {
  it('builds query strings, drops empty values and escapes path ids', async () => {
    const { api, calls } = recorder();
    await api.events('crw_a/b', 5, { limit: 50 });
    await api.sessions('crw_1');
    expect(calls[0].url).toBe('https://api.example/api/v1/crews/crw_a%2Fb/events?since_seq=5&limit=50');
    expect(calls[1].url).toBe('https://api.example/api/v1/crews/crw_1/sessions');
  });

  it('sends If-Match and If-None-Match, and returns data null on 304 with the ETag', async () => {
    const { api, calls } = recorder((call) =>
      call.url.includes('/snapshot') ? new Response(null, { status: 304, headers: { ETag: '"41"' } }) : jsonResponse(200, { settings_version: 4 }),
    );
    const snap = await api.snapshot('crw_1', '"41"');
    expect(snap).toEqual({ status: 304, data: null, etag: '"41"' });
    expect(calls[0].headers['If-None-Match']).toBe('"41"');
    await api.patchSettings('crw_1', { enforcement: 'observe' }, 3);
    expect(calls[1].headers['If-Match']).toBe('3');
    expect(calls[1].body).toEqual({ settings: { enforcement: 'observe' } });
  });

  it('falls back to the API key when there is no dashboard login', async () => {
    const calls: Record<string, string>[] = [];
    const api = createCrewApi({
      baseUrl: '',
      credentials: () => ({ jwt: null, apiKey: 'rem_key' }),
      fetch: async (url, init) => {
        expect(url).toBe('/api/v1/crews');
        calls.push(init?.headers as Record<string, string>);
        return jsonResponse(200, { crews: [], count: 0 });
      },
    });
    expect(await api.listCrews()).toEqual({ crews: [], count: 0 });
    expect(calls[0]['X-API-Key']).toBe('rem_key');
    expect(calls[0].Authorization).toBeUndefined();
  });

  it('refuses to call without credentials', async () => {
    const api = createCrewApi({ baseUrl: '', credentials: () => ({}), fetch: async () => jsonResponse(200, {}) });
    await expect(api.listCrews()).rejects.toMatchObject({ status: 401, code: 'not_authenticated' });
  });

  it('parses CrewError bodies: code, message, blockers, retry_after_s and extras', async () => {
    const blockers = [{ claim_id: 'clm_1', zone_id: 'zn_pos', holder_session_id: 'cs_b', holder_callsign: 'codex-1', task_id: null, reason: 'held' }];
    const { api } = recorder(() =>
      jsonResponse(409, { detail: { error: 'claim_cap', message: 'Too many claims.', blockers, retry_after_s: 3, current_version: 7 } }),
    );
    const err = await api.freezeZone('zn_1', 'r').catch((e) => e);
    expect(err).toBeInstanceOf(CrewApiError);
    expect(err).toMatchObject({ status: 409, code: 'claim_cap', message: 'Too many claims.', retryAfterS: 3 });
    expect(err.blockers).toEqual(blockers);
    expect(err.details).toEqual({ current_version: 7 });
  });

  it('recognises step-up and human-only refusals', () => {
    const stepUp = parseCrewError(401, { detail: { error: 'step_up_required', message: 'Sign in again.', max_age_s: 900 } });
    expect(stepUp.stepUpRequired).toBe(true);
    expect(stepUp.details).toEqual({ max_age_s: 900 });
    const human = parseCrewError(403, { detail: { error: 'human_only', message: 'Needs a login.' } });
    expect(human.humanOnly).toBe(true);
    expect(human.stepUpRequired).toBe(false);
  });

  it('parses validation lists, plain string details, bare bodies and empty bodies', () => {
    expect(parseCrewError(422, { detail: [{ msg: 'field required' }, { msg: 'too long' }] })).toMatchObject({
      code: 'validation_error',
      message: 'field required, too long',
    });
    expect(parseCrewError(400, { detail: 'Bad thing' })).toMatchObject({ code: 'http_400', message: 'Bad thing' });
    expect(parseCrewError(429, { error: 'Rate limit exceeded: 60 per 1 minute' })).toMatchObject({
      code: 'Rate limit exceeded: 60 per 1 minute',
      message: 'Rate limit exceeded: 60 per 1 minute',
    });
    expect(parseCrewError(502, null)).toMatchObject({ code: 'http_502', message: 'Request failed (502)' });
  });

  it('maps network failures to status 0 and keeps aborts as aborts', async () => {
    const down = createCrewApi({
      baseUrl: '',
      credentials: () => ({ jwt: 't' }),
      fetch: async () => {
        throw new TypeError('Failed to fetch');
      },
    });
    await expect(down.listCrews()).rejects.toMatchObject({ status: 0, code: 'unreachable' });
    const aborted = createCrewApi({
      baseUrl: '',
      credentials: () => ({ jwt: 't' }),
      fetch: async () => {
        throw new DOMException('aborted', 'AbortError');
      },
    });
    await expect(aborted.listCrews()).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('rejects non-JSON success bodies (a wrong API URL serving the SPA)', async () => {
    const { api } = recorder(() => new Response('<!doctype html><html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } }));
    await expect(api.listCrews()).rejects.toMatchObject({ code: 'bad_response' });
  });

  it('resolveCrew is read-only and answers null for a project without a crew', async () => {
    const { api, calls } = recorder(() => jsonResponse(404, { detail: { error: 'not_found', message: 'Not found.' } }));
    expect(await api.resolveCrew('nope')).toBeNull();
    expect(calls[0]).toMatchObject({ method: 'POST', body: { project_id: 'nope' } });
  });

  it('postMessage fills kind and client_msg_id and keeps a caller key for retries', async () => {
    const { api, calls } = recorder(() => jsonResponse(201, { seq: 9 }));
    await api.postMessage('crw_1', { body: 'Hold POS until T-14 lands', thread_root_id: 'msg_1' });
    await api.postMessage('crw_1', { body: 'again', kind: 'note', clientMsgId: 'stable-1' });
    expect(calls[0].body).toEqual({ kind: 'chat', client_msg_id: 'key-1', body: 'Hold POS until T-14 lands', thread_root_id: 'msg_1' });
    expect(calls[1].body).toEqual({ kind: 'note', client_msg_id: 'stable-1', body: 'again' });
  });

  it('uses a caller-supplied idempotency key through request()', async () => {
    const { api, calls } = recorder();
    await api.request('/crews/crw_1/read', { method: 'POST', body: { stream: 'feed', seq: 1 }, idempotencyKey: 'fixed' });
    expect(calls[0].headers['Idempotency-Key']).toBe('fixed');
  });
});
