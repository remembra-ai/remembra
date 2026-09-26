import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { normalizeReview } from '../../../lib/accountReview';
import { AccountReview } from '../AccountReview';

const noop = () => {};
const APP = readFileSync(new URL('../../../App.tsx', import.meta.url), 'utf8');

const review = normalizeReview({
  pending: true,
  can_review: true,
  origin: 'google',
  items: {
    keys: [
      { id: 'k1', name: 'laptop', role: 'admin', project_ids: ['widget'], agent_id: 'codex', before_verification: true },
      { id: 'k2', name: 'late', role: 'viewer', project_ids: [], before_verification: false },
    ],
    connections: [{ id: 'g1', name: 'Claude', scopes: ['memory'], project_ids: [] }],
    webhooks: [{ id: 'wh1', url: 'https://hooks.example/secret-path', events: ['memory.created'] }],
    identities: [{ id: 'github', provider: 'github', name: 'GitHub', email: 'me@example.org' }],
    two_factor: true,
    password: true,
  },
});

describe('AccountReview screen', () => {
  it('lists every item with its own action and one "Keep all"', () => {
    const html = renderToStaticMarkup(
      <AccountReview jwt="t" email="me@gmail.com" review={review} onToken={noop} onDone={noop} onLater={noop} />,
    );
    expect(html).toContain('Is all of this yours?');
    expect(html).toContain('me@gmail.com');
    for (const text of ['laptop', 'admin · widget · agent codex', 'Claude', 'hooks.example', 'GitHub (me@example.org)', 'Password', 'Two-factor sign-in']) {
      expect(html).toContain(text);
    }
    expect(html).not.toContain('secret-path'); // webhook paths can carry secrets
    expect(html).toContain('Keep all 7');
    expect(html).toContain('Remove password');
    expect(html).toContain('added since'); // the key made after the email was confirmed
    expect(html.match(/>Revoke</g)?.length).toBe(3); // two keys + one app
    expect(html).toContain('Later');
    // Plain copy: no jargon, never "Forgot password".
    expect(html).not.toMatch(/forgot password|squat|pre-verification/i);
  });

  it('offers "Done" when nothing is left', () => {
    const empty = normalizeReview({
      pending: true,
      can_review: true,
      items: { keys: [], connections: [], webhooks: [], identities: [], two_factor: false, password: false },
    });
    const html = renderToStaticMarkup(<AccountReview jwt="t" review={empty} onToken={noop} onDone={noop} onLater={noop} />);
    expect(html).toContain('Nothing left to check.');
    expect(html).toContain('>Done<');
    expect(html).not.toContain('Later');
  });

  it('blocks the dashboard in App until kept or put off for the session', () => {
    expect(APP).toContain('shouldShowReview(review, reviewDeferred)');
    expect(APP).toContain('rememberDeferred(currentUser.id)');
    expect(APP).toContain('clearDeferred()');
  });
});
