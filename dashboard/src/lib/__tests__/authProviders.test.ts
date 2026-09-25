import { describe, expect, it } from 'vitest';
import {
  appPath,
  normalizeAuthConfig,
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
    });
  });

  it('parses an error and the page it came from', () => {
    const f = parseOAuthFragment('#error=email_unverified&provider=github&from=signup');
    expect(f.error).toBe('email_unverified');
    expect(f.from).toBe('signup');
    expect(parseOAuthFragment('#from=elsewhere').from).toBe('login');
  });

  it('maps codes to fixed copy and never echoes unknown input', () => {
    expect(oauthErrorMessage('email_unverified', 'github')).toContain('verified primary email');
    expect(oauthErrorMessage('account_exists_unverified', 'google')).toContain('Forgot password');
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
