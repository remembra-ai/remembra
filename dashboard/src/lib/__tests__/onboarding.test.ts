import { describe, expect, it } from 'vitest';
import { oneLineInstall } from '../agents';

describe('oneLineInstall', () => {
  it('chains install, key save and connect --apply with the real key', () => {
    expect(oneLineInstall('https://api.example.test', 'rem_abc123XYZ')).toBe(
      "pipx install --force 'remembra>=0.16' && remembra-install --all --api-key rem_abc123XYZ --url https://api.example.test && remembra-relay connect --apply",
    );
  });

  it('keeps the placeholder without a key, and defaults the server', () => {
    const line = oneLineInstall('', null);
    expect(line).toContain('--api-key <your-key>');
    expect(line).toContain('--url https://api.remembra.dev');
  });

  it('never splices shell syntax from a malformed key into the command', () => {
    expect(oneLineInstall('https://x.test', 'abc; rm -rf ~')).toContain('--api-key <your-key>');
  });
});
