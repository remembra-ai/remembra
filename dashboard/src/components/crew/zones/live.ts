// Words for the live status strip (status never depends on colour alone, §9).

import type { ConnectionStatus } from '../../../lib/crew/socket';
import type { CrewStreamStatus } from '../../../lib/crew/store';
import type { CrewAccessBody } from '../../../lib/crew/types';

export function liveWords(status: CrewStreamStatus, connection: ConnectionStatus): { text: string; live: boolean } {
  if (status === 'live') return { text: 'live', live: true };
  if (status === 'resyncing') return { text: 'catching up', live: false };
  if (connection === 'unauthorized') return { text: 'signed out: reload to sign in', live: false };
  if (connection === 'forbidden') return { text: 'access changed: retrying', live: false };
  if (status === 'polling') return { text: 'updating every few seconds', live: false };
  if (status === 'loading') return { text: 'loading', live: false };
  return { text: status.replace('_', ' '), live: false };
}

export interface ActorAccess {
  canAct: boolean;
  why: string | null;
}

/** May this login take human-only actions on the crew (D27: a dashboard login with role owner or admin)? */
export function actorAccess(body: Pick<CrewAccessBody, 'human' | 'role' | 'permissions'> | undefined | null): ActorAccess {
  if (!body) return { canAct: false, why: 'Checking your access…' };
  if (!body.human) return { canAct: false, why: 'Only a dashboard login can do this; API keys never can.' };
  if (!body.permissions.includes('crew:override')) {
    return { canAct: false, why: `Your crew role (${body.role}) can see this but not change it; an owner or admin can.` };
  }
  return { canAct: true, why: null };
}
