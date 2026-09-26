// Crew mode wire types (spec §3, §4, §6). The source of truth is
// `remembra.crew.schemas` on the server (see docs/crew/openapi.json); these
// mirror the entity views, the event envelope, the snapshot and the WebSocket
// frames the dashboard consumes. Every object from the server is closed and
// never carries tokens or raw commands.

export type PresenceState = 'joining' | 'active' | 'idle' | 'quiet' | 'lost' | 'ended' | 'quota_blocked' | 'paused';
export type QuietReason = 'host_unreachable' | 'mcp_silent';
export type CrewMode = 'solo' | 'multi';
export type EnforcementLevel = 'off' | 'observe' | 'enforce';
export type AdapterEnforcement = 'enforced' | 'advisory';
export type GithookState = 'ok' | 'missing' | 'chained' | 'unknown';
export type HostState = 'online' | 'unreachable' | 'retired';
export type LimitLevel = 'ok' | 'warn' | 'critical' | 'exhausted';
export type LimitSource = 'reported' | 'detected' | 'inferred';
export type ClaimMode = 'exclusive' | 'shared' | 'watch';
export type ClaimState =
  | 'requested'
  | 'queued'
  | 'active'
  | 'offered'
  | 'reserved'
  | 'released'
  | 'expired'
  | 'revoked'
  | 'denied';
export type ReserveReason = 'lost' | 'quota' | 'ended_dirty' | 'baton' | 'human_hold' | 'idle' | 'offline';
export type TaskStatus =
  | 'backlog'
  | 'ready'
  | 'claimed'
  | 'in_progress'
  | 'blocked'
  | 'review'
  | 'done'
  | 'stalled'
  | 'cancelled';
export type FactsSource = 'relay-cli' | 'agent-declared' | 'server-inferred' | 'server-verified';
export type Severity = 'info' | 'notice' | 'low' | 'medium' | 'high' | 'critical';
export type CollisionState = 'open' | 'acknowledged' | 'resolved' | 'dismissed';
export type DecisionState = 'proposed' | 'in_force' | 'rejected' | 'superseded';
export type MessageKind = 'chat' | 'note' | 'question' | 'answer' | 'status' | 'decision' | 'request_release' | 'system';
export type AuthorKind = 'agent' | 'human' | 'system';
export type InboxAudience = 'project' | 'crew' | 'session';
export type InboxState = 'open' | 'seen' | 'claimed' | 'resolved' | 'dismissed';
export type CrewRole = 'owner' | 'admin' | 'member' | 'viewer';
export type CrewPermission = 'crew:read' | 'crew:write' | 'crew:claim' | 'crew:override' | 'crew:admin';

export const LIVE_PRESENCE_STATES: readonly PresenceState[] = ['joining', 'active', 'idle', 'quiet', 'quota_blocked', 'paused'];
export const LIVE_CLAIM_STATES: readonly ClaimState[] = ['requested', 'queued', 'active', 'offered', 'reserved'];
export const LIVE_COLLISION_STATES: readonly CollisionState[] = ['open', 'acknowledged'];
export const LIVE_DECISION_STATES: readonly DecisionState[] = ['proposed', 'in_force'];
export const LIVE_INBOX_STATES: readonly InboxState[] = ['open', 'seen', 'claimed'];

export interface CrewView {
  id: string;
  project_id: string;
  name?: string | null;
  mode: CrewMode;
  enforcement: EnforcementLevel;
  settings_version: number;
  last_seq: number;
}

export interface LimitView {
  level: LimitLevel;
  pct?: number | null;
  source: LimitSource;
}

export interface LastAction {
  tool: string;
  path_rel?: string | null;
  verb?: string | null;
  age_s: number;
}

