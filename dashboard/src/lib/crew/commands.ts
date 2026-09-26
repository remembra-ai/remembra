// Crew commands for the command palette (spec §9.1): "Go to crew…", "Who
// holds…", "Freeze zone…", "Pause agent…", "Request checkpoint…", "Post to
// crew…", "Issue bypass code…".
//
// Each command is a small flow: pick the crew (skipped when the current page
// is a crew), then the zone or session, then any text (every human action
// asks for a reason, §9.5), then run it against the API. The flow is plain
// data plus pure step functions, so the palette only renders the current step
// and the behaviour is tested without a browser.

import { absoluteTime } from '../time';
import { CrewApiError, type CrewApi } from './api';
import { crewHref } from './routes';
import { describeHolder, liveSessions, presenceText, sessionLabel, sortedZones } from './selectors';
import type { CrewListItem, CrewState } from './types';

export type CrewCommandId = 'go' | 'who-holds' | 'freeze' | 'pause' | 'checkpoint' | 'post' | 'bypass';

export interface CrewCommandDef {
  id: CrewCommandId;
  label: string;
  description: string;
  keywords: string[];
  /** Needs a dashboard login (D27): hidden when signed in with an API key. */
  humanOnly: boolean;
}

export const CREW_COMMANDS: readonly CrewCommandDef[] = [
  { id: 'go', label: 'Go to crew…', description: "Open a project's Mission Control", keywords: ['crew', 'project', 'mission', 'track', 'lanes'], humanOnly: false },
  { id: 'who-holds', label: 'Who holds…', description: 'Which agent holds a zone, and for which task', keywords: ['zone', 'claim', 'holder', 'lock', 'who'], humanOnly: false },
  { id: 'freeze', label: 'Freeze zone…', description: 'Stop every agent editing a zone', keywords: ['zone', 'lock', 'hold', 'stop'], humanOnly: true },
  { id: 'pause', label: 'Pause agent…', description: 'Its next write is denied until you resume it', keywords: ['agent', 'session', 'stop', 'halt'], humanOnly: true },
  { id: 'checkpoint', label: 'Request checkpoint…', description: 'Ask an agent to record where it is', keywords: ['agent', 'session', 'save', 'status'], humanOnly: true },
  { id: 'post', label: 'Post to crew…', description: 'A message every agent sees at its next turn', keywords: ['message', 'channel', 'say', 'chat'], humanOnly: false },
  { id: 'bypass', label: 'Issue bypass code…', description: 'A single-use code that lets one agent pass the gate', keywords: ['override', 'gate', 'unblock', 'code'], humanOnly: true },
];

export function crewCommand(id: CrewCommandId): CrewCommandDef {
  const def = CREW_COMMANDS.find((c) => c.id === id);
  if (!def) throw new Error(`unknown crew command ${id}`);
  return def;
}

export interface FlowCrew {
  crewId: string;
  project: string;
}

export interface CrewFlow {
  command: CrewCommandId;
  crew: FlowCrew | null;
  zoneId: string | null;
  sessionId: string | null;
  scope: string | null;
  minutes: number | null;
  text: string | null;
}

export interface PickOption {
  value: string;
  label: string;
  description?: string;
  /** Why it cannot be chosen (shown instead of acting). */
  disabled?: string;
}

export type FlowStep =
  | { kind: 'pick'; field: 'crew' | 'zone' | 'session' | 'scope' | 'minutes'; title: string; options: PickOption[]; empty: string }
  | { kind: 'text'; field: 'text'; title: string; placeholder: string; maxLength: number }
  /** The chosen crew's state is still loading. */
  | { kind: 'loading'; title: string }
  | { kind: 'ready' };

export interface FlowContext {
  crews: CrewListItem[];
  /** Live state of a crew (null while it loads). */
  stateOf: (crewId: string) => CrewState | null;
}

export interface FlowResult {
  message: string;
  /** Navigate here after the action (a hash href). */
  href?: string;
  /** A value to show once with a copy button (the bypass code). */
  copy?: string;
}

export const REASON_MAX = 280;
export const MESSAGE_MAX = 8192;
export const BYPASS_MINUTES = [5, 10, 15] as const;

export function startFlow(command: CrewCommandId, current: FlowCrew | null = null): CrewFlow {
  return { command, crew: current, zoneId: null, sessionId: null, scope: null, minutes: null, text: null };
}

