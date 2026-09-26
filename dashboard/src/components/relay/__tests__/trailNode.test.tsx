// Server-render the real trail components (no DOM needed) to check the
// health badge (R-21), "picked up by" (R-18) and the detail's health list.

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { HandoffDetail, TrailNode } from '../Handoff';
import type { TrailItem } from '../../../lib/relay';

const NOW = new Date('2026-09-26T12:00:00Z');

const item = (over: Partial<TrailItem> = {}): TrailItem => ({
  id: 'h1',
  project_id: 'widget',
  memory_type: 'handoff',
  agent_id: 'claude-code',
  session_id: 's1',
  created_at: '2026-09-26T11:50:00Z',
  branch: 'main',
  head_commit: 'b'.repeat(40),
  headline: '2 commit(s), last: feat: widget api',
  failing: 0,
  open: 1,
  detail: {
    structured: true,
    done: ['bbbbbbb feat: widget api'],
    not_done: ['2 commit(s) not pushed to origin/main'],
    failing: [],
    next: 'push 2 commit(s) to origin/main',
    commits: [],
  },
  health: {
    status: 'ready_with_warnings',
    label: 'Ready with warnings',
    missing: ['2 commit(s) not pushed', 'tests not run'],
    warnings: ['summary contradicted: claims tests pass, but no test run was recorded'],
    rules_version: 1,
  },
  picked_up_by: [{ agent_id: 'codex', agent_verified: true, picked_up_at: '2026-09-26T11:52:00Z', gap_seconds: 120 }],
  ...over,
});

describe('TrailNode', () => {
  it('shows the health badge and who picked the handoff up', () => {
    const html = renderToStaticMarkup(<TrailNode item={item()} expanded={false} onToggle={() => {}} now={NOW} />);
    expect(html).toContain('Ready with warnings');
    expect(html).toContain('title="Ready with warnings: 2 commit(s) not pushed; tests not run"');
    expect(html).toMatch(/picked up by Codex 2m after it stopped/);
  });

  it('shows neither for an ungraded handoff nobody picked up', () => {
    const html = renderToStaticMarkup(
      <TrailNode item={item({ health: null, picked_up_by: [] })} expanded={false} onToggle={() => {}} now={NOW} />,
    );
    expect(html).not.toContain('Ready');
    expect(html).not.toContain('picked up by');
  });

  it('lists what is missing and the contradicted claims when expanded', () => {
    const html = renderToStaticMarkup(<HandoffDetail item={item()} />);
    expect(html).toContain('Health: Ready with warnings');
    expect(html).toContain('tests not run');
    expect(html).toContain('summary contradicted: claims tests pass, but no test run was recorded');
  });
});