export interface SessionView {
  id: string;
  callsign: string;
  agent_id: string;
  member_key: string;
  agent_verified: boolean;
  adapter?: string | null;
  adapter_enforcement: AdapterEnforcement;
  client_kind?: 'hook' | 'mcp' | 'cli' | null;
  model?: string | null;
  host_id?: string | null;
  state: PresenceState;
  quiet_reason?: QuietReason | null;
  state_reason?: string | null;
  stuck: boolean;
  branch?: string | null;
  head_commit?: string | null;
  worktree_id?: string | null;
  githook_state?: GithookState | null;
  current_task_id?: string | null;
  limit?: LimitView | null;
  joined_at: string;
  last_activity_at?: string | null;
  ended_at?: string | null;
  end_reason?: string | null;
}

/** A presence lane (ephemeral, ≤1 per session per 5 s; §4.4). */
export interface PresenceLane {
  session_id: string;
  state: PresenceState;
  stuck: boolean;
  last_action?: LastAction | null;
  calls_since_checkpoint: number;
  next_checkpoint_due_at?: string | null;
  limit?: LimitView | null;
}

export type PresenceOverlay = Omit<PresenceLane, 'session_id'>;

/** A session in reducer state: the view plus the ephemeral presence overlay. */
export interface SessionState extends SessionView {
  presence: PresenceOverlay | null;
}

export interface ClaimView {
  id: string;
  zone_id?: string | null;
  path_glob?: string | null;
  resource?: string | null;
  mode: ClaimMode;
  holder_kind: 'session' | 'human';
  holder_session_id?: string | null;
  holder_agent_id?: string | null;
  holder_user_id?: string | null;
  task_id?: string | null;
  state: ClaimState;
  source: string;
  epoch: number;
  unconfirmed: boolean;
  fenced: boolean;
  lease_expires_at?: string | null;
  reserve_reason?: ReserveReason | null;
  reserved_for?: string | null;
  offered_to?: string | null;
  queue_pos?: number | null;
  baton_ref?: string | null;
  granted_at?: string | null;
  version: number;
}

export interface McpToolRule {
  tool: string;
  service?: string | null;
}

export interface ZoneView {
  id: string;
  slug: string;
  title: string;
  parent_id?: string | null;
  is_leaf: boolean;
  builtin: boolean;
  include_globs: string[];
  exclude_globs: string[];
  services: string[];
  command_patterns: string[];
  mcp_tools: McpToolRule[];
  mode: ClaimMode;
  auto_claim: boolean;
  protected: boolean;
  reserve_for?: string | null;
  fail_closed: boolean;
  frozen_by?: string | null;
  frozen_note?: string | null;
  frozen_until?: string | null;
  source: 'repo' | 'dashboard' | 'api' | 'suggested' | 'builtin';
  version: number;
}

export interface CommonsEntry {
  glob: string;
  kind: 'plain' | 'serialize' | 'append_only';
}

export interface Criterion {
  id: string;
  text: string;
  kind: 'test' | 'command' | 'file' | 'commit' | 'deploy' | 'manual';
  match?: string | null;
  url?: string | null;
  required: boolean;
}

export interface TaskView {
  id: string;
  number: number;
  title: string;
  status: TaskStatus;
  status_before_stall?: TaskStatus | null;
  phase?: string | null;
  priority: number;
  zone_ids: string[];
  owner_session_id?: string | null;
  owner_agent_id?: string | null;
  reviewer?: string | null;
  depends_on: string[];
  acceptance: Criterion[];
  acceptance_locked: boolean;
  started_head?: string | null;
  current_report_id?: string | null;
  blocked_reason?: string | null;
  version: number;
}

export interface CollisionView {
  id: string;
  kind: string;
  severity: Severity;
  subject: string;
  zone_id?: string | null;
  session_a?: string | null;
  session_b?: string | null;
  claim_id?: string | null;
  attribution?: 'certain' | 'probable' | null;
  state: CollisionState;
  escalated: boolean;
  resolution?: string | null;
}

export interface DecisionView {
  id: string;
  number: number;
  title: string;
  decision: string;
  state: DecisionState;
  source: 'direct' | 'proposal' | 'override';
  decided_by_kind: AuthorKind;
  decided_by: string;
  confirmed_by?: string | null;
  task_id?: string | null;
  zone_id?: string | null;
  supersedes_id?: string | null;
}

