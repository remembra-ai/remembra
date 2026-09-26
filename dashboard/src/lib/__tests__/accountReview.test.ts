import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  NO_REVIEW,
  accessLine,
  clearDeferred,
  completeReview,
  isDeferred,
  itemCount,
  normalizeReview,
  rememberDeferred,
  revokeReviewItem,
  shouldShowReview,
  webhookHost,
} from '../accountReview';

const SERVER = {
  pending: true,
  can_review: true,
  origin: 'google',
  verified_at: '2026-09-26T12:00:00+00:00',
  message: null,
  items: {
    keys: [
      {
        id: 'k1',
        name: 'laptop',
        created_at: '2026-01-02T00:00:00+00:00',
        last_used_at: null,
        agent_id: 'codex',
        role: 'admin',
        scopes: [],
        project_ids: ['widget'],
        before_verification: true,
      },
      { id: 'k2', name: null, role: 'viewer', project_ids: [], before_verification: false },
      { name: 'no id is dropped' },
    ],
    connections: [{ id: 'g1', name: 'Claude', scopes: ['memory'], project_ids: [], agent_id: 'claude' }],
    webhooks: [{ id: 'wh1', url: 'https://hooks.example/abc?secret=1', events: ['memory.created'] }],
    identities: [{ id: 'github', provider: 'github', name: 'GitHub', email: 'me@example.org', created_at: null }],
    two_factor: true,
    password: true,
  },
};

function memoryStore() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('account review', () => {
  it('normalizes the server answer and counts every item', () => {
    const review = normalizeReview(SERVER);
    expect(review.pending && review.canReview).toBe(true);
    expect(review.items?.keys.map((k) => k.id)).toEqual(['k1', 'k2']);
    expect(review.items?.keys[1]).toMatchObject({ name: null, role: 'viewer', before_verification: false });
    expect(itemCount(review.items)).toBe(2 + 1 + 1 + 1 + 1 + 1);
  });

  it('shows nothing for no review, a malformed answer, or a session that cannot act', () => {
    expect(normalizeReview(null)).toEqual(NO_REVIEW);
    expect(normalizeReview({ pending: 'yes' })).toEqual(NO_REVIEW);
    const untrusted = normalizeReview({ pending: true, can_review: false, message: 'Sign in with Google…' });
    expect(untrusted.pending).toBe(true);
    expect(shouldShowReview(untrusted, false)).toBe(false);
    expect(shouldShowReview(normalizeReview(SERVER), false)).toBe(true);
    expect(shouldShowReview(normalizeReview(SERVER), true)).toBe(false);
  });

  it('"Later" lasts for this browser session and this account only', () => {
    const store = memoryStore();
    expect(isDeferred('u1', store)).toBe(false);
    rememberDeferred('u1', store);
    expect(isDeferred('u1', store)).toBe(true);
    expect(isDeferred('u2', store)).toBe(false);
    clearDeferred(store);
    expect(isDeferred('u1', store)).toBe(false);
    const broken = { getItem: () => { throw new Error('blocked'); } };
    expect(isDeferred('u1', broken)).toBe(false);
  });

  it('formats access and hides webhook paths', () => {
    expect(accessLine('admin', ['widget'], 'codex')).toBe('admin · widget · agent codex');
    expect(accessLine('editor', [], null)).toBe('editor · all projects');
    expect(webhookHost('https://hooks.example/abc?secret=1')).toBe('hooks.example');
    expect(webhookHost('not a url')).toBe('not a url');
  });

  it('revokes with the session and passes a fresh token on', async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({ revoked: 'Password', access_token: 'new-jwt', review: SERVER }), { status: 200 });
      }),
    );
    const result = await revokeReviewItem('old-jwt', 'password');
    expect(result.accessToken).toBe('new-jwt');
    expect(result.review.canReview).toBe(true);
    expect(calls[0].url).toMatch(/\/auth\/review\/revoke$/);
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ kind: 'password' });
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe('Bearer old-jwt');
  });

  it('surfaces the server refusal, and treats an already finished check as done', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'That item is not in this account check.' }), { status: 404 })),
    );
    await expect(revokeReviewItem('jwt', 'key', 'k9')).rejects.toThrow('That item is not in this account check.');
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 409 })));
    await expect(completeReview('jwt')).resolves.toBeUndefined();
  });
});
