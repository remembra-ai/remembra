// Text an agent (or a person) can paste to continue from a handoff.

import type { TrailItem } from './relay';
import { agentMeta } from './agents';
import { where } from './time';

const SAFE_BRANCH = /^[A-Za-z0-9._/-]+$/;

export function continueCommand(item: TrailItem): string {
  const id = item.project_id && item.project_id !== 'default' ? item.project_id : '';
  const project = id ? ` --project ${SAFE_BRANCH.test(id) ? id : `'${id.replace(/'/g, "'\\''")}'`}` : '';
  const brief = `remembra-relay brief${project}`;
  const branch = (item.branch || '').trim();
  if (branch && branch !== 'HEAD' && SAFE_BRANCH.test(branch)) return `git switch ${branch} && ${brief}`;
  return brief;
}

export function continuePrompt(item: TrailItem): string {
  const meta = agentMeta(item.agent_id);
  const at = where(item.branch, item.head_commit);
  const project = item.project_id && item.project_id !== 'default' ? item.project_id : 'this project';
  const parts = [`Continue ${project} from the last Remembra handoff (${meta.name}${at ? `, ${at}` : ''}).`];
  const detail = item.detail;
  if (detail?.structured) {
    if (detail.failing.length) parts.push(`Failing: ${detail.failing.slice(0, 2).join('; ')}.`);
    if (detail.not_done.length) parts.push(`Not done: ${detail.not_done.slice(0, 3).join('; ')}.`);
    if (detail.next) parts.push(`Next step: ${detail.next}.`);
  } else if (item.headline) {
    parts.push(`Last note: ${item.headline}.`);
  }
  parts.push('Start by reading the session brief (remembra-relay brief, or the session_brief tool).');
  return parts.join(' ');
}