export interface MessageView {
  id: string;
  seq: number;
  thread_root_id?: string | null;
  reply_to_id?: string | null;
  kind: MessageKind;
  author_kind: AuthorKind;
  author_session_id?: string | null;
  author_agent_id?: string | null;
  author_verified: boolean;
  body: string;
  body_truncated: boolean;
  mentions: string[];
  refs: string[];
  edited: boolean;
  redacted: boolean;
  pinned: boolean;
}

export interface InboxItemView {
  id: string;
  audience: InboxAudience;
  recipient?: string | null;
  kind: string;
  origin: 'server' | 'human' | 'agent';
  ref_type?: string | null;
  ref_id?: string | null;
  priority: number;
  title: string;
  primary_action?: string | null;
  state: InboxState;
  claimed_by?: string | null;
  coalesced_count: number;
}

export interface CriterionResult {
  id: string;
  status: 'met' | 'waived' | 'unmet' | 'unknown';
  source?: FactsSource | null;
}

export interface ReportView {
  id: string;
  task_id: string;
  session_id?: string | null;
  kind: 'completion' | 'partial' | 'stalled' | 'waived';
  verdict?: 'complete' | 'partial' | null;
  review_state?: 'accepted' | 'review' | 'rejected' | 'waived' | null;
  is_current: boolean;
  superseded_reason?: string | null;
  facts_source: FactsSource;
  criteria: CriterionResult[];
  baton_ref?: string | null;
  handoff_id?: string | null;
}

export interface CheckpointView {
  id: string;
  session_id: string;
  task_id?: string | null;
  trigger: string;
  headline: string;
  facts_source: FactsSource;
}

export interface HostView {
  id: string;
  host_label: string;
  platform?: string | null;
  crewd_version?: string | null;
  state: HostState;
}

export interface OfferView {
  id: string;
  claim_id: string;
  task_id?: string | null;
  to_session: string;
  via: 'brief' | 'human' | 'reserved_for';
}

export interface Blocker {
  claim_id?: string | null;
  zone_id?: string | null;
  holder_session_id?: string | null;
  holder_callsign?: string | null;
  task_id?: string | null;
  reason: string;
}

export interface FootprintView {
  session_id: string;
  worktree_id?: string | null;
  path: string;
  state: 'dirty' | 'committed' | 'landed';
  attribution: 'certain' | 'probable';
}

export interface Actor {
  kind: 'session' | 'human' | 'system';
  id: string;
  callsign?: string | null;
  agent_id?: string | null;
  user_id?: string | null;
  verified: boolean;
}

export interface EventRefs {
  zone_id?: string | null;
  task_id?: string | null;
  claim_id?: string | null;
  session_id?: string | null;
  report_id?: string | null;
  collision_id?: string | null;
  message_id?: string | null;
  decision_id?: string | null;
  inbox_item_id?: string | null;
  host_id?: string | null;
}

/** The stored and streamed event envelope (§4.1). */
export interface CrewEvent {
  seq: number;
  id: string;
  crew_id: string;
  project_id: string;
  ts: string;
  type: string;
  v: number;
  origin: 'server' | 'client';
  actor: Actor;
  refs: EventRefs;
  severity: Severity;
  moment: boolean;
  summary: string;
  payload: Record<string, unknown>;
}

/** `GET /crews/{id}/snapshot` (docs/crew/snapshot.md). */
export interface CrewSnapshot {
  crew: CrewView;
  server_time: string;
  as_of_seq: number;
  etag: string;
  sessions: SessionView[];
  claims: ClaimView[];
  zones: ZoneView[];
  commons: CommonsEntry[];
  ignore: string[];
  tasks: TaskView[];
  collisions: CollisionView[];
  decisions: DecisionView[];
  offers: OfferView[];
  footprints: FootprintView[];
  inbox_counts: { project: number; crew: number };
  pending_zone_changes: string[];
}

export interface CrewSummaryItem {
  crew_id: string;
  project_id: string;
  mode: CrewMode;
  live: number;
  moments: number;
  needs_you: number;
}

