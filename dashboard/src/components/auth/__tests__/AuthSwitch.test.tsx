import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ApiKeyForm } from '../../ApiKeyForm';
import { Login } from '../../../pages/Login';
import { Signup } from '../../../pages/Signup';
import { AuthSwitch } from '../AuthSwitch';

const noop = () => {};
const APP = readFileSync(new URL('../../../App.tsx', import.meta.url), 'utf8');

// The "Use API Key instead" link used to be `fixed bottom-4 right-4`: on a 375x667 phone the Sign up
// button scrolled under it. It now renders in the page flow, after the form's submit button.
describe('auth mode switch', () => {
  beforeAll(() => {
    // ApiKeyForm reads the saved user and project ids (node has no localStorage).
    const store = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
    });
  });

  it('is never positioned over the page', () => {
    const html = renderToStaticMarkup(<AuthSwitch label="Use API Key instead" onClick={noop} />);
    expect(html).toContain('Use API Key instead');
    expect(html).not.toMatch(/\b(fixed|absolute|sticky)\b/);
    expect(APP).not.toMatch(/className="fixed bottom-4 right-4"/);
  });

  it.each([
    ['Login', <Login onLogin={noop} onSwitchToSignup={noop} onForgotPassword={noop} footer={<AuthSwitch label="Use API Key instead" onClick={noop} />} />],
    ['Signup', <Signup onSignup={noop} onSwitchToLogin={noop} footer={<AuthSwitch label="Use API Key instead" onClick={noop} />} />],
    ['ApiKeyForm', <ApiKeyForm onAuthenticated={noop} footer={<AuthSwitch label="Sign in with email" onClick={noop} />} />],
  ])('%s renders it after the submit button, inside the form column', (_name, element) => {
    const html = renderToStaticMarkup(element);
    const submit = html.indexOf('type="submit"');
    const link = Math.max(html.indexOf('Use API Key instead'), html.indexOf('Sign in with email'));
    expect(submit).toBeGreaterThan(-1);
    expect(link).toBeGreaterThan(submit);
    // Same max-w-md column as the form: it closes after the link.
    const column = html.indexOf('max-w-md w-full');
    expect(column).toBeGreaterThan(-1);
    expect(column).toBeLessThan(submit);
  });

  it('App passes the switch to each sign-in form', () => {
    expect(APP.match(/footer=\{<AuthSwitch label="Use API Key instead"/g)?.length).toBe(2);
    expect(APP).toContain('footer={<AuthSwitch label="Sign in with email"');
  });
});
