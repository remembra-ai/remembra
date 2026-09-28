import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { WS_CLOSE_NORMAL, WS_CLOSE_UNAUTHORIZED, shouldReconnect } from '../wsClose';

describe('shouldReconnect', () => {
  it('does not retry a deliberate close', () => {
    expect(shouldReconnect(WS_CLOSE_NORMAL)).toBe(false);
  });

  it('does not retry credentials the server refused (revoked, expired or signed out)', () => {
    expect(WS_CLOSE_UNAUTHORIZED).toBe(4001);
    expect(shouldReconnect(WS_CLOSE_UNAUTHORIZED)).toBe(false);
  });

  it('retries a dropped connection or a server error', () => {
    expect(shouldReconnect(1006)).toBe(true);
    expect(shouldReconnect(1011)).toBe(true);
    expect(shouldReconnect(1012)).toBe(true);
  });
});

describe('useWebSocket', () => {
  it('reconnects only when shouldReconnect allows it', () => {
    const hook = readFileSync(new URL('../../hooks/useWebSocket.ts', import.meta.url), 'utf8');
    expect(hook).toMatch(/if \(autoReconnect && shouldReconnect\(event\.code\)\)/);
    expect(hook).not.toMatch(/event\.code !== 1000/);
  });
});
