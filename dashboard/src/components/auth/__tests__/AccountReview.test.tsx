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
  version: 'v1',
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
    // "Keep all" keeps 6: 2FA from before is kept only with a code, and the screen says so.
    expect(html).toContain('Keep all 6');
    expect(html).toContain('7 items');
    expect(html).toContain('Turns off when you finish, unless you enter a code from your app.');
    expect(html).toContain('one-time-code');
    expect(html).toContain('>Keep<');
    expect(html).toContain('Remove password');
    expect(html.match(/>Revoke</g)?.length).toBe(3); // two keys + one app
    expect(html).toContain('Later');
    // Plain copy: no jargon or internal labels, never "Forgot password", no contradiction.
    expect(html).not.toMatch(/forgot password|squat|pre-verification|account\.check|added since|before that/i);
    expect(html).toContain('These are set up on your account and all still work.');
  });

  it('says "Keep it" for one item and "Finish" when only 2FA is left', () => {
    const one = normalizeReview({
      pending: true,
      can_review: true,
      version: 'v1',
      items: { keys: [], connections: [], webhooks: [], identities: [], two_factor: false, password: true },
    });
    expect(renderToStaticMarkup(<AccountReview jwt="t" review={one} onToken={noop} onDone={noop} onLater={noop} />)).toContain('>Keep it<');
    const onlyTwoFactor = normalizeReview({
      pending: true,
      can_review: true,
      version: 'v1',
      items: { keys: [], connections: [], webhooks: [], identities: [], two_factor: true, password: false },
    });
    const html = renderToStaticMarkup(<AccountReview jwt="t" review={onlyTwoFactor} onToken={noop} onDone={noop} onLater={noop} />);
    expect(html).toContain('>Finish<');
    expect(html).toContain('Two-factor sign-in turns off unless you keep it with a code.');
    expect(html).not.toMatch(/Nothing left to check|Everything you did not keep is gone/);
  });

  it('blocks the dashboard in App until kept or put off for the session', () => {
    expect(APP).toContain('shouldShowReview(review, reviewDeferred)');
    expect(APP).toContain('rememberDeferred(currentUser.id)');
    expect(APP).toContain('clearDeferred()');
  });
});
