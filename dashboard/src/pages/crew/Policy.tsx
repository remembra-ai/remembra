// Policy (spec §9.1, route `#/crew?project=X&view=policy`): enforcement level,
// pending zone changes, bypass codes and git-hook status per checkout.

import { useMemo } from 'react';
import '../../components/crew/zones/zones.css';
import { useCrewSocket } from '../../hooks/useCrewSocket';
import { useNow } from '../../hooks/useResource';
import { useCrewRuntime } from '../../lib/crew/context';
import { liveSessions } from '../../lib/crew/selectors';
import { ErrorNotice, StaleNotice, TrailSkeleton } from '../../components/relay/ui';
import { PolicyPanel } from '../../components/crew/policy/PolicyPanel';
import { checkoutRows } from '../../components/crew/policy/policyModel';
import { StepUpDialog } from '../../components/crew/policy/StepUpDialog';
import { useHumanAction } from '../../components/crew/policy/useHumanAction';
import { actorAccess, liveWords } from '../../components/crew/zones/live';
import { ScreenHeader } from '../../components/crew/zones/ScreenHeader';
import { useCrewAccess, useEventTail, useLiveLoad, useZonesApi } from '../../components/crew/zones/zonesData';

export function PolicyPage({ crewId, project }: { crewId: string; project: string }) {
  const crew = useCrewSocket(crewId);
  const runtime = useCrewRuntime();
  const zapi = useZonesApi();
  const runner = useHumanAction();
  const now = useNow(1000);
  const state = crew.state;
  const detail = useCrewAccess(crewId);
  const access = actorAccess(detail.data);
  const canAdmin = !!detail.data?.human && !!detail.data?.permissions.includes('crew:admin');
  const adminWhy = !detail.data
    ? 'Checking your access…'
    : !detail.data.human
      ? 'Only a dashboard login can change enforcement; API keys never can.'
      : canAdmin
        ? null
        : `Your crew role (${detail.data.role}) cannot change enforcement; an owner can.`;
  const tail = useEventTail(state ? crewId : null, state?.last_seq ?? 0);
  const pendingSig = state ? Object.keys(state.pending_zone_changes).sort().join(',') : '';
  const settingsSig = `${state?.crew?.settings_version ?? 0}`;
  const pending = useLiveLoad(crewId, pendingSig, () => zapi.zoneChanges(crewId, 'pending'));
  const recent = useLiveLoad(crewId, pendingSig, () => zapi.zoneChanges(crewId));
  const bypassSig = useMemo(() => tail.events.filter((e) => e.type === 'guard.bypass_used').length, [tail.events]);
  const codes = useLiveLoad(access.canAct ? crewId : null, String(bypassSig), () => zapi.bypassCodes(crewId));
  const settings = useLiveLoad(crewId, settingsSig, () => runtime.api.getCrew(crewId));

  if (crew.status === 'not_found') return <ErrorNotice error={crew.error} what={`the ${project} crew`} />;
  if (!state) {
    return crew.error ? <ErrorNotice error={crew.error} what={`the ${project} crew`} onRetry={crew.refresh} /> : <TrailSkeleton rows={4} />;
  }
  const conn = liveWords(crew.status, crew.connection);
  const level = state.crew?.enforcement ?? 'enforce';
  const missing = checkoutRows(state).filter((r) => r.hook === 'missing').length;
  const activeCodes = (codes.data?.codes ?? []).filter((c) => c.state === 'active').length;
  const offset = crew.meta?.clock_offset_ms ?? 0;
  // The crew state follows the action's events (stream or polling); only REST listings reload here.
  const refreshAll = () => {
    pending.refresh();
    recent.refresh();
    settings.refresh();
    detail.refresh();
  };

  return (
    <div className="cz-root space-y-3">
      <ScreenHeader
        project={project}
        screen="policy"
        title="Policy"
        lede="How hard the crew is held to its zones, and every way around it: changes waiting for you, bypass codes and the git gates on each checkout. Only you can loosen any of it."
        seq={state.last_seq}
        live={conn.live}
        strip={[
          conn.text,
          `seq ${state.last_seq}`,
          level,
          `${Object.keys(state.pending_zone_changes).length} pending`,
          access.canAct ? `${activeCodes} active code${activeCodes === 1 ? '' : 's'}` : '',
          missing ? `${missing} gate${missing === 1 ? '' : 's'} missing` : `${liveSessions(state).length} agents gated`,
        ]}
      />
      {Boolean(pending.error || settings.error) && <StaleNotice error={pending.error || settings.error} what="the policy" />}
      <PolicyPanel
        crewId={crewId}
        state={state}
        detail={settings.data ?? detail.data}
        access={access}
        canAdmin={canAdmin}
        adminWhy={adminWhy}
        offsetMs={offset}
        now={now}
        pendingChanges={pending.data ?? []}
        recentChanges={recent.data ?? []}
        codes={codes.data?.codes}
        codesError={codes.error}
        events={tail.events}
        api={runtime.api}
        runner={runner}
        onChanged={refreshAll}
        onCodesChanged={codes.refresh}
      />
      {runner.prompt && <StepUpDialog prompt={runner.prompt} />}
    </div>
  );
}