function crewOptions(crews: CrewListItem[]): PickOption[] {
  return crews.map((item) => ({
    value: item.crew.id,
    label: item.crew.name || item.crew.project_id,
    description: `${item.crew.project_id} · ${item.live} live · ${item.needs_you} need${item.needs_you === 1 ? 's' : ''} you`,
  }));
}

function zoneOptions(state: CrewState, command: CrewCommandId): PickOption[] {
  return sortedZones(state).map((zone) => {
    let disabled: string | undefined;
    if (command === 'freeze') {
      if (zone.builtin) disabled = 'built-in policy zone: always protected';
      else if (zone.frozen_by) disabled = 'already frozen';
    }
    return { value: zone.id, label: zone.slug, description: describeHolder(state, zone), disabled };
  });
}

function sessionOptions(state: CrewState, command: CrewCommandId): PickOption[] {
  return liveSessions(state).map((session) => ({
    value: session.id,
    label: sessionLabel(session),
    description: presenceText(session),
    disabled: command === 'pause' && session.state === 'paused' ? 'already paused' : undefined,
  }));
}

function scopeOptions(state: CrewState): PickOption[] {
  const options: PickOption[] = [
    { value: 'commit', label: 'commit', description: 'one commit past the commit gate' },
    { value: 'push', label: 'push', description: 'one push past the pre-push gate' },
  ];
  for (const zone of sortedZones(state)) {
    if (zone.builtin) continue;
    options.push({ value: `write:${zone.slug}`, label: `write:${zone.slug}`, description: `edits in zone ${zone.slug}` });
  }
  return options;
}

/** The next input the flow needs, or `ready`. */
export function nextStep(flow: CrewFlow, ctx: FlowContext): FlowStep {
  const def = crewCommand(flow.command);
  if (!flow.crew) {
    return { kind: 'pick', field: 'crew', title: def.label.replace('…', ''), options: crewOptions(ctx.crews), empty: 'No crews yet.' };
  }
  if (flow.command === 'go') return { kind: 'ready' };
  if (flow.command === 'post') {
    if (flow.text === null) {
      return { kind: 'text', field: 'text', title: `Post to ${flow.crew.project}`, placeholder: 'Message for every agent on this crew…', maxLength: MESSAGE_MAX };
    }
    return { kind: 'ready' };
  }
  const state = ctx.stateOf(flow.crew.crewId);
  if (!state) return { kind: 'loading', title: `Loading ${flow.crew.project}…` };

  const needsZone = flow.command === 'who-holds' || flow.command === 'freeze';
  const needsSession = flow.command === 'pause' || flow.command === 'checkpoint' || flow.command === 'bypass';
  if (needsZone && flow.zoneId === null) {
    return {
      kind: 'pick',
      field: 'zone',
      title: flow.command === 'freeze' ? 'Freeze which zone?' : 'Who holds which zone?',
      options: zoneOptions(state, flow.command),
      empty: 'This crew has no zones yet.',
    };
  }
  if (needsSession && flow.sessionId === null) {
    const title = flow.command === 'pause' ? 'Pause which agent?' : flow.command === 'checkpoint' ? 'Checkpoint which agent?' : 'Code for which agent?';
    return { kind: 'pick', field: 'session', title, options: sessionOptions(state, flow.command), empty: 'No live agents on this crew.' };
  }
  if (flow.command === 'bypass') {
    if (flow.scope === null) return { kind: 'pick', field: 'scope', title: 'What may it pass?', options: scopeOptions(state), empty: '' };
    if (flow.minutes === null) {
      return {
        kind: 'pick',
        field: 'minutes',
        title: 'Valid for',
        options: BYPASS_MINUTES.map((m) => ({ value: String(m), label: `${m} minutes`, description: 'single use' })),
        empty: '',
      };
    }
    return { kind: 'ready' };
  }
  if (flow.command === 'who-holds') return { kind: 'ready' };
  if (flow.text === null) {
    const verb = flow.command === 'freeze' ? 'freezing' : flow.command === 'pause' ? 'pausing' : 'the checkpoint';
    return { kind: 'text', field: 'text', title: `Reason for ${verb}`, placeholder: 'Reason (recorded in the audit trail)…', maxLength: REASON_MAX };
  }
  return { kind: 'ready' };
}

/** Why a typed value cannot be submitted, or null when it can. */
export function validateText(step: Extract<FlowStep, { kind: 'text' }>, value: string): string | null {
  const text = value.trim();
  if (!text) return step.maxLength === REASON_MAX ? 'Give a reason.' : 'Write a message.';
  if (new TextEncoder().encode(text).length > step.maxLength) return `Too long (max ${step.maxLength} characters).`;
  return null;
}

