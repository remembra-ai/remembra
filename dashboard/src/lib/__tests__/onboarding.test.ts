import { describe, expect, it } from 'vitest';
import {
  CONNECTABLE_AGENTS,
  PIPX_INSTALL,
  UNINSTALL_STEPS,
  agentMeta,
  canonicalAgentId,
  joinNames,
  oneLineInstall,
  saveKeyCommand,
} from '../agents';

describe('oneLineInstall', () => {
  it('chains install, key save and connect --apply, with the key asked for at a prompt', () => {
    expect(oneLineInstall('https://api.example.test')).toBe(
      "pipx install --force 'remembra[mcp]>=0.16' && remembra-install --all --url https://api.example.test && remembra-relay connect --apply",
    );
  });

  it('without a server, keeps the one the machine uses (Remembra Cloud on a first install): the remembra.dev line', () => {
    expect(oneLineInstall('')).toBe(
      "pipx install --force 'remembra[mcp]>=0.16' && remembra-install --all && remembra-relay connect --apply",
    );
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
    // --delete-backups: the *.bak-remembra-* / *.bak-relay-* copies of agent configs still hold the key.
    expect(commands.indexOf('remembra-install --remove --all --apply --delete-backups')).toBe(1);
    expect(commands.indexOf('pipx uninstall remembra')).toBe(2);
  });
});

describe('verified adapters', () => {
  it('match the relay: every adapter but Cursor is verified (relay/adapters verified=True)', () => {
    // tests/test_install_commands.py checks the same list against the Python adapters.
    expect(CONNECTABLE_AGENTS.filter((id) => agentMeta(id).verified)).toEqual(['claude-code', 'codex', 'gemini', 'qwen', 'kimi']);
    expect(CONNECTABLE_AGENTS.filter((id) => !agentMeta(id).verified)).toEqual(['cursor']);
  });

  it('names Kimi Code by its product name, under its old and new ids', () => {
    expect(agentMeta('kimi').name).toBe('Kimi Code');
    expect(canonicalAgentId('kimi-code')).toBe('kimi');
    expect(canonicalAgentId('kimi-cli')).toBe('kimi');
  });

  it('lists names the way a sentence does', () => {
    expect(joinNames([])).toBe('');
    expect(joinNames(['Cursor'])).toBe('Cursor');
    expect(joinNames(['Claude Code', 'Codex'])).toBe('Claude Code and Codex');
    expect(joinNames(['Claude Code', 'Codex', 'Gemini CLI'])).toBe('Claude Code, Codex and Gemini CLI');
  });
});
