// The zone drawer (spec §9.5): the zone's definition and enforcement, the
// holder card with epoch and lease, waiters and near-misses, the last 20 claim
// events, and the human actions (Grant, Transfer, Revoke, Hold, Freeze /
// Unfreeze, Edit or Export patch). Every action asks for a reason; the ones
// that need a fresh login prompt for it (step-up) and retry.
//
// Zone titles, descriptions, task titles and freeze notes are untrusted text:
// rendered as plain text only.

import { useId, useState, type ReactNode } from 'react';
import { toast } from 'sonner';
import { Check, Copy, Lock, Snowflake, X } from 'lucide-react';
import type { CrewApi } from '../../../lib/crew/api';
import { callsignOf, isSubAgent, liveSessions, presenceText, sessionLabel, taskRef } from '../../../lib/crew/selectors';
import type { ClaimView, CrewEvent, CrewState, EnforcementLevel, ZoneView } from '../../../lib/crew/types';
import { absoluteTime, relativeTime } from '../../../lib/time';
import { useCopy } from '../../../hooks/useCopy';
import { AgentAvatar } from '../../relay/ui';
import type { HumanActionRunner } from '../policy/useHumanAction';
import { actionErrorText } from '../policy/stepUp';
import { StepUpCancelled } from '../policy/useHumanAction';
import { zoneClaimEvents, zoneNearMisses } from './eventTail';
import { performZoneAction, type ZoneAction, type ZoneActionInput } from './zoneActions';
import { Modal } from './Modal';
import { PixelGlyph } from './PixelGlyph';
import { ZoneActionConfirm, type ZoneActionSpec } from './ZoneActionConfirm';
import { StateMark } from './ZoneTree';
import type { ZonePatch, ZonesApi } from './zonesApi';
import { clockTime, enforcementLayers, leaseText, rowHolderText, type ZoneDetail, type ZoneStatus } from './zoneModel';

export interface DrawerAccess {
  /** A dashboard login with crew role owner or admin: may act (D27). */
  canAct: boolean;
  /** Why not, when it may not. */
  why: string | null;
}

type Action = ZoneAction;

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="cz-sect px-5 py-4">
      <h3 className="rr-eyebrow">{title}</h3>
      <div className="mt-2">{children}</div>
    </section>
  );
}

function Globs({ items, empty = '—' }: { items: readonly string[]; empty?: string }) {
  if (!items.length) return <span className="text-ink-3">{empty}</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((g) => (
        <code key={g} className="cz-glob">
          {g}
        </code>
      ))}
    </span>
  );
}

