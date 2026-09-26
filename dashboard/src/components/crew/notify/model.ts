// Notifications model (spec §9.11): the in-app list behind the bell and the
// human-set real-time targets (instant email, signed https webhook).

import { crewHref } from '../../../lib/crew/routes';

/** One row of `GET /notifications` (remembra.crew.notify.list_notifications). */
export interface NotificationItem {
  crew_id: string;
  project_id: string;
  seq: number;
  kind: string;
  event_type: string;
  /** Also delivered by email or webhook (real-time kinds). */
  realtime: boolean;
  /** Server template text (callsigns, slugs, counts). */
  text: string;
  link: string;
  ts: string;
  read: boolean;
}

export interface NotificationList {
  items: NotificationItem[];
  unread: number;
}

export interface NotifyTarget {
  id: string;
  kind: 'email' | 'webhook';
  target: string;
  verified_at: string | null;
  created_at: string;
}

/** `POST /notifications/targets` response: the target, plus the webhook secret shown exactly once. */
export interface AddedTarget extends NotifyTarget {
  signing_secret?: string;
  signature_header?: string;
  /** Crews (owner/admin) whose `settings.notify.realtime` does not include this kind yet. */
  crews_without_channel?: string[];
}

export interface NotificationRules {
  defaults: { kind: string; realtime: boolean; channels: string[] }[];
  rules: unknown[];
  targets: NotifyTarget[];
  batch_window_s: number;
  quiet_hours_tz: string;
}

export const KIND_TITLE: Record<string, string> = {
  handoff: 'Auto-handoff',
  collision: 'Collision',
  tamper: 'Tamper blocked',
  bypass: 'Bypass used',
  githook: 'Git gate missing',
  zone_change: 'Zone change',
  decision: 'Decision to confirm',
  stuck: 'Stuck agent',
  checkpoint_missed: 'Checkpoint missed',
  task_done: 'Task done',
};

export function kindTitle(kind: string): string {
  return KIND_TITLE[kind] ?? kind.replace(/_/g, ' ');
}

/** Where a notification opens in this dashboard (the crew feed, or Needs-you for decisions and zone changes). */
export function notificationHref(item: Pick<NotificationItem, 'kind' | 'project_id'>): string {
  if (item.kind === 'decision') return crewHref(item.project_id, 'channel');
  if (item.kind === 'zone_change') return crewHref(item.project_id, 'policy');
  if (item.kind === 'task_done') return crewHref(item.project_id, 'board');
  return crewHref(item.project_id, 'feed', { feed: { moments: item.kind === 'handoff' } });
}

/** Badge text: nothing for 0, the count up to 99, then "99+". */
export function badgeText(unread: number): string | null {
  if (!Number.isFinite(unread) || unread <= 0) return null;
  return unread > 99 ? '99+' : String(Math.trunc(unread));
}

/** The newest unread seq per crew in a list (what "mark read" advances each crew's cursor to). */
export function unreadUpto(items: readonly NotificationItem[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const item of items) {
    if (item.read) continue;
    out[item.crew_id] = Math.max(out[item.crew_id] ?? 0, item.seq);
  }
  return out;
}

/** Mark one crew read up to a seq locally (optimistic, before the server answers). */
export function markReadLocally(list: NotificationList, crewId: string | null, uptoSeq: number | null): NotificationList {
  let cleared = 0;
  const items = list.items.map((item) => {
    const hit = crewId === null || (item.crew_id === crewId && (uptoSeq === null || item.seq <= uptoSeq));
    if (hit && !item.read) {
      cleared += 1;
      return { ...item, read: true };
    }
    return item;
  });
  return { items, unread: crewId === null ? 0 : Math.max(0, list.unread - cleared) };
}

// Same rule as the server (notify.py EMAIL_RE); the server re-validates.
const EMAIL_RE = /^[^@\s<>"']{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$/;

/** A problem with a target the user typed, or null when it may be sent. */
export function targetProblem(kind: 'email' | 'webhook', raw: string): string | null {
  const target = raw.trim();
  if (!target) return kind === 'email' ? 'Type an email address.' : 'Paste the https URL of your webhook.';
  if (kind === 'email') {
    if (target.length > 254 || !EMAIL_RE.test(target)) return 'That does not look like an email address.';
    return null;
  }
  if (!/^https:\/\//i.test(target)) return 'Webhooks must use https:// (plain http is refused).';
  let url: URL;
  try {
    url = new URL(target);
  } catch {
    return 'That is not a valid URL.';
  }
  if (target.length > 2048) return 'That URL is too long (2,048 characters at most).';
  if (url.username || url.password) return 'Leave credentials out of the URL; every call is signed instead.';
  const host = url.hostname.toLowerCase();
  if (host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') || /^(127\.|10\.|192\.168\.|169\.254\.|0\.)/.test(host) || host === '[::1]') {
    return 'The server only calls public addresses; a local or private host will be refused.';
  }
  return null;
}

/** The receiver check the owner copies into their webhook (Clawdbot/Telegram bridge). */
export function webhookRecipe(header: string): string {
  return [
    `1. Compute HMAC-SHA256(secret, raw request body) and compare it with the ${header} header ("sha256=<hex>").`,
    '2. For a "crew.notification.challenge" body, reply 200 with {"challenge": "<the challenge value>"}.',
    '3. Reject bodies whose sent_at is older than 5 minutes, and ignore delivery ids you have seen.',
  ].join('\n');
}
