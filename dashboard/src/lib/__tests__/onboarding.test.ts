import { describe, expect, it } from 'vitest';
import { PIPX_INSTALL, UNINSTALL_STEPS, oneLineInstall, saveKeyCommand } from '../agents';

describe('oneLineInstall', () => {
  it('chains install, key save and connect --apply, with the key asked for at a prompt', () => {
    expect(oneLineInstall('https://api.example.test')).toBe(
      "pipx install --force 'remembra[mcp]>=0.16' && remembra-install --all --url https://api.example.test && remembra-relay connect --apply",
    );
  });

  it('defaults the server', () => {
    expect(oneLineInstall('')).toContain('--url https://api.remembra.dev');
  });

  it('never puts a key on the command line', () => {
    for (const line of [oneLineInstall('https://x.test'), saveKeyCommand('https://x.test')]) {
      expect(line).not.toContain('--api-key');
      expect(line).not.toMatch(/rem_[A-Za-z0-9]/);
      expect(line).not.toContain('<your-key>');
    }
  });

  it('installs the [mcp] extra, which remembra-mcp needs', () => {
    expect(PIPX_INSTALL).toContain("'remembra[mcp]>=0.16'");
    expect(oneLineInstall('')).toContain("'remembra[mcp]>=0.16'");
  });
});

describe('uninstall steps', () => {
  it('lists disconnect, the MCP removal and pipx uninstall, in that order', () => {
    const commands = UNINSTALL_STEPS.map((step) => step.command);
    expect(commands.indexOf('remembra-relay disconnect --apply')).toBe(0);
    expect(commands.indexOf('remembra-install --remove --all --apply')).toBe(1);
    expect(commands.indexOf('pipx uninstall remembra')).toBe(2);
  });
});
