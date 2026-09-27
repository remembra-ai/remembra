// The phone check-in bar's data: tabs with live badges. The crew list (and
// its summary subscription) is only kept live on a phone-sized screen.

import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { useCrewList, useCrewMode } from '../../../lib/crew/hooks';
import { useRoute } from '../../../lib/nav';
import { checkInItems, lastCrewProjectSeen, moreSections, rememberCrewProject, type CheckInItem } from './checkin';

const PHONE_QUERY = '(max-width: 767.98px)';

function subscribePhone(listener: () => void): () => void {
  if (typeof window === 'undefined' || !window.matchMedia) return () => {};
  const mq = window.matchMedia(PHONE_QUERY);
  mq.addEventListener?.('change', listener);
  return () => mq.removeEventListener?.('change', listener);
}

function isPhone(): boolean {
  try {
    return typeof window !== 'undefined' && !!window.matchMedia?.(PHONE_QUERY).matches;
  } catch {
    return false;
  }
}

export function useIsPhone(): boolean {
  return useSyncExternalStore(subscribePhone, isPhone, () => false);
}

export function useCheckIn(agentInboxUnread: number, isAdmin: boolean): { items: CheckInItem[]; more: ReturnType<typeof moreSections> } {
  const route = useRoute();
  const phone = useIsPhone();
  const crewOff = useCrewMode() === 'off';
  const list = useCrewList(phone && !crewOff);
  useEffect(() => rememberCrewProject(route), [route]);
  const items = useMemo(
    () => checkInItems(route, list.items, agentInboxUnread, lastCrewProjectSeen(), crewOff),
    [route, list.items, agentInboxUnread, crewOff],
  );
  const more = useMemo(() => moreSections(isAdmin, crewOff), [isAdmin, crewOff]);
  return { items, more };
}
