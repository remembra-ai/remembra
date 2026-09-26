// Row targets as hash links (deep-linkable, open in a new tab, work on a phone).

import { agentHref, crewHref } from '../../../lib/crew/routes';
import type { RowTarget, RowTone } from './model';

export function targetHref(project: string, target: RowTarget | null): string | null {
  if (!target) return null;
  switch (target.screen) {
    case 'board':
      return crewHref(project, 'board', { task: target.task });
    case 'zones':
      return crewHref(project, 'zones', { zone: target.zone });
    case 'channel':
      return crewHref(project, 'channel', { thread: target.thread });
    case 'report':
      return crewHref(project, 'report', { report: target.report });
    case 'policy':
      return crewHref(project, 'policy');
    case 'agent':
      return agentHref(target.agent, target.session);
  }
}

export function targetLabel(target: RowTarget | null): string {
  if (!target) return '';
  switch (target.screen) {
    case 'board':
      return `Open ${target.task} on the board`;
    case 'zones':
      return `Open zone ${target.zone}`;
    case 'channel':
      return 'Open in the channel';
    case 'report':
      return 'Open the report receipt';
    case 'policy':
      return 'Open policy';
    case 'agent':
      return `Open ${target.agent}`;
  }
}

/** Glyph node and status word colours per tone. Orange only for signal (moves / needs you). */
export const TONE_CLASS: Record<RowTone, { node: string; word: string }> = {
  neutral: { node: 'border-rule bg-panel text-ink-3', word: 'text-ink-3' },
  signal: { node: 'border-signal bg-signal-wash text-signal-ink', word: 'text-signal-ink' },
  ok: { node: 'border-ok/50 bg-ok-wash text-ok', word: 'text-ok' },
  fail: { node: 'border-fail/50 bg-fail-wash text-fail', word: 'text-fail' },
  warn: { node: 'border-amber-400/50 bg-panel text-amber-400', word: 'text-amber-400' },
};
