// The Zone Map's read model (spec §9.5): the repository's folder tree with the
// crew's zones laid over it, and one status per zone (free, held, shared,
// reserved, contested, frozen, breach, plus "policy change pending").
//
// Pure functions over the reducer state and the zones listing; the screens
// only render what these return. Text is built from ids, slugs, callsigns and
// globs; titles are rendered by the screens as plain text (§9, untrusted text).

import { beforeWriteLabel, describeHolder, presenceText, taskRef } from '../../../lib/crew/selectors';
import type { ClaimView, CollisionView, CrewState, SessionState, ZoneView } from '../../../lib/crew/types';

/** A folder of the tree snapshot (`PUT /crews/{id}/tree`): names and file counts only (§11). */
export interface RepoTreeNode {
  name: string;
  files?: number | null;
  children?: RepoTreeNode[] | null;
}

/** `GET /crews/{id}/zones` zone rows: ZoneView plus the dashboard fields. */
export interface ZoneDetail extends ZoneView {
  description?: string | null;
  color?: string | null;
  files_estimate?: number | null;
  archived_at?: string | null;
}

export interface CommonsRule {
  glob: string;
  kind: 'plain' | 'serialize' | 'append_only';
}

export interface ZonesListing {
  zones: ZoneDetail[];
  commons: CommonsRule[];
  ignore: string[];
  /** True while the no-zone bootstrap's temporary zones are the only zones (D37). */
  bootstrap_zones: boolean;
  pending_zone_changes: string[];
  tree: { tree: RepoTreeNode; node_count: number | null; captured_at: string | null } | null;
}

export interface DiffItem {
  op: 'add' | 'remove' | 'change';
  /** `zone:<slug>`, `commons:<glob>`, `ignore:<glob>` or `enforcement`. */
  target: string;
  field: string | null;
  loosening: boolean;
  reason: string | null;
}

