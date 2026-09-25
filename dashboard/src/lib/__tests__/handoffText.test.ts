import { describe, expect, it } from 'vitest';
import { continueCommand, continuePrompt } from '../handoffText';
import type { TrailItem } from '../relay';

function item(overrides: Partial<TrailItem> = {}): TrailItem {
  return {
    id: 'h1',
    project_id: 'invoices-api',
    memory_type: 'handoff',
    agent_id: 'claude-code',
    session_id: 's1',
    created_at: '2026-09-25T10:00:00',
    branch: 'feat/rounding',
    head_commit: '1d50ae3f9b2c4d5e',
    headline: '2 commit(s)',
    failing: 1,
    open: 2,
    detail: {
      structured: true,
      done: ['1d50ae3 fix: rounding'],
      not_done: ['TODO: negative totals', 'uncommitted changes in 1 file(s): pdf.py'],
      failing: ['FAILING: pytest -q tests/test_rounding.py'],
      next: 'fix the rounding test',
      commits: [],
    },
    ...overrides,
  };
}

describe('continueCommand', () => {
  it('switches to the branch, then reads the brief for the project', () => {
    expect(continueCommand(item())).toBe('git switch feat/rounding && remembra-relay brief --project invoices-api');
  });

  it('skips the switch for a detached HEAD, a missing branch or an unsafe name', () => {
    expect(continueCommand(item({ branch: 'HEAD' }))).toBe('remembra-relay brief --project invoices-api');
    expect(continueCommand(item({ branch: null }))).toBe('remembra-relay brief --project invoices-api');
    expect(continueCommand(item({ branch: 'x; rm -rf ~' }))).toBe('remembra-relay brief --project invoices-api');
  });

  it('omits the default project and shell-quotes unusual project ids', () => {
    expect(continueCommand(item({ project_id: 'default', branch: null }))).toBe('remembra-relay brief');
    expect(continueCommand(item({ project_id: "it's mine", branch: null }))).toBe("remembra-relay brief --project 'it'\\''s mine'");
  });
});

describe('continuePrompt', () => {
  it('carries the agent, location, failing, open items and next step', () => {
    const text = continuePrompt(item());
    expect(text).toContain('Continue invoices-api from the last Remembra handoff (Claude Code, feat/rounding@1d50ae3).');
    expect(text).toContain('Failing: FAILING: pytest -q tests/test_rounding.py.');
    expect(text).toContain('Not done: TODO: negative totals; uncommitted changes in 1 file(s): pdf.py.');
    expect(text).toContain('Next step: fix the rounding test.');
    expect(text).toContain('session_brief');
  });

  it('falls back to the headline for a free-form handoff', () => {
    const text = continuePrompt(item({ detail: { structured: false, content: 'notes' }, headline: 'wrapped up the API' }));
    expect(text).toContain('Last note: wrapped up the API.');
  });
});
