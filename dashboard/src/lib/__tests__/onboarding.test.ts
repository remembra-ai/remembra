import { describe, expect, it } from 'vitest';
import { CONNECTABLE_AGENTS, PIPX_INSTALL, UNINSTALL_STEPS, agentMeta, oneLineInstall, saveKeyCommand } from '../agents';

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

describe('verified adapters', () => {
  it('match the relay: Claude Code and Codex are verified (relay/adapters verified=True), the rest are not', () => {
    // tests/test_install_commands.py checks the same list against the Python adapters.
    expect(CONNECTABLE_AGENTS.filter((id) => agentMeta(id).verified)).toEqual(['claude-code', 'codex']);
    expect(CONNECTABLE_AGENTS.filter((id) => !agentMeta(id).verified)).toEqual(['cursor', 'gemini', 'qwen', 'kimi']);
  });
});
