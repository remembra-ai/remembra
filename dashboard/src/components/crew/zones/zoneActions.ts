// The human actions of the zone drawer and setup mode, as plain async
// functions over the crew API, so the drawer and the live test run the same
// calls in the same order (spec §5.9, §9.5, D37).

import type { CrewApi } from '../../../lib/crew/api';
import type { ClaimView, ZoneView } from '../../../lib/crew/types';
import type { ZonesApi } from './zonesApi';

export type ZoneAction = 'grant' | 'transfer' | 'revoke' | 'hold' | 'freeze' | 'unfreeze';

export interface ZoneActionInput {
  reason: string;
  sessionId: string | null;
  until: string | null;
}

/** Runs an action; the dashboard passes its step-up runner, tests pass `(what, fn) => fn()`. */
export type Run = <T>(what: string, action: () => Promise<T>) => Promise<T>;

export interface ZoneActionDeps {
  crewId: string;
  zone: Pick<ZoneView, 'id' | 'slug'>;
  /** The live claim the action applies to (holder, or the reserved baton). */
  target: Pick<ClaimView, 'id'> | null;
  crewApi: Pick<CrewApi, 'overrideClaim' | 'freezeZone' | 'unfreezeZone'>;
  zonesApi: Pick<ZonesApi, 'humanClaim' | 'releaseClaim'>;
  run: Run;
}

export async function performZoneAction(action: ZoneAction, deps: ZoneActionDeps, input: ZoneActionInput): Promise<void> {
  const { crewId, zone, target, crewApi, zonesApi, run } = deps;
  const needTarget = () => {
    if (!target) throw new Error(`Zone ${zone.slug} has no live claim to act on.`);
    return target.id;
  };
  switch (action) {
    case 'grant': {
      // A person takes the free zone, then hands it to the session (epoch 2).
      if (!input.sessionId) throw new Error('Pick the session to grant it to.');
      const res = await zonesApi.humanClaim(crewId, zone.id, input.reason);
      const claim = res.claim;
      if (!claim || !['granted', 'existing', 'retaken'].includes(res.status)) throw new Error(`Could not take zone ${zone.slug} first (${res.status}).`);
      try {
        await run('Granting a zone', () => crewApi.overrideClaim(claim.id, { action: 'transfer', to: input.sessionId, reason: input.reason }));
      } catch (err) {
        // do not leave the zone held by a person when the hand-over did not happen
        await zonesApi.releaseClaim(claim.id, 'grant not completed').catch(() => undefined);
        throw err;
      }
      return;
    }
    case 'transfer': {
      if (!input.sessionId) throw new Error('Pick the session to transfer it to.');
      const id = needTarget();
      await run('Transferring a zone', () => crewApi.overrideClaim(id, { action: 'transfer', to: input.sessionId, reason: input.reason }));
      return;
    }
    case 'revoke': {
      const id = needTarget();
      await run('Revoking a claim', () => crewApi.overrideClaim(id, { action: 'revoke', reason: input.reason }));
      return;
    }
    case 'hold': {
      const id = needTarget();
      await run('Holding a zone', () => crewApi.overrideClaim(id, { action: 'hold', reason: input.reason }));
      return;
    }
    case 'freeze':
      await run('Freezing a zone', () => crewApi.freezeZone(zone.id, input.reason, input.until));
      return;
    case 'unfreeze':
      await run('Unfreezing a zone', () => crewApi.unfreezeZone(zone.id, input.reason));
      return;
  }
}

/** Setup mode "Undo": remove every temporary (bootstrap) zone; returns how many were removed. */
export async function undoTemporaryZones(
  zones: readonly { id: string; source: string; archived_at?: string | null; builtin?: boolean }[],
  api: Pick<ZonesApi, 'archiveZone'>,
  run: Run,
): Promise<number> {
  const temporary = zones.filter((z) => z.source === 'suggested' && !z.archived_at && !z.builtin);
  for (const z of temporary) await run('Removing temporary zones', () => api.archiveZone(z.id));
  return temporary.length;
}