/** Record the answer to the current step. Disabled options and invalid text leave the flow unchanged. */
export function answer(flow: CrewFlow, ctx: FlowContext, value: string): CrewFlow {
  const step = nextStep(flow, ctx);
  if (step.kind === 'text') {
    if (validateText(step, value) !== null) return flow;
    return { ...flow, text: value.trim() };
  }
  if (step.kind !== 'pick') return flow;
  const option = step.options.find((o) => o.value === value);
  if (!option || option.disabled) return flow;
  switch (step.field) {
    case 'crew': {
      const item = ctx.crews.find((c) => c.crew.id === value);
      return item ? { ...flow, crew: { crewId: item.crew.id, project: item.crew.project_id } } : flow;
    }
    case 'zone':
      return { ...flow, zoneId: value };
    case 'session':
      return { ...flow, sessionId: value };
    case 'scope':
      return { ...flow, scope: value };
    case 'minutes':
      return { ...flow, minutes: Number(value) };
  }
}

/** Step back one answer (Backspace on an empty field). */
export function back(flow: CrewFlow, pinnedCrew: boolean): CrewFlow | null {
  if (flow.text !== null) return { ...flow, text: null };
  if (flow.minutes !== null) return { ...flow, minutes: null };
  if (flow.scope !== null) return { ...flow, scope: null };
  if (flow.sessionId !== null) return { ...flow, sessionId: null };
  if (flow.zoneId !== null) return { ...flow, zoneId: null };
  if (flow.crew !== null && !pinnedCrew) return { ...flow, crew: null };
  return null;
}

function slugOf(state: CrewState | null, zoneId: string): string {
  return state?.zones[zoneId]?.slug ?? zoneId;
}

function callsign(state: CrewState | null, sessionId: string): string {
  return state?.sessions[sessionId]?.callsign ?? sessionId;
}

/** Run a ready flow. Throws CrewApiError when the server refuses (the palette shows its message). */
export async function runFlow(
  flow: CrewFlow,
  ctx: FlowContext,
  api: Pick<CrewApi, 'freezeZone' | 'pauseSession' | 'requestCheckpoint' | 'postMessage' | 'issueBypassCode'>,
): Promise<FlowResult> {
  if (nextStep(flow, ctx).kind !== 'ready' || !flow.crew) throw new Error('flow is not ready');
  const { crewId, project } = flow.crew;
  const state = ctx.stateOf(crewId);
  switch (flow.command) {
    case 'go':
      return { message: `Opening ${project}`, href: crewHref(project) };
    case 'who-holds': {
      const zone = state?.zones[flow.zoneId!];
      const slug = slugOf(state, flow.zoneId!);
      return {
        message: zone && state ? `${slug}: ${describeHolder(state, zone)}` : slug,
        href: crewHref(project, 'zones', { zone: slug }),
      };
    }
    case 'freeze':
      await api.freezeZone(flow.zoneId!, flow.text!);
      return { message: `Froze zone ${slugOf(state, flow.zoneId!)}. Agents are denied there until you unfreeze it.` };
    case 'pause':
      await api.pauseSession(flow.sessionId!, flow.text!);
      return { message: `Paused ${callsign(state, flow.sessionId!)}. Its next write is denied.` };
    case 'checkpoint':
      await api.requestCheckpoint(flow.sessionId!, flow.text!);
      return { message: `Asked ${callsign(state, flow.sessionId!)} for a checkpoint.` };
    case 'post':
      await api.postMessage(crewId, { kind: 'chat', body: flow.text! });
      return { message: `Posted to ${project}.`, href: crewHref(project, 'channel') };
    case 'bypass': {
      const res = await api.issueBypassCode(crewId, { session_id: flow.sessionId!, scope: flow.scope!, minutes: flow.minutes! });
      return {
        message: `Bypass code for ${callsign(state, flow.sessionId!)} (${res.scope}), single use, valid until ${absoluteTime(res.expires_at) || res.expires_at}.`,
        copy: res.code,
      };
    }
  }
}

/** A short, human message for a failed action. */
export function flowErrorMessage(err: unknown): string {
  if (err instanceof CrewApiError) {
    if (err.stepUpRequired) return 'Sign in again to confirm this action (a login within the last 15 minutes is required).';
    if (err.humanOnly) return 'This action needs a dashboard login; API keys cannot perform it.';
    if (err.status === 404) return 'Not found, or you no longer have access to it.';
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}