/** `GET /crews/{id}/zone-changes` rows. */
export interface ZoneChange {
  id: string;
  yaml_sha: string;
  state: 'pending' | 'applied' | 'rejected';
  loosening: boolean;
  summary: string | null;
  items: DiffItem[];
  uploaded_by_session: string | null;
  uploaded_by_user: string | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Globs → folders
// ---------------------------------------------------------------------------

const WILD = /[*?[{]/;

/** `src/{a,b}/**` → [`src/a/**`, `src/b/**`] (nested braces expand too; unbalanced text is kept as is). */
export function expandBraces(glob: string): string[] {
  const open = glob.indexOf('{');
  if (open < 0) return [glob];
  let depth = 0;
  let close = -1;
  const commas: number[] = [];
  for (let i = open; i < glob.length; i++) {
    const ch = glob[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) {
        close = i;
        break;
      }
    } else if (ch === ',' && depth === 1) commas.push(i);
  }
  if (close < 0) return [glob];
  const head = glob.slice(0, open);
  const tail = glob.slice(close + 1);
  const parts: string[] = [];
  let start = open + 1;
  for (const c of [...commas, close]) {
    parts.push(glob.slice(start, c));
    start = c + 1;
  }
  const out: string[] = [];
  for (const part of parts) for (const rest of expandBraces(part + tail)) out.push(head + rest);
  return [...new Set(out)];
}

export interface GlobAnchor {
  /** The deepest literal folder the glob starts from ('' = the repository root). */
  dir: string;
  /** The glob also covers every folder below `dir` (it continues with `**`). */
  deep: boolean;
  /** A literal path to one file (no wildcard at all). */
  file: boolean;
}

/**
 * Where a zone glob lives in the folder tree: `src/app/pos/**` anchors at
 * `src/app/pos` (deep), `src/app/pos/*.ts` at `src/app/pos` (files only),
 * `package.json` at the root (one file), `**\/migrations/**` at the root (deep).
 */
export function globAnchors(glob: string): GlobAnchor[] {
  return expandBraces(glob.trim().replace(/^\.\//, '')).map((g) => {
    const segs = g.split('/').filter((s) => s !== '' && s !== '.');
    const lit: string[] = [];
    let i = 0;
    while (i < segs.length && !WILD.test(segs[i])) lit.push(segs[i++]);
    if (i === segs.length) {
      // no wildcard: a literal path. `src/app/` style (trailing slash) was a folder.
      const folder = g.endsWith('/');
      return { dir: folder ? lit.join('/') : lit.slice(0, -1).join('/'), deep: folder, file: !folder };
    }
    return { dir: lit.join('/'), deep: segs[i] === '**', file: false };
  });
}

// ---------------------------------------------------------------------------
// The overlay tree
// ---------------------------------------------------------------------------

export interface TreeRow {
  /** Repo-relative folder path ('' = the repository root). */
  path: string;
  name: string;
  depth: number;
  /** Files directly in this folder, from the snapshot (null for folders only known from a glob). */
  files: number | null;
  /** Zones anchored exactly here. */
  zoneIds: string[];
  /** Zones covering this folder from an ancestor's `**` glob (innermost first). */
  inheritedIds: string[];
  /** Not in the tree snapshot: known only from a zone glob. */
  synthetic: boolean;
  children: TreeRow[];
  /** Zones anchored in this folder or anywhere below it. */
  zonesBelow: number;
}

function sortRows(rows: TreeRow[]): void {
  rows.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  for (const r of rows) sortRows(r.children);
}

/**
 * The repository tree with zones overlaid. Folders come from the snapshot plus
 * every folder a zone glob anchors at (so a crew without a snapshot still gets
 * a tree). The built-in crew-policy zone is left out: the screens pin it
 * separately because part of it lives outside the repository (`~/.remembra`).
 */
export function buildZoneTree(zones: Iterable<ZoneView>, snapshot: RepoTreeNode | null | undefined): TreeRow {
  const root: TreeRow = { path: '', name: '.', depth: 0, files: null, zoneIds: [], inheritedIds: [], synthetic: false, children: [], zonesBelow: 0 };
  const byPath = new Map<string, TreeRow>([['', root]]);

  const ensure = (path: string, synthetic: boolean, files: number | null = null): TreeRow => {
    const hit = byPath.get(path);
    if (hit) return hit;
    const slash = path.lastIndexOf('/');
    const parent = ensure(slash < 0 ? '' : path.slice(0, slash), synthetic);
    const row: TreeRow = {
      path,
      name: slash < 0 ? path : path.slice(slash + 1),
      depth: parent.depth + 1,
      files,
      zoneIds: [],
      inheritedIds: [],
      synthetic,
      children: [],
      zonesBelow: 0,
    };
    parent.children.push(row);
    byPath.set(path, row);
    return row;
  };

  if (snapshot) {
    root.files = typeof snapshot.files === 'number' ? snapshot.files : null;
    const walk = (node: RepoTreeNode, prefix: string) => {
      for (const child of node.children ?? []) {
        if (!child || typeof child.name !== 'string' || !child.name || child.name.includes('/')) continue;
        const path = prefix ? `${prefix}/${child.name}` : child.name;
        ensure(path, false, typeof child.files === 'number' ? child.files : null);
        walk(child, path);
      }
    };
    walk(snapshot, '');
  }

  const deepAnchors: { dir: string; zoneId: string }[] = [];
  for (const zone of zones) {
    if (zone.builtin) continue;
    const seen = new Set<string>();
    for (const glob of zone.include_globs) {
      for (const anchor of globAnchors(glob)) {
        if (!seen.has(anchor.dir)) {
          seen.add(anchor.dir);
          ensure(anchor.dir, true).zoneIds.push(zone.id);
        }
        if (anchor.deep) deepAnchors.push({ dir: anchor.dir, zoneId: zone.id });
      }
    }
  }

  // inherited coverage: a deep anchor covers every folder strictly below it
  for (const row of byPath.values()) {
    const covering = deepAnchors
      .filter((a) => a.dir !== row.path && (a.dir === '' || row.path.startsWith(`${a.dir}/`)) && !row.zoneIds.includes(a.zoneId))
      .sort((a, b) => b.dir.length - a.dir.length)
      .map((a) => a.zoneId);
    row.inheritedIds = [...new Set(covering)];
  }

  const count = (row: TreeRow): number => {
    row.zonesBelow = row.zoneIds.length + row.children.reduce((n, c) => n + count(c), 0);
    return row.zonesBelow;
  };
  count(root);
  sortRows(root.children);
  return root;
}

/** A visible tree row with its box-drawing prefix. */
export interface FlatRow {
  row: TreeRow;
  guide: string;
  parentPath: string | null;
}

/** Visible rows with their box-drawing prefix (`│  ├─ `). */
export function flattenTree(root: TreeRow, expanded: ReadonlySet<string>): FlatRow[] {
  const out: FlatRow[] = [];
  if (root.zoneIds.length) out.push({ row: root, guide: '', parentPath: null });
  const walk = (rows: TreeRow[], lead: string, parent: string | null) => {
    rows.forEach((row, idx) => {
      const last = idx === rows.length - 1;
      out.push({ row, guide: `${lead}${last ? '└─ ' : '├─ '}`, parentPath: parent });
      if (row.children.length && expanded.has(row.path)) walk(row.children, `${lead}${last ? '   ' : '│  '}`, row.path);
    });
  };
  walk(root.children, '', null);
  return out;
}

export function toneOf(status: ZoneStatus | undefined): 'signal' | 'fail' | 'ink' | 'dim' {
  if (!status) return 'dim';
  if (status.primary === 'breach') return 'fail';
  if (status.primary === 'reserved' || status.primary === 'contested' || status.pending) return 'signal';
  if (status.primary === 'free') return 'dim';
  return 'ink';
}

/** Folders to open by default: every folder with a zone anchored somewhere below it. */
export function defaultExpanded(root: TreeRow): Set<string> {
  const open = new Set<string>();
  const walk = (row: TreeRow) => {
    for (const child of row.children) {
      if (child.zonesBelow - child.zoneIds.length > 0) open.add(child.path);
      walk(child);
    }
  };
  walk(root);
  return open;
}

/** The folder rows a zone is anchored at (for "reveal in tree"). */
export function anchorPaths(zone: ZoneView): string[] {
  return [...new Set(zone.include_globs.flatMap((g) => globAnchors(g).map((a) => a.dir)))];
}

/** Every ancestor folder of `path` ('' excluded), outermost first. */
export function ancestorsOf(path: string): string[] {
  const parts = path.split('/').filter(Boolean);
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join('/'));
}

// ---------------------------------------------------------------------------
// Zone status
// ---------------------------------------------------------------------------

export type ZonePrimary = 'breach' | 'frozen' | 'contested' | 'reserved' | 'held' | 'shared' | 'watched' | 'free';

/** Status words used on screen (status never depends on colour alone, §9). */
export const PRIMARY_LABEL: Record<ZonePrimary, string> = {
  breach: 'breach',
  frozen: 'frozen',
  contested: 'contested',
  reserved: 'reserved',
  held: 'held',
  shared: 'shared',
  watched: 'watched',
  free: 'free',
};

/** Collision kinds that mean someone wrote where they must not (§5.3 severities high and critical). */
export const BREACH_KINDS: readonly string[] = ['exclusive_breach', 'same_worktree_file', 'foreign_checkout_write', 'stale_epoch_write'];

export interface ZoneStatus {
  primary: ZonePrimary;
  /** Active or offered claims (the holders). */
  holders: ClaimView[];
  /** Queued or requested claims, by queue position. */
  waiters: ClaimView[];
  /** The reserved claim (the baton), if any. */
  reserved: ClaimView | null;
  /** Open or acknowledged breach collisions on this zone. */
  breaches: CollisionView[];
  frozen: boolean;
  /** A pending zone change touches this zone (or project enforcement). */
  pending: boolean;
  /** One line, ids/slugs/callsigns only: `held EXCLUSIVELY by codex-1 for T-14 · active`. */
  line: string;
}

export function isFrozen(zone: Pick<ZoneView, 'frozen_by' | 'frozen_until'>, nowMs: number): boolean {
  if (!zone.frozen_by) return false;
  if (!zone.frozen_until) return true;
  const until = Date.parse(zone.frozen_until);
  return Number.isNaN(until) || until > nowMs;
}

/** Slugs touched by pending zone changes, plus whether one changes project enforcement. */
export function pendingTargets(changes: readonly ZoneChange[]): { slugs: Set<string>; enforcement: boolean } {
  const slugs = new Set<string>();
  let enforcement = false;
  for (const change of changes) {
    if (change.state !== 'pending') continue;
    for (const item of change.items ?? []) {
      if (item.target === 'enforcement') enforcement = true;
      else if (item.target.startsWith('zone:')) slugs.add(item.target.slice(5));
    }
  }
  return { slugs, enforcement };
}

export function zoneStatus(state: CrewState, zone: ZoneView, pendingSlugs: ReadonlySet<string>, nowMs: number): ZoneStatus {
  const claims = Object.values(state.claims).filter((c) => c.zone_id === zone.id);
  const holders = claims.filter((c) => c.state === 'active' || c.state === 'offered');
  const waiters = claims
    .filter((c) => c.state === 'queued' || c.state === 'requested')
    .sort((a, b) => (a.queue_pos ?? 0) - (b.queue_pos ?? 0) || a.id.localeCompare(b.id));
  const reserved = claims.find((c) => c.state === 'reserved') ?? null;
  const breaches = Object.values(state.collisions).filter(
    (c) => c.zone_id === zone.id && BREACH_KINDS.includes(c.kind) && (c.state === 'open' || c.state === 'acknowledged'),
  );
  const frozen = isFrozen(zone, nowMs);
  let primary: ZonePrimary = 'free';
  if (breaches.length) primary = 'breach';
  else if (frozen) primary = 'frozen';
  else if ((holders.length || reserved) && waiters.length) primary = 'contested';
  else if (reserved) primary = 'reserved';
  else if (holders.some((c) => c.mode === 'exclusive')) primary = 'held';
  else if (holders.some((c) => c.mode === 'shared')) primary = 'shared';
  else if (holders.length) primary = 'watched';
  return {
    primary,
    holders,
    waiters,
    reserved,
    breaches,
    frozen,
    pending: pendingSlugs.has(zone.slug),
    line: describeHolder(state, zone),
  };
}

/** `codex-1 (enforced)` or `a human` for a claim's holder. */
export function holderLabel(state: CrewState, claim: ClaimView): string {
  if (claim.holder_kind === 'human') return 'a human';
  const session = claim.holder_session_id ? state.sessions[claim.holder_session_id] : undefined;
  if (!session) return claim.holder_agent_id ?? claim.holder_session_id ?? 'a session';
  const parent = session.parent_session_id ? state.sessions[session.parent_session_id]?.callsign : undefined;
  return `${session.callsign} (${beforeWriteLabel(session)}${session.parent_session_id ? `, sub-agent of ${parent ?? 'another session'}` : ''})`;
}

/** Local wall-clock time, `14:02`. */
export function clockTime(value: string | null | undefined): string {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/**
 * The tree row's holder text (§9.5): `held by codex-1 (enforced) since 14:02 · T-14`,
 * `reserved for the next pickup of T-12 (quota) · held until picked up`, `frozen by a human`, `free`.
 */
export function rowHolderText(state: CrewState, zone: ZoneView, status: ZoneStatus): string {
  const parts: string[] = [];
  if (status.frozen) parts.push('frozen by a human');
  for (const b of status.breaches) {
    const who = b.session_b ? state.sessions[b.session_b]?.callsign ?? b.session_b : b.session_a ? state.sessions[b.session_a]?.callsign ?? b.session_a : 'an agent';
    parts.push(`breach: ${b.kind.replace(/_/g, ' ')} by ${who}`);
  }
  for (const claim of status.holders) {
    if (claim.holder_kind === 'human' && status.frozen) continue;
    const verb = claim.mode === 'exclusive' ? 'held' : claim.mode === 'shared' ? 'shared' : 'watched';
    const since = clockTime(claim.granted_at);
    const task = taskRef(state, claim.task_id);
    const offered = claim.state === 'offered' ? ' (handover offered)' : '';
    parts.push(`${verb} by ${holderLabel(state, claim)}${since ? ` since ${since}` : ''}${task ? ` · ${task}` : ''}${offered}`);
  }
  if (status.reserved) {
    const task = taskRef(state, status.reserved.task_id);
    const reason = status.reserved.reserve_reason ? ` (${status.reserved.reserve_reason.replace('_', ' ')})` : '';
    parts.push(`reserved for the next pickup${task ? ` of ${task}` : ''}${reason} · held until picked up`);
  }
  if (status.waiters.length) parts.push(`${status.waiters.length} waiting`);
  if (!parts.length) return zone.protected ? 'free · protected: needs a human grant' : 'free';
  return parts.join(' · ');
}

// ---------------------------------------------------------------------------
// Lease, enforcement layers
// ---------------------------------------------------------------------------

/** `8m left`, `40s left`, `expired 2m ago`; null without a lease. */
export function leaseText(claim: Pick<ClaimView, 'lease_expires_at'>, nowMs: number): string | null {
  if (!claim.lease_expires_at) return null;
  const at = Date.parse(claim.lease_expires_at);
  if (Number.isNaN(at)) return null;
  const s = Math.round((at - nowMs) / 1000);
  const fmt = (n: number) => (n >= 3600 ? `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m` : n >= 60 ? `${Math.floor(n / 60)}m` : `${n}s`);
  return s >= 0 ? `${fmt(s)} left` : `expired ${fmt(-s)} ago`;
}

/** The enforcement layers for one session's adapter (§8.5), as short text. */
export function enforcementLayers(session: Pick<SessionState, 'adapter_enforcement' | 'client_kind' | 'githook_state'>): {
  beforeWrite: string;
  commit: string;
  push: string;
} {
  const hooks = session.githook_state === 'ok' || session.githook_state === 'chained';
  const gate = session.githook_state === 'missing' ? 'missing' : hooks ? 'enforced' : 'unknown';
  return { beforeWrite: beforeWriteLabel(session), commit: gate, push: gate };
}

export function sessionPresence(session: SessionState | undefined): string {
  return session ? presenceText(session) : 'ended';
}
