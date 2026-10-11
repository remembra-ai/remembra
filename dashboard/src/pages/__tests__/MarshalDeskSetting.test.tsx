// Settings > Diagnostics: the Marshal desk switch. The card with its exact
// label and help; its change handler; the save going out as PUT
// /marshal/settings before the desk is told; and no card when the server
// has no desk for this account.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MARSHAL_DESK_OFF, MarshalDeskContext, type MarshalDeskApi } from '../../hooks/marshalDesk';
import { api } from '../../lib/api';
import { DESK_COPY, deskToggle, setDeskSettings } from '../../lib/marshalDesk';
import { MarshalDeskSetting, MarshalDeskSettingCard } from '../../components/marshal/MarshalDeskSetting';

type AnyProps = Record<string, unknown> & { children?: ReactNode };

function elements(node: ReactNode): ReactElement<AnyProps>[] {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!isValidElement<AnyProps>(node)) return [];
  return [node, ...elements(node.props.children)];
}

function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ').trim();
}

const inDesk = (desk: Partial<MarshalDeskApi>) =>
  renderToStaticMarkup(
    <MarshalDeskContext.Provider value={{ ...MARSHAL_DESK_OFF, ...desk }}>
      <MarshalDeskSetting />
    </MarshalDeskContext.Provider>,
  );

describe('MarshalDeskSetting', () => {
  it('renders the exact label and help, checked while the desk is on', () => {
    const html = inDesk({ available: true });
    expect(text(html)).toContain(DESK_COPY.settingLabel);
    expect(text(html)).toContain(DESK_COPY.settingHelp);
    expect(html).toMatch(/<input[^>]*type="checkbox"[^>]*checked=""/);
    expect(html).toMatch(/aria-describedby="[^"]+-help"/);
  });

  it('unchecked when opted out; absent when settings answered 404 (no desk here)', () => {
    expect(inDesk({ available: true, optedOut: true })).not.toContain('checked=""');
    expect(inDesk({ available: false })).toBe('');
    expect(inDesk({})).toBe('');
  });

  it('the checkbox hands its new value to the handler', () => {
    const changes: boolean[] = [];
    const card = MarshalDeskSettingCard({ id: 'm', enabled: true, busy: false, error: null, onChange: (d) => changes.push(d) });
    const input = elements(card).find((el) => el.type === 'input') as ReactElement<AnyProps>;
    (input.props.onChange as (e: unknown) => void)({ target: { checked: false } });
    (input.props.onChange as (e: unknown) => void)({ target: { checked: true } });
    expect(changes).toEqual([false, true]);
  });

  it('while saving the checkbox is off; a failure is said', () => {
    const html = renderToStaticMarkup(<MarshalDeskSettingCard id="m" enabled busy error={DESK_COPY.unreachable} onChange={() => {}} />);
    expect(html).toMatch(/<input[^>]*disabled=""/);
    expect(html).toContain('role="alert"');
    expect(text(html)).toContain(DESK_COPY.unreachable);
  });
});

describe('the toggle it runs: save, then tell the desk', () => {
  const store = new Map<string, string>();
  const sent: { url: string; method: string; body: unknown; auth: string | null }[] = [];

  beforeEach(() => {
    store.clear();
    sent.length = 0;
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    api.clearAll();
    api.setJwtToken('desk-session');
  });

  afterEach(() => {
    api.clearAll();
    vi.unstubAllGlobals();
  });

  it('calls setDeskSettings(false), then dispatches the opt-out', async () => {
    vi.stubGlobal('fetch', async (url: string, init: RequestInit = {}) => {
      sent.push({ url, method: init.method ?? 'GET', body: JSON.parse(String(init.body)), auth: new Headers(init.headers).get('authorization') });
      return new Response(JSON.stringify({ desk: false }), { status: 200, headers: { 'content-type': 'application/json' } });
    });
    const applied: boolean[] = [];
    await deskToggle(setDeskSettings, (desk) => applied.push(desk))(false);
    expect(sent).toEqual([{ url: '/api/v1/marshal/settings', method: 'PUT', body: { desk: false }, auth: 'Bearer desk-session' }]);
    expect(applied).toEqual([false]);
  });

  it('a refused save leaves the desk as it was', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(JSON.stringify({ detail: { error: 'marshal_unavailable', message: "Marshal isn't available on this account." } }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const applied: boolean[] = [];
    await expect(deskToggle(setDeskSettings, (desk) => applied.push(desk))(false)).rejects.toThrow("Marshal isn't available on this account.");
    expect(applied).toEqual([]);
  });
});
