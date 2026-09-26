// Zone Map (spec §9.5, route `#/crew?project=X&view=zones[&zone=pos]`): the
// repository tree with the crew's zones overlaid, live; the zone drawer; setup
// mode after the no-zone bootstrap; pending zone changes. L0 is the tree; the
// treemap site plan is L1.

import { useMemo, useState } from 'react';
import '../../components/crew/zones/zones.css';
import { useCrewSocket } from '../../hooks/useCrewSocket';
import { useNow } from '../../hooks/useResource';
import { goToCrew } from '../../lib/crew/routes';
import { useCrewRuntime } from '../../lib/crew/context';
import { liveSessions, sortedZones } from '../../lib/crew/selectors';
import type { CrewState } from '../../lib/crew/types';
import { Card, ErrorNotice, StaleNotice, TrailSkeleton } from '../../components/relay/ui';
import { StepUpDialog } from '../../components/crew/policy/StepUpDialog';
import { useHumanAction } from '../../components/crew/policy/useHumanAction';
import { actorAccess, liveWords } from '../../components/crew/zones/live';
import { PendingZoneChanges } from '../../components/crew/zones/PendingZoneChanges';
import { PixelGlyph } from '../../components/crew/zones/PixelGlyph';
import { ScreenHeader } from '../../components/crew/zones/ScreenHeader';
import { ZoneDrawer } from '../../components/crew/zones/ZoneDrawer';
import { ZoneSetup } from '../../components/crew/zones/ZoneSetup';
import { ZoneTree } from '../../components/crew/zones/ZoneTree';
import { useCrewAccess, useEventTail, useLiveLoad, useZonesApi } from '../../components/crew/zones/zonesData';
import {
  PRIMARY_LABEL,
  ancestorsOf,
  anchorPaths,
  buildZoneTree,
  defaultExpanded,
  pendingTargets,
  zoneStatus,
  type ZonePrimary,
  type ZoneStatus,
} from '../../components/crew/zones/zoneModel';

const LEGEND: ZonePrimary[] = ['free', 'held', 'shared', 'watched', 'reserved', 'contested', 'frozen', 'breach'];

function zonesSignature(state: CrewState | null): string {
  if (!state) return '';
  const zones = Object.values(state.zones)
    .map((z) => `${z.id}:${z.version}`)
    .sort()
    .join(',');
  return `${zones}|${Object.keys(state.pending_zone_changes).sort().join(',')}`;
}