function HolderCard({ state, claim, now }: { state: CrewState; claim: ClaimView; now: number }) {
  const session = claim.holder_session_id ? state.sessions[claim.holder_session_id] : undefined;
  const task = claim.task_id ? state.tasks[claim.task_id] : undefined;
  const lease = leaseText(claim, now);
  const layers = session ? enforcementLayers(session) : null;
  if (claim.holder_kind === 'human') {
    return (
      <div className="rounded-[3px] border border-rule p-3 text-sm">
        <p className="font-semibold text-ink">A human holds this zone</p>
        <p className="mt-1 font-mono text-[11.5px] text-ink-2">
          {claim.mode} · epoch {claim.epoch}
          {claim.granted_at ? ` · since ${clockTime(claim.granted_at)}` : ''}
        </p>
      </div>
    );
  }
  return (
    <div className="rounded-[3px] border border-rule-strong p-3 text-sm">
      <div className="flex items-start gap-3">
        <AgentAvatar agentId={session?.agent_id ?? claim.holder_agent_id} />
        <div className="min-w-0 flex-1">
          <p className="font-mono text-[13px] font-semibold text-ink">
            {session ? sessionLabel(session) : (claim.holder_agent_id ?? claim.holder_session_id)}
            {session?.parent_session_id ? ` of ${callsignOf(state, session.parent_session_id)}` : ''}
          </p>
          <p className="font-mono text-[11.5px] text-ink-2">
            {session ? presenceText(session) : 'session ended'} · {claim.mode}
            {claim.state === 'offered' ? ` · handover offered to ${callsignOf(state, claim.offered_to) ?? 'a session'}` : ''}
          </p>
        </div>
      </div>
      <dl className="cz-kv mt-3">
        <dt>epoch</dt>
        <dd className="font-mono">{claim.epoch}</dd>
        <dt>lease</dt>
        <dd className="font-mono">
          {claim.fenced ? (
            <span className="text-signal-ink">lease unconfirmed · reconnecting (fenced)</span>
          ) : (
            (lease ?? '—')
          )}
          {claim.unconfirmed && <span className="ml-2 text-signal-ink">unconfirmed claim</span>}
        </dd>
        <dt>since</dt>
        <dd className="font-mono" title={absoluteTime(claim.granted_at)}>
          {claim.granted_at ? `${clockTime(claim.granted_at)} (${relativeTime(claim.granted_at, new Date(now))})` : '—'}
        </dd>
        <dt>task</dt>
        <dd>
          {task ? (
            <>
              <span className="font-mono">T-{task.number}</span> <span className="text-ink-2">{task.title}</span>
            </>
          ) : (
            <span className="text-ink-3">{taskRef(state, claim.task_id) ?? 'no task (auto-claimed on first write)'}</span>
          )}
        </dd>
        {layers && (
          <>
            <dt>enforcement</dt>
            <dd className="font-mono text-[11.5px]">
              before write: {layers.beforeWrite} · commit {layers.commit} · push {layers.push}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}

function ReservedCard({ state, claim }: { state: CrewState; claim: ClaimView }) {
  const from = callsignOf(state, claim.holder_session_id);
  return (
    <div className="rounded-[3px] border border-dashed border-signal p-3 text-sm">
      <p className="flex items-center gap-2 font-semibold text-ink">
        <PixelGlyph name="reserved" size={12} /> Waiting for the next runner
      </p>
      <p className="mt-1 text-ink-2">
        Held until picked up or released{from ? `, handed off by ${from}` : ''}
        {claim.reserve_reason ? ` (${claim.reserve_reason.replace('_', ' ')})` : ''}.
      </p>
      <dl className="cz-kv mt-2">
        <dt>task</dt>
        <dd className="font-mono">{taskRef(state, claim.task_id) ?? '—'}</dd>
        <dt>reserved for</dt>
        <dd className="font-mono">{claim.reserved_for ? (callsignOf(state, claim.reserved_for) ?? claim.reserved_for) : 'the next pickup'}</dd>
        <dt>saved work</dt>
        <dd className="font-mono">{claim.baton_ref ?? 'none'}</dd>
        <dt>epoch</dt>
        <dd className="font-mono">{claim.epoch}</dd>
      </dl>
    </div>
  );
}

function EventLine({ event, now }: { event: CrewEvent; now: number }) {
  const tone = event.moment || event.type === 'claim.denied' ? 'signal' : event.severity === 'high' || event.severity === 'critical' ? 'fail' : undefined;
  return (
    <li data-tone={tone} className="py-1">
      <span className="font-mono text-[11.5px] text-ink">{event.summary}</span>
      <span className="ml-2 font-mono text-[10.5px] text-ink-3" title={absoluteTime(event.ts)}>
        {relativeTime(event.ts, new Date(now))} · #{event.seq}
      </span>
    </li>
  );
}

function PatchBlock({ patch, label }: { patch: string; label: string }) {
  const [copy, copied] = useCopy();
  return (
    <div className="mt-3">
      <div className="flex items-center justify-between">
        <p className="font-mono text-[11px] text-ink-3">{label}</p>
        <button type="button" className="rr-btn-ghost inline-flex items-center gap-1.5 px-2 py-1 text-xs" onClick={() => copy(patch, 'Patch copied')}>
          {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />} Copy
        </button>
      </div>
      <pre className="rr-cmd mt-1 max-h-72 overflow-auto rounded-[3px] p-3 font-mono text-[11.5px] leading-relaxed whitespace-pre">{patch}</pre>
    </div>
  );
}

function EditZone({
  zone,
  detail,
  api,
  runner,
  onDone,
}: {
  zone: ZoneView;
  detail: ZoneDetail | undefined;
  api: ZonesApi;
  runner: HumanActionRunner;
  onDone: () => void;
}) {
  const [title, setTitle] = useState(zone.title);
  const [description, setDescription] = useState(detail?.description ?? '');
  const [mode, setMode] = useState(zone.mode);
  const [flags, setFlags] = useState({ protected: zone.protected, fail_closed: zone.fail_closed, auto_claim: zone.auto_claim });
  const [include, setInclude] = useState(zone.include_globs.join('\n'));
  const [exclude, setExclude] = useState(zone.exclude_globs.join('\n'));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [patch, setPatch] = useState<string | null>(null);
  const lines = (s: string) => s.split('\n').map((l) => l.trim()).filter(Boolean);
  const same = (a: string[], b: readonly string[]) => a.length === b.length && a.every((v, i) => v === b[i]);

  const body: ZonePatch = {};
  if (title.trim() !== zone.title) body.title = title.trim();
  if ((description.trim() || null) !== (detail?.description ?? null)) body.description = description.trim() || null;
  if (mode !== zone.mode) body.mode = mode;
  if (flags.protected !== zone.protected) body.protected = flags.protected;
  if (flags.fail_closed !== zone.fail_closed) body.fail_closed = flags.fail_closed;
  if (flags.auto_claim !== zone.auto_claim) body.auto_claim = flags.auto_claim;
  if (!same(lines(include), zone.include_globs)) body.include = lines(include);
  if (!same(lines(exclude), zone.exclude_globs)) body.exclude = lines(exclude);
  const changed = Object.keys(body).length > 0;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await runner.run('Editing a zone', () => api.patchZone(zone.id, body, zone.version));
      if (res.applied) {
        toast.success(`Zone ${zone.slug} saved`);
        onDone();
      } else {
        setPatch(res.export_patch);
      }
    } catch (err) {
      setError(err instanceof StepUpCancelled ? 'Not saved: sign-in was cancelled.' : actionErrorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (patch !== null) {
    return (
      <div>
        <p className="text-sm text-ink-2">
          <span className="font-mono">{zone.slug}</span> is declared in <span className="font-mono">.remembra/zones.yml</span>, which is the authority for it. Commit this patch on the default branch; crewd uploads it. A loosening change then waits for your approval here.
        </p>
        <PatchBlock patch={patch || '# no change'} label="git apply" />
        <button type="button" className="rr-btn-ghost mt-3 px-3 py-1.5 text-sm" onClick={onDone}>
          Done
        </button>
      </div>
    );
  }

  const check = (key: keyof typeof flags, label: string, hint: string) => (
    <label className="flex items-start gap-2 text-sm">
      <input type="checkbox" className="mt-1" checked={flags[key]} onChange={(e) => setFlags((f) => ({ ...f, [key]: e.target.checked }))} />
      <span>
        <span className="text-ink">{label}</span> <span className="text-ink-3">{hint}</span>
      </span>
    </label>
  );

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (changed && !busy) void save();
      }}
      className="space-y-3"
    >
      <label className="block text-sm">
        <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Title</span>
        <input className="rr-input mt-1 w-full px-2 py-1.5" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="block text-sm">
        <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Description</span>
        <textarea className="rr-input mt-1 w-full px-2 py-1.5" rows={2} maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label className="block text-sm">
        <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Mode</span>
        <select className="rr-input mt-1 w-full px-2 py-1.5 font-mono text-[13px]" value={mode} onChange={(e) => setMode(e.target.value as ZoneView['mode'])}>
          <option value="exclusive">exclusive: one agent at a time</option>
          <option value="shared">shared: several agents, overlaps reported</option>
          <option value="watch">watch: never blocks, holders are told</option>
        </select>
      </label>
      {check('protected', 'Protected', 'agents are denied unless you grant it')}
      {check('fail_closed', 'Fail closed', 'deny even when the server cannot be reached')}
      {check('auto_claim', 'Auto-claim', 'the first write claims it (leaf zones)')}
      {/* Glob editing is desktop only (§9.12). */}
      <div className="hidden space-y-3 sm:block">
        <label className="block text-sm">
          <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Include globs (one per line)</span>
          <textarea className="rr-input mt-1 w-full px-2 py-1.5 font-mono text-[12px]" rows={3} value={include} onChange={(e) => setInclude(e.target.value)} />
        </label>
        <label className="block text-sm">
          <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Exclude globs</span>
          <textarea className="rr-input mt-1 w-full px-2 py-1.5 font-mono text-[12px]" rows={2} value={exclude} onChange={(e) => setExclude(e.target.value)} />
        </label>
      </div>
      {error && (
        <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button type="submit" disabled={!changed || busy} className="rr-btn-primary px-3 py-1.5 text-sm">
          {zone.source === 'repo' ? 'Make the patch' : 'Save'}
        </button>
        <button type="button" className="rr-btn-ghost px-3 py-1.5 text-sm" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function ZoneDrawer({
  crewId,
  state,
  zone,
  detail,
  status,
  enforcement,
  events,
  historyFromSeq,
  now,
  access,
  crewApi,
  zonesApi,
  runner,
  onClose,
  onChanged,
}: {
  crewId: string;
  state: CrewState;
  zone: ZoneView;
  detail: ZoneDetail | undefined;
  status: ZoneStatus;
  enforcement: EnforcementLevel;
  events: readonly CrewEvent[];
  historyFromSeq: number;
  now: number;
  access: DrawerAccess;
  crewApi: CrewApi;
  zonesApi: ZonesApi;
  runner: HumanActionRunner;
  onClose: () => void;
  onChanged: () => void;
}) {
  const id = useId();
  const [action, setAction] = useState<Action | null>(null);
  const [editing, setEditing] = useState(false);
  const [exported, setExported] = useState<string | null>(null);
  const holder = status.holders.find((c) => c.holder_kind === 'session' || c.holder_kind === 'human') ?? null;
  const live = liveSessions(state).filter((s) => s.state !== 'paused');
  const claimEvents = zoneClaimEvents(events, zone.id);
  const nearMisses = zoneNearMisses(events, zone);
  const target: ClaimView | null = holder ?? status.reserved;

  const specs: Record<Action, ZoneActionSpec> = {
    grant: {
      title: `Grant ${zone.slug} to…`,
      consequence: zone.protected
        ? `${zone.slug} is protected: only a person can hand it out. The session holds it exclusively until it releases it.`
        : `The session holds ${zone.slug} exclusively until it releases it; everyone else is denied.`,
      confirmLabel: 'Grant',
      sessions: live,
    },
    transfer: {
      title: status.reserved && !holder ? `Hand baton to…` : `Transfer ${zone.slug} to…`,
      consequence: 'The zone (and its saved work, if any) moves to this session with a new epoch. The current holder is told at its next tool call.',
      confirmLabel: 'Transfer',
      // a baton goes to a top-level session (sub-agents work for their parent); a zone can go to anyone
      sessions: live.filter((s) => s.id !== target?.holder_session_id && !(status.reserved && !holder && isSubAgent(s))),
    },
    revoke: {
      title: `Revoke ${zone.slug}`,
      consequence: 'The claim ends now. The zone is free for the next agent that needs it.',
      warning:
        holder && holder.holder_kind === 'session' && holder.state === 'active'
          ? `${callsignOf(state, holder.holder_session_id)} is working in this zone: it will be stopped at its next tool call.`
          : null,
      confirmLabel: 'Revoke',
      danger: true,
    },
    hold: {
      title: `Put ${zone.slug} on hold`,
      consequence: 'The claim is reserved (human hold): nobody writes here until you hand it over or release it.',
      warning: holder?.state === 'active' ? 'The holder is stopped at its next tool call.' : null,
      confirmLabel: 'Hold',
    },
    freeze: {
      title: `Freeze ${zone.slug}`,
      consequence: 'Every agent is denied in this zone until you unfreeze it ("Mani is editing it himself"). Holders are told at their next tool call.',
      confirmLabel: 'Freeze',
      until: true,
    },
    unfreeze: {
      title: `Unfreeze ${zone.slug}`,
      consequence: 'Agents can claim the zone again; the first one waiting gets it.',
      confirmLabel: 'Unfreeze',
    },
  };

  const perform = async (a: Action, input: ZoneActionInput) => {
    await performZoneAction(a, { crewId, zone, target, crewApi, zonesApi, run: runner.run }, input);
    toast.success(`${specs[a].confirmLabel}: ${zone.slug}`);
    onChanged();
  };

  const exportPatch = async () => {
    try {
      const res = await zonesApi.exportZones(crewId);
      setExported(res.yaml);
    } catch (err) {
      toast.error(actionErrorText(err));
    }
  };

  const btn = 'rr-btn-ghost px-2.5 py-1.5 text-xs font-mono';
  const buttonLabel = (a: Action) => {
    if (a === 'transfer') return status.reserved && !holder ? 'Hand baton to…' : 'Transfer…';
    if (a === 'grant') return 'Grant…';
    return specs[a].confirmLabel;
  };
  const buttons: { a: Action; show: boolean }[] = [
    { a: 'grant', show: !holder && !status.reserved && !status.frozen },
    { a: 'transfer', show: !!target && target.holder_kind === 'session' },
    { a: 'hold', show: !!holder && holder.holder_kind === 'session' && holder.state === 'active' },
    { a: 'revoke', show: !!target && !(status.frozen && target.holder_kind === 'human') },
    { a: 'freeze', show: !status.frozen },
    { a: 'unfreeze', show: status.frozen },
  ];

  return (
    <Modal labelledBy={`${id}-t`} onClose={onClose} className="cz-drawer" initialFocus="[data-close]">
      <div className="flex items-start gap-3 border-b border-rule px-5 py-4">
        <div className="min-w-0 flex-1">
          <p className="rr-eyebrow">Zone</p>
          <h2 id={`${id}-t`} className="font-display mt-1 flex flex-wrap items-baseline gap-2 text-2xl font-extrabold text-ink">
            <span className="font-mono text-[20px]">{zone.slug}</span>
            <span className="text-base font-semibold text-ink-2">{zone.title}</span>
          </h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StateMark status={status} />
            {zone.protected && (
              <span className="cz-state">
                <Lock className="h-3 w-3" aria-hidden="true" /> protected
              </span>
            )}
            {zone.fail_closed && <span className="cz-state">fail closed</span>}
          </div>
          <p className="mt-2 font-mono text-[11.5px] text-ink-2">{rowHolderText(state, zone, status)}</p>
        </div>
        <button type="button" data-close className="rr-btn-ghost p-1.5" aria-label="Close zone drawer" onClick={onClose}>
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {zone.builtin ? (
          <Section title="Built-in">
            <p className="text-sm text-ink-2">
              The crew-policy zone is always on and always protected (D28). Agents are denied here in every mode; changes to it go through you. It
              covers the zones file, git hooks and the local crew runtime, some of which live outside the repository.
            </p>
          </Section>
        ) : (
          <Section title="Who holds it">
            <div className="space-y-2">
              {status.frozen && (
                <div className="rounded-[3px] border border-rule-strong p-3 text-sm">
                  <p className="flex items-center gap-2 font-semibold text-ink">
                    <Snowflake className="h-4 w-4" aria-hidden="true" /> Frozen by a human
                  </p>
                  {zone.frozen_note && <p className="mt-1 text-ink-2">{zone.frozen_note}</p>}
                  <p className="mt-1 font-mono text-[11.5px] text-ink-3">{zone.frozen_until ? `until ${absoluteTime(zone.frozen_until)}` : 'until unfrozen'}</p>
                </div>
              )}
              {status.holders.map((c) => (
                <HolderCard key={c.id} state={state} claim={c} now={now} />
              ))}
              {status.reserved && <ReservedCard state={state} claim={status.reserved} />}
              {!status.holders.length && !status.reserved && !status.frozen && (
                <p className="text-sm text-ink-2">
                  Free. {zone.protected ? 'Protected: an agent gets it only when you grant it.' : zone.auto_claim ? 'The first agent to write here claims it.' : 'Agents claim it explicitly (auto-claim is off).'}
                </p>
              )}
              {status.breaches.map((b) => (
                <p key={b.id} className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
                  {b.kind.replace(/_/g, ' ')} ({b.attribution ?? 'unattributed'}): <span className="font-mono">{b.subject}</span>
                  {b.session_b ? ` by ${callsignOf(state, b.session_b)}` : ''}
                </p>
              ))}
            </div>
          </Section>
        )}

        {status.waiters.length > 0 && (
          <Section title={`Waiting (${status.waiters.length})`}>
            <ol className="space-y-1 font-mono text-[12px] text-ink-2">
              {status.waiters.map((c, i) => (
                <li key={c.id}>
                  {i + 1}. {callsignOf(state, c.holder_session_id) ?? c.holder_agent_id} · {c.mode}
                  {c.task_id ? ` · ${taskRef(state, c.task_id)}` : ''}
                </li>
              ))}
            </ol>
          </Section>
        )}

        {!zone.builtin && (
          <Section title="Human actions">
            {!access.canAct ? (
              <p className="text-sm text-ink-3">{access.why}</p>
            ) : editing ? (
              <EditZone zone={zone} detail={detail} api={zonesApi} runner={runner} onDone={() => setEditing(false)} />
            ) : (
              <>
                <div className="flex flex-wrap gap-1.5">
                  {buttons
                    .filter((b) => b.show)
                    .map((b) => (
                      <button key={b.a} type="button" className={btn} onClick={() => setAction(b.a)}>
                        {buttonLabel(b.a)}
                      </button>
                    ))}
                  <button type="button" className={btn} onClick={() => setEditing(true)}>
                    {zone.source === 'repo' ? 'Edit (patch)…' : 'Edit…'}
                  </button>
                  <button type="button" className={btn} onClick={() => void exportPatch()}>
                    Export zones.yml
                  </button>
                </div>
                {exported !== null && <PatchBlock patch={exported} label=".remembra/zones.yml (every live zone)" />}
              </>
            )}
          </Section>
        )}

        <Section title="Definition">
          <dl className="cz-kv">
            <dt>include</dt>
            <dd>
              <Globs items={zone.include_globs} />
            </dd>
            <dt>exclude</dt>
            <dd>
              <Globs items={zone.exclude_globs} />
            </dd>
            <dt>commands</dt>
            <dd>
              <Globs items={zone.command_patterns} empty="none" />
            </dd>
            <dt>MCP tools</dt>
            <dd>
              <Globs items={zone.mcp_tools.map((t) => (t.service ? `${t.tool} → ${t.service}` : t.tool))} empty="none" />
            </dd>
            <dt>services</dt>
            <dd>
              <Globs items={zone.services} empty="none" />
            </dd>
            <dt>mode</dt>
            <dd className="font-mono">{zone.mode}</dd>
            <dt>auto-claim</dt>
            <dd className="font-mono">{zone.auto_claim ? (zone.is_leaf ? 'on (first write)' : 'task only (has child zones)') : 'off'}</dd>
            {zone.reserve_for && (
              <>
                <dt>reserved for</dt>
                <dd className="font-mono">{zone.reserve_for} (key-verified only)</dd>
              </>
            )}
            <dt>source</dt>
            <dd className="font-mono">
              {zone.source === 'repo' ? '.remembra/zones.yml' : zone.source === 'suggested' ? 'temporary (from folders)' : zone.source}
              {detail?.description ? <span className="mt-1 block font-sans text-ink-2">{detail.description}</span> : null}
            </dd>
            <dt>enforcement</dt>
            <dd className="font-mono text-[11.5px]">
              project: {enforcement}
              {enforcement === 'enforce' ? ' · denied before the write (hooked agents), at commit and at push' : enforcement === 'observe' ? ' · logged, not denied' : ' · gate off'}
            </dd>
          </dl>
        </Section>

        <Section title={`Near-misses (${nearMisses.length})`}>
          {nearMisses.length ? (
            <ul className="cz-trail">
              {nearMisses.map((e) => (
                <EventLine key={e.seq} event={e} now={now} />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-3">No agent has been blocked here recently.</p>
          )}
        </Section>

        <Section title="Last claim events">
          {claimEvents.length ? (
            <ul className="cz-trail">
              {claimEvents.map((e) => (
                <EventLine key={e.seq} event={e} now={now} />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-3">No claims on this zone recently.</p>
          )}
          <p className="mt-2 font-mono text-[10.5px] text-ink-3">from event #{historyFromSeq} on</p>
        </Section>
      </div>

      {action && <ZoneActionConfirm spec={specs[action]} onConfirm={(input) => perform(action, input)} onClose={() => setAction(null)} />}
    </Modal>
  );
}

