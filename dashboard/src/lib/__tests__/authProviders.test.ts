import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  appPath,
  exchangeLoginCode,
  normalizeAuthConfig,
  ReauthRequiredError,
  requestProviderLink,
  oauthErrorMessage,
  orderProviders,
  parseOAuthFragment,
  passwordChecks,
  providerStartUrl,
} from '../authProviders';

describe('auth provider config', () => {
  it('keeps only well-formed providers whose start path matches their id', () => {
    const config = normalizeAuthConfig({
      providers: [
        { id: 'github', name: 'GitHub', start_path: '/api/v1/auth/oauth/github/start' },
        { id: 'google', name: 'Google', start_path: 'https://evil.example/start' },
        { id: 'Bad Id', name: 'x', start_path: '/api/v1/auth/oauth/Bad Id/start' },
        null,
      ],
      turnstile_site_key: '  0x4AAA  ',
    });
    expect(config.providers.map((p) => p.id)).toEqual(['github']);
    expect(config.turnstile_site_key).toBe('0x4AAA');
  });

  it('treats junk as no providers and no Turnstile', () => {
    expect(normalizeAuthConfig(null)).toEqual({ providers: [], turnstile_site_key: null });
    expect(normalizeAuthConfig({ providers: 'x', turnstile_site_key: '' })).toEqual({ providers: [], turnstile_site_key: null });
  });

  it('puts Google first and builds start URLs with the page', () => {
    const github = { id: 'github', name: 'GitHub', start_path: '/api/v1/auth/oauth/github/start' };
    const google = { id: 'google', name: 'Google', start_path: '/api/v1/auth/oauth/google/start' };
    expect(orderProviders([github, google]).map((p) => p.id)).toEqual(['google', 'github']);
    expect(providerStartUrl(github, 'signup')).toBe('/api/v1/auth/oauth/github/start?from=signup');
  });
});

describe('oauth callback fragment', () => {
  it('parses a login code', () => {
    expect(parseOAuthFragment('#code=abc&provider=github')).toEqual({
      code: 'abc',
      error: null,
      provider: 'github',
      from: 'login',
      linked: false,
    });
  });

  it('parses an error and the page it came from', () => {
    const f = parseOAuthFragment('#error=email_unverified&provider=github&from=signup');
    expect(f.error).toBe('email_unverified');
    expect(f.from).toBe('signup');
    expect(parseOAuthFragment('#from=elsewhere').from).toBe('login');
  });

  it('parses a provider connected from Settings', () => {
    const f = parseOAuthFragment('#linked=1&provider=github&from=settings');
    expect(f).toEqual({ code: null, error: null, provider: 'github', from: 'settings', linked: true });
    expect(parseOAuthFragment('#linked=yes').linked).toBe(false);
  });

  it('maps codes to fixed copy and never echoes unknown input', () => {
    expect(oauthErrorMessage('email_unverified', 'github')).toContain('verified primary email');
    // Forgot password is never offered as a way to verify an email.
    for (const code of ['account_exists_unverified', 'account_exists_link_required', 'email_in_use', 'email_unverified']) {
      for (const provider of ['google', 'github']) {
        const copy = oauthErrorMessage(code, provider);
        expect(copy).not.toMatch(/forgot password|verification link/i);
        expect(copy.length).toBeLessThan(140);
      }
    }
    expect(oauthErrorMessage('account_exists_link_required', 'github')).toBe(
      'You already have an account with this email. Sign in with Google or your password first. Then add GitHub in Settings.',
    );
    expect(oauthErrorMessage('email_in_use', 'google')).toContain('already used by another Remembra account');
    expect(oauthErrorMessage('identity_in_use', 'github')).toContain('different Remembra account');
    expect(oauthErrorMessage('access_denied', 'google')).toBe('Sign-in with Google was cancelled.');
    const unknown = oauthErrorMessage('<script>alert(1)</script>', '<img>');
    expect(unknown).not.toContain('<');
  });
});

describe('password rules', () => {
  it('mirror the server signup rules', () => {
    expect(Object.values(passwordChecks('Str0ng!Passw0rd')).every(Boolean)).toBe(true);
    expect(passwordChecks('weakpassword')).toEqual({ length: true, upper: false, lower: true, number: false, special: false });
    expect(passwordChecks('Aa1_').special).toBe(true);
  });
});

describe('app paths', () => {
  it('strip the base URL and trailing slashes', () => {
    expect(appPath('/oauth/callback', '/')).toBe('/oauth/callback');
    expect(appPath('/oauth/callback/', '/')).toBe('/oauth/callback');
    expect(appPath('/', '/')).toBe('/');
    expect(appPath('/preview/verify-email/', '/preview/')).toBe('/verify-email');
    expect(appPath('/preview/', '/preview/')).toBe('/');
  });
});

describe('login code exchange and provider linking', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('sends the browser-binding cookie with the exchange', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await exchangeLoginCode('the-code', '123456');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/auth/oauth/exchange');
    expect(init.credentials).toBe('include');
    expect(JSON.parse(init.body)).toEqual({ code: 'the-code', totp_code: '123456' });
  });

  it('stores the ticket-binding cookie when it asks for a link ticket', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ start_path: '/api/v1/auth/oauth/google/start?link=t' })));
    vi.stubGlobal('fetch', fetchMock);
    await requestProviderLink('jwt', 'google');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/auth/oauth/google/link');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
    expect(init.headers).toEqual({ Authorization: 'Bearer jwt' });
  });

  it('only follows a start path on the API for the same provider', async () => {
    const reply = (body: unknown, status = 200) => vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }));
    vi.stubGlobal('fetch', reply({ start_path: '/api/v1/auth/oauth/github/start?link=abc' }));
    await expect(requestProviderLink('jwt', 'github')).resolves.toBe('/api/v1/auth/oauth/github/start?link=abc');
    vi.stubGlobal('fetch', reply({ start_path: 'https://evil.example/?link=abc' }));
    await expect(requestProviderLink('jwt', 'github')).rejects.toThrow('Could not start');
    vi.stubGlobal('fetch', reply({ start_path: '/api/v1/auth/oauth/google/start?link=abc' }));
    await expect(requestProviderLink('jwt', 'github')).rejects.toThrow('Could not start');
    vi.stubGlobal('fetch', reply({ detail: 'For your security, sign in again to connect a sign-in method.' }, 403));
    await expect(requestProviderLink('jwt', 'github')).rejects.toBeInstanceOf(ReauthRequiredError);
  });
});
