// Human actions offered on lanes and pickup slots (spec §9.3 lane menu,
// §9.4 Needs-you actions, §5.9). All are human-only (H): the server accepts
// them only from a dashboard login, some after a fresh login (step-up). Every
// one asks for a reason, which lands in the audit trail.
//
// Pure request logic (which endpoint, which body, what to say afterwards);
// the dialog renders the inputs and shows `flowErrorMessage` on failure.

import type { CrewApi } from '../../../lib/crew/api';

export type LaneActionId = 'checkpoint' | 'pause' | 'resume' | 'release-all' | 'hand-over' | 'hand-baton' | 'release-baton';

export interface LaneActionDef {
  id: LaneActionId;
  label: string;
  /** Needs a target session. */
  needsTarget: boolean;
  /** Acts on a subset of claims the user ticks. */
  pickClaims: boolean;
  confirm: string;
  /** Asks for a fresh login on the server (shown up front). */
  stepUp: boolean;
}

export const LANE_ACTIONS: Record<LaneActionId, LaneActionDef> = {
  checkpoint: {
    id: 'checkpoint',
    label: 'Request checkpoint',
    needsTarget: false,
    pickClaims: false,
    confirm: 'Ask for a checkpoint',
    stepUp: false,
  },
  pause: { id: 'pause', label: 'Pause', needsTarget: false, pickClaims: false, confirm: 'Pause this agent', stepUp: false },
  resume: { id: 'resume', label: 'Resume', needsTarget: false, pickClaims: false, confirm: 'Resume this agent', stepUp: false },
  'release-all': {
    id: 'release-all',
    label: 'Release all claims',
    needsTarget: false,
    pickClaims: false,
    confirm: 'Release every claim',
    stepUp: false,
  },
  'hand-over': { id: 'hand-over', label: 'Hand over zones…', needsTarget: true, pickClaims: true, confirm: 'Hand over', stepUp: true },
  'hand-baton': {
    id: 'hand-baton',
    label: 'Hand baton to…',
    needsTarget: true,
    pickClaims: false,
    confirm: 'Hand the baton over',
    stepUp: false,
  },
  'release-baton': {
    id: 'release-baton',
    label: 'Release',
    needsTarget: false,
    pickClaims: false,
    confirm: 'Release the zones',
    stepUp: true,
  },
};

export interface LaneActionInput {
  action: LaneActionId;
  reason: string;
  /** The lane's session (checkpoint, pause, resume, release-all, hand-over). */
  sessionId?: string | null;
  /** Claims to act on (hand-over: ticked; hand-baton without a task and release-baton: every claim of the slot). */
  claimIds?: string[];
  /** Task of a baton slot (hand-baton assigns the task, which records an offer). */
  taskId?: string | null;
  /** Target session (hand-over, hand-baton). */
  to?: string | null;
  /** Callsigns for the confirmation text. */
  names?: { session?: string; to?: string };
}

export type LaneApi = Pick<
  CrewApi,
  'requestCheckpoint' | 'pauseSession' | 'resumeSession' | 'releaseAllClaims' | 'overrideClaim' | 'assignTask'
>;

export const REASON_MAX = 280;

/** Why the input cannot be sent yet, or null. */
export function validateAction(input: LaneActionInput): string | null {
  const def = LANE_ACTIONS[input.action];
  const reason = input.reason.trim();
  if (!reason) return 'Give a reason (it is recorded in the audit trail).';
  if (reason.length > REASON_MAX) return `Keep the reason under ${REASON_MAX} characters.`;
  if (def.needsTarget && !input.to) return 'Choose who gets it.';
  if (['checkpoint', 'pause', 'resume', 'release-all', 'hand-over'].includes(input.action) && !input.sessionId) return 'No agent selected.';
  if ((input.action === 'hand-over' || input.action === 'release-baton') && !(input.claimIds && input.claimIds.length)) {
    return 'Choose at least one zone.';
  }
  if (input.action === 'hand-baton' && !input.taskId && !(input.claimIds && input.claimIds.length)) return 'Nothing to hand over.';
  return null;
}

/** Run the action against the API; resolves to the confirmation text. Throws CrewApiError on refusal. */
export async function runLaneAction(api: LaneApi, input: LaneActionInput): Promise<string> {
  const problem = validateAction(input);
  if (problem) throw new Error(problem);
  const reason = input.reason.trim();
  const who = input.names?.session ?? input.sessionId ?? 'the agent';
  const to = input.names?.to ?? input.to ?? '';
  switch (input.action) {
    case 'checkpoint':
      await api.requestCheckpoint(input.sessionId!, reason);
      return `Asked ${who} for a checkpoint.`;
    case 'pause':
      await api.pauseSession(input.sessionId!, reason);
      return `Paused ${who}. Its next write is denied until you resume it.`;
    case 'resume':
      await api.resumeSession(input.sessionId!, reason);
      return `Resumed ${who}.`;
    case 'release-all':
      await api.releaseAllClaims(input.sessionId!, reason);
      return `Released every claim ${who} held.`;
    case 'hand-over':
      for (const claimId of input.claimIds!) await api.overrideClaim(claimId, { action: 'transfer', to: input.to!, reason });
      return `Handed ${input.claimIds!.length === 1 ? 'the zone' : `${input.claimIds!.length} zones`} to ${to}.`;
    case 'hand-baton':
      if (input.taskId) {
        await api.assignTask(input.taskId, input.to!);
        return `Handed the baton to ${to}: the task and its zones are now its.`;
      }
      for (const claimId of input.claimIds!) await api.overrideClaim(claimId, { action: 'transfer', to: input.to!, reason });
      return `Handed the held zones to ${to}.`;
    case 'release-baton':
      for (const claimId of input.claimIds!) await api.overrideClaim(claimId, { action: 'revoke', reason });
      return `Released ${input.claimIds!.length === 1 ? 'the zone' : `${input.claimIds!.length} zones`}. Other agents can claim it now.`;
  }
}