export function ZonesPage({ crewId, project, zoneSlug }: { crewId: string; project: string; zoneSlug: string | null }) {
  const crew = useCrewSocket(crewId);
  const runtime = useCrewRuntime();
  const zapi = useZonesApi();
  const runner = useHumanAction();
  const now = useNow(1000);
  const state = crew.state;
  const sig = zonesSignature(state);
  const listing = useLiveLoad(crewId, sig, () => zapi.listZones(crewId));
  const pendingSig = state ? Object.keys(state.pending_zone_changes).sort().join(',') : '';
  const changes = useLiveLoad(crewId, pendingSig, () => zapi.zoneChanges(crewId, 'pending'));
  const detail = useCrewAccess(crewId);
  const access = actorAccess(detail.data);
  const tail = useEventTail(state ? crewId : null, state?.last_seq ?? 0);
  const [toggles, setToggles] = useState<Map<string, boolean>>(new Map());

  const offset = crew.meta?.clock_offset_ms ?? 0;
  const nowMs = now.getTime() + offset;
  const pending = useMemo(() => pendingTargets(changes.data ?? []), [changes.data]);

  const statuses = useMemo(() => {
    const map = new Map<string, ZoneStatus>();
    if (!state) return map;
    for (const z of Object.values(state.zones)) map.set(z.id, zoneStatus(state, z, pending.slugs, nowMs));
    return map;
  }, [state, pending, nowMs]);

  const tree = useMemo(() => (state ? buildZoneTree(Object.values(state.zones), listing.data?.tree?.tree) : null), [state, listing.data]);
  const selected = state && zoneSlug ? (Object.values(state.zones).find((z) => z.slug === zoneSlug) ?? null) : null;

  const expanded = useMemo(() => {
    const open = tree ? defaultExpanded(tree) : new Set<string>();
    for (const [path, isOpen] of toggles) {
      if (isOpen) open.add(path);
      else open.delete(path);
    }
    if (selected) for (const p of anchorPaths(selected)) for (const a of ancestorsOf(p)) open.add(a);
    return open;
  }, [tree, toggles, selected]);

  const flash = useMemo(() => {
    const ids = new Set<string>();
    for (const e of tail.events.slice(-30)) {
      const at = Date.parse(e.ts);
      if (e.refs?.zone_id && !Number.isNaN(at) && nowMs - at < 2500) ids.add(e.refs.zone_id);
    }
    return ids;
  }, [tail.events, nowMs]);

  if (crew.status === 'not_found') return <ErrorNotice error={crew.error} what={`the ${project} crew`} />;
  if (!state) {
    return crew.error ? <ErrorNotice error={crew.error} what={`the ${project} crew`} onRetry={crew.refresh} /> : <TrailSkeleton rows={4} />;
  }

  const zones = sortedZones(state);
  const userZones = zones.filter((z) => !z.builtin);
  const builtin = zones.filter((z) => z.builtin);
  const conn = liveWords(crew.status, crew.connection);
  const count = (p: ZonePrimary) => userZones.filter((z) => statuses.get(z.id)?.primary === p).length;
  const held = count('held') + count('shared') + count('watched') + count('contested');
  const enforcement = state.crew?.enforcement ?? 'enforce';
  // After an action only the REST listings are reloaded: the crew state moves by the events the
  // action produces (stream or polling). A snapshot reload would reset the live-only state (lane
  // badges, moments) and spend a replay subscribe.
  const refreshAll = () => {
    listing.refresh();
    changes.refresh();
  };
  const open = (zoneId: string) => {
    const z = state.zones[zoneId];
    if (z) goToCrew(project, 'zones', { zone: z.slug });
  };
  const close = () => goToCrew(project, 'zones', {});
  const toggle = (path: string, force?: boolean) =>
    setToggles((prev) => {
      const next = new Map(prev);
      next.set(path, force ?? !expanded.has(path));
      return next;
    });

  const bootstrap = listing.data?.bootstrap_zones ?? false;

  return (
    <div className="cz-root space-y-3">
      <ScreenHeader
        project={project}
        screen="zones"
        title="Zone map"
        lede={
          <>
            Who is working where, and what must not be touched. Zones cover folders; a held zone is closed to the other agents
            {enforcement === 'enforce'
              ? ': denied before the write where their hooks enforce it, read-only where they get a fence, and at commit and push where the git gates are installed.'
              : enforcement === 'observe'
                ? ' (observe mode: logged, not denied).'
                : ' (the gate is off).'}
          </>
        }
        seq={state.last_seq}
        live={conn.live}
        strip={[
          conn.text,
          `seq ${state.last_seq}`,
          `${liveSessions(state).length} agent${liveSessions(state).length === 1 ? '' : 's'}`,
          `${userZones.length} zone${userZones.length === 1 ? '' : 's'}`,
          held ? `${held} held` : '',
          count('reserved') ? `${count('reserved')} reserved` : '',
          count('breach') ? `${count('breach')} breach` : '',
          enforcement,
        ]}
      />

      {Boolean(listing.error || changes.error) && <StaleNotice error={listing.error || changes.error} what="the zone listing" />}

      <PendingZoneChanges
        state={state}
        changes={changes.data ?? []}
        now={now}
        canAct={access.canAct}
        why={access.why}
        crewApi={runtime.api}
        runner={runner}
        onChanged={refreshAll}
      />

      {listing.data && (bootstrap || userZones.length === 0) && (
        <ZoneSetup
          key={bootstrap ? 'bootstrap' : 'draft'}
          crewId={crewId}
          zones={listing.data.zones}
          tree={listing.data.tree?.tree ?? null}
          bootstrap={bootstrap}
          canAct={access.canAct}
          why={access.why}
          api={zapi}
          runner={runner}
          onChanged={refreshAll}
        />
      )}

      <Card labelledBy="zone-tree-title">
        <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-rule px-4 py-3 sm:px-5">
          <h3 id="zone-tree-title" className="font-display text-lg font-bold text-ink">
            Repository
          </h3>
          <p className="font-mono text-[11px] text-ink-3">
            {listing.data?.tree
              ? `tree snapshot · ${listing.data.tree.node_count ?? '?'} folders${listing.data.tree.captured_at ? ` · ${new Date(listing.data.tree.captured_at).toLocaleString()}` : ''}`
              : listing.loading
                ? 'loading tree…'
                : 'no tree snapshot yet: folders shown from zone globs'}
          </p>
        </div>
        <ul className="flex flex-wrap gap-x-4 gap-y-1 border-b border-dashed border-rule px-4 py-2 sm:px-5" aria-label="Legend">
          {LEGEND.map((p) => (
            <li key={p} className="cz-state">
              <PixelGlyph name={p} size={10} />
              {PRIMARY_LABEL[p]}
            </li>
          ))}
          <li className="cz-state" data-tone="signal">
            <PixelGlyph name="pending" size={10} /> policy change pending
          </li>
        </ul>
        {tree && (
          <ZoneTree
            state={state}
            root={tree}
            statuses={statuses}
            expanded={expanded}
            onToggle={toggle}
            selectedZoneId={selected?.id ?? null}
            onOpenZone={open}
            flashZoneIds={flash}
          />
        )}
        {builtin.map((z) => {
          return (
            <button
              key={z.id}
              type="button"
              onClick={() => open(z.id)}
              className="flex w-full flex-wrap items-baseline gap-x-2 gap-y-1 border-t border-dashed border-rule px-4 py-3 text-left font-mono text-[12.5px] hover:bg-[var(--cz-row-hover)] sm:px-5"
            >
              <PixelGlyph name="policy" size={11} />
              <span className="cz-chip" data-mode="exclusive">
                {z.slug}
              </span>
              <span className="font-sans text-ink-2">always protected: agents are denied here in every mode</span>
              <span className="w-full text-[11px] text-ink-3">{z.include_globs.join(' · ')}</span>
            </button>
          );
        })}
      </Card>

      {selected && statuses.get(selected.id) && (
        <ZoneDrawer
          crewId={crewId}
          state={state}
          zone={selected}
          detail={listing.data?.zones.find((z) => z.id === selected.id)}
          status={statuses.get(selected.id)!}
          enforcement={enforcement}
          events={tail.events}
          historyFromSeq={tail.fromSeq}
          now={nowMs}
          access={access}
          crewApi={runtime.api}
          zonesApi={zapi}
          runner={runner}
          onClose={close}
          onChanged={refreshAll}
        />
      )}
      {runner.prompt && <StepUpDialog prompt={runner.prompt} />}
    </div>
  );
}
