import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ADMIN_KEY_NOTE, CREATABLE_KEY_ROLES, KEY_ROLE_SUMMARIES } from '../../lib/keyRoles';
import { KeyRolePicker } from '../ApiKeyManager';

const noop = () => {};

// Truth audit P-347: the key form offered "Admin: Full root access. Can delete workspaces and manage
// billing." The server refuses to create admin keys from the dashboard (403) and no key can manage billing.
describe('API key role picker', () => {
  const html = renderToStaticMarkup(<KeyRolePicker value="editor" onChange={noop} />);

  it('offers exactly the roles the server accepts from a dashboard session', () => {
    expect(CREATABLE_KEY_ROLES).toEqual(['editor', 'viewer']);
    const offered = [...html.matchAll(/type="radio"[^>]*value="([a-z]+)"/g)].map((m) => m[1]);
    expect(offered).toEqual(['editor', 'viewer']);
    expect(html).not.toContain('value="admin"');
  });

  it('describes each role as the server enforces it', () => {
    expect(html).toContain(KEY_ROLE_SUMMARIES.editor);
    expect(html).toContain(KEY_ROLE_SUMMARIES.viewer);
    expect(html).toContain('master key');
    expect(html).toContain(ADMIN_KEY_NOTE.replace("'", '&#x27;'));
    expect(html).not.toMatch(/billing|root access|workspaces/i);
  });
});