export type ResyncReason = 'gap_too_large' | 'overflow' | 'server_restart';

/** Frames the server sends on `/ws` for crew subscriptions, plus the client-local snapshot frame. */
export type CrewFrame =
  | { type: 'crew.event'; crew_id: string; data: CrewEvent }
  | { type: 'presence'; crew_id: string; lanes: PresenceLane[] }
  | { type: 'resync_required'; crew_id: string; reason: ResyncReason; last_seq: number }
  | { type: 'crew.subscribed'; crew_id: string; since_seq: number; replayed: number }
  | { type: 'crew.summary'; crews: CrewSummaryItem[] }
  | { type: 'snapshot'; data: CrewSnapshot };

/** A crew error frame (`{"type":"error","data":{"channel":"crew",…}}`). */
export interface CrewErrorFrame {
  type: 'error';
  data: { channel?: string; crew_id?: string | null; code?: string; message?: string; retry_after_s?: number };
}

// ---------------------------------------------------------------------------
// Reducer state (docs/crew/reducer.md). Plain JSON, identical to the Python
// reference reducer value for value.
// ---------------------------------------------------------------------------

export interface MomentEntry {
  seq: number;
  type: string;
  summary: string;
  ts: string | null;
}

export interface BatonEntry {
  seq: number;
  ts: string | null;
  [key: string]: unknown;
}

export interface BatonRefEntry {
  seq: number;
  task_id: string | null;
  session_id: string | null;
  dirty_files: number;
  unpushed: number;
}

export interface BudgetEntry {
  used: number;
  limit: number;
  capped: boolean;
}

export interface CrewState {
  crew: CrewView | null;
  mode: CrewMode;
  last_seq: number;
  needs_resync: boolean;
  resync_reason: 'gap' | 'server' | null;
  sessions: Record<string, SessionState>;
  claims: Record<string, ClaimView>;
  zones: Record<string, ZoneView>;
  tasks: Record<string, TaskView>;
  collisions: Record<string, CollisionView>;
  decisions: Record<string, DecisionView>;
  offers: Record<string, OfferView>;
  inbox: Record<string, InboxItemView>;
  inbox_counts: { project: number; crew: number };
  reports: Record<string, ReportView>;
  checkpoints: Record<string, CheckpointView>;
  hosts: Record<string, { id: string; state: HostState }>;
  pending_zone_changes: Record<string, { loosening: boolean | null }>;
  messages: MessageView[];
  moments: MomentEntry[];
  batons: BatonEntry[];
  baton_refs: Record<string, BatonRefEntry>;
  guard_blocks: Record<string, number>;
  tamper_blocks: Record<string, number>;
  budget: Record<string, BudgetEntry>;
}

// ---------------------------------------------------------------------------
// REST responses (§6) the dashboard reads.
// ---------------------------------------------------------------------------

export interface CrewListItem {
  crew: CrewView;
  role: CrewRole;
  live: number;
  needs_you: number;
  crew_inbox: number;
  moments_24h: number;
  last_event_at: string | null;
  tasks_by_status: Partial<Record<TaskStatus, number>>;
  phases: { phase: string | null; total: number; done: number }[];
  live_sessions: SessionView[];
  live_sessions_truncated: boolean;
}

export interface CrewListResponse {
  crews: CrewListItem[];
  count: number;
}

export interface CrewAccessBody {
  role: CrewRole;
  permissions: CrewPermission[];
  human: boolean;
}

export interface CrewDetail extends CrewAccessBody {
  crew: CrewView;
  settings: Record<string, unknown>;
  settings_version: number;
  members: number;
  created_at: string;
}

export interface CrewResolveResponse {
  crew: CrewView;
  project_id: string;
  role: CrewRole;
  permissions: CrewPermission[];
  resolution: Record<string, unknown> | null;
}

export interface CrewEventsPage {
  crew_id: string;
  events: CrewEvent[];
  last_seq: number;
  has_more: boolean;
}
