// Zone changes waiting for a person (D9): a zones.yml upload that loosens
// protection (removes a zone, lowers enforcement or a mode, drops protected /
// fail_closed / reserve_for, narrows a zone with a live claim) is held back
// until a human approves it here. Until then the old policy stays in force.
// Shows the diff item by item and who uploaded it.

import { useState } from 'react';
import { toast } from 'sonner';
import type { CrewApi } from '../../../lib/crew/api';
import { callsignOf } from '../../../lib/crew/selectors';
import type { CrewState } from '../../../lib/crew/types';
import { absoluteTime, relativeTime, shortSha } from '../../../lib/time';
import type { HumanActionRunner } from '../policy/useHumanAction';
import { PixelGlyph } from './PixelGlyph';
import { ZoneActionConfirm } from './ZoneActionConfirm';
import type { DiffItem, ZoneChange } from './zoneModel';

function itemText(item: DiffItem): string {
  if (item.target === 'enforcement') return `enforcement ${item.reason ?? 'changed'}`;
  const [kind, ...rest] = item.target.split(':');
  const name = rest.join(':');
  const what = kind === 'zone' ? `zone ${name}` : `${kind} ${name}`;
  if (item.op === 'add') return `add ${what}`;
  if (item.op === 'remove') return `remove ${what}`;
  return `${what}: ${item.field ?? 'changed'}${item.reason ? ` (${item.reason})` : ''}`;
}

function uploaderText(state: CrewState | null, change: ZoneChange): string {
  if (change.uploaded_by_session) return `uploaded by ${state ? (callsignOf(state, change.uploaded_by_session) ?? 'a session') : 'a session'}`;
  if (change.uploaded_by_user) return 'saved by a human';
  return 'uploaded';
}

function ChangeCard({
  state,
  change,
  now,
  canAct,
  why,
  onDecide,
}: {
  state: CrewState | null;
  change: ZoneChange;
  now: Date;
  canAct: boolean;
  why: string | null;
  onDecide: (change: ZoneChange, approve: boolean) => void;
}) {
  const loose = change.items.filter((i) => i.loosening);
  const rest = change.items.filter((i) => !i.loosening);
  return (
    <article className="rounded-[3px] border border-signal bg-panel p-3 shadow-[3px_3px_0_var(--signal)] sm:p-4" aria-label={`Pending zone change ${change.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <PixelGlyph name="pending" size={12} />
        <span className="font-mono text-[12px] font-semibold text-ink">zones.yml @ {shortSha(change.yaml_sha) || change.yaml_sha.slice(0, 8)}</span>
        <span className="font-mono text-[11px] text-ink-3" title={absoluteTime(change.created_at)}>
          {uploaderText(state, change)} · {relativeTime(change.created_at, now)}
        </span>
        {change.loosening && <span className="cz-state" data-tone="signal">loosens protection</span>}
      </div>
      {change.summary && <p className="mt-2 font-mono text-[12px] text-ink-2">{change.summary}</p>}
      <ul className="mt-2 space-y-0.5 font-mono text-[12px]">
        {loose.map((i, n) => (
          <li key={`l${n}`} className="text-signal-ink">
            <span aria-hidden="true">▲ </span>
            {itemText(i)} <span className="text-ink-3">(held back)</span>
          </li>
        ))}
        {rest.map((i, n) => (
          <li key={`r${n}`} className="text-ink-2">
            <span aria-hidden="true">· </span>
            {itemText(i)} <span className="text-ink-3">(applied already)</span>
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {canAct ? (
          <>
            <button type="button" className="rr-btn-primary px-3 py-1.5 text-sm" onClick={() => onDecide(change, true)}>
              Approve
            </button>
            <button type="button" className="rr-btn-ghost px-3 py-1.5 text-sm" onClick={() => onDecide(change, false)}>
              Reject
            </button>
          </>
        ) : (
          <span className="text-sm text-ink-3">{why}</span>
        )}
      </div>
    </article>
  );
}

export function PendingZoneChanges({
  state,
  changes,
  now,
  canAct,
  why,
  crewApi,
  runner,
  onChanged,
  heading = true,
}: {
  state: CrewState | null;
  changes: readonly ZoneChange[];
  now: Date;
  canAct: boolean;
  why: string | null;
  crewApi: CrewApi;
  runner: HumanActionRunner;
  onChanged: () => void;
  heading?: boolean;
}) {
  const [deciding, setDeciding] = useState<{ change: ZoneChange; approve: boolean } | null>(null);
  const pending = changes.filter((c) => c.state === 'pending');
  if (!pending.length) return null;
  return (
    <section aria-label="Zone changes waiting for approval" className="space-y-2">
      {heading && (
        <p className="rr-eyebrow">
          {pending.length} zone change{pending.length === 1 ? '' : 's'} waiting for you · the old policy stays in force until you decide
        </p>
      )}
      {pending.map((c) => (
        <ChangeCard key={c.id} state={state} change={c} now={now} canAct={canAct} why={why} onDecide={(change, approve) => setDeciding({ change, approve })} />
      ))}
      {deciding && (
        <ZoneActionConfirm
          spec={{
            title: deciding.approve ? 'Approve this zone change' : 'Reject this zone change',
            consequence: deciding.approve
              ? `The uploaded zones.yml applies in full, including: ${deciding.change.summary ?? 'the listed changes'}. Agents see the new zones at their next check.`
              : 'The held-back items are dropped and the current policy stays. Fix zones.yml in the repository to try again.',
            warning: deciding.approve && deciding.change.loosening ? 'This loosens protection: agents may be allowed where they were denied.' : null,
            confirmLabel: deciding.approve ? 'Approve' : 'Reject',
            reason: false,
          }}
          onConfirm={async () => {
            const { change, approve } = deciding;
            await runner.run(approve ? 'Approving a zone change' : 'Rejecting a zone change', () =>
              approve ? crewApi.approveZoneChange(change.id) : crewApi.rejectZoneChange(change.id),
            );
            toast.success(approve ? 'Zone change approved' : 'Zone change rejected');
            onChanged();
          }}
          onClose={() => setDeciding(null)}
        />
      )}
    </section>
  );
}
