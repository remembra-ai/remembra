// Time helpers for the relay pages. Server timestamps are UTC; naive ones
// (no zone suffix) are treated as UTC, never as the viewer's local time.

const ZONED = /(Z|[+-]\d{2}:?\d{2})$/i;

export function parseServerTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const text = value.trim().replace(' ', 'T');
  const date = new Date(ZONED.test(text) ? text : `${text}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "just now", "4m ago", "2h ago", "yesterday", "3d ago", then "Sep 12". */
export function relativeTime(value: string | Date | null | undefined, now: Date = new Date()): string {
  const date = value instanceof Date ? value : parseServerTime(value);
  if (!date) return 'unknown time';
  const seconds = Math.round((now.getTime() - date.getTime()) / 1000);
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  });
}

export function absoluteTime(value: string | Date | null | undefined): string {
  const date = value instanceof Date ? value : parseServerTime(value);
  if (!date) return '';
  return date.toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export function minutesSince(value: string | null | undefined, now: Date = new Date()): number | null {
  const date = parseServerTime(value);
  return date ? (now.getTime() - date.getTime()) / 60000 : null;
}

/** Local calendar-day label for grouping a trail: "Today", "Yesterday", "Tue, Sep 23". */
export function dayLabel(value: string | null | undefined, now: Date = new Date()): string {
  const date = parseServerTime(value);
  if (!date) return 'Unknown day';
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((startOf(now) - startOf(date)) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return date.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  });
}

export function greeting(now: Date = new Date()): string {
  const hour = now.getHours();
  if (hour < 5) return 'Working late';
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

export function shortSha(sha: string | null | undefined): string {
  return (sha || '').slice(0, 7);
}

/** "main@1d50ae3", "main", "@1d50ae3" or "" */
export function where(branch: string | null | undefined, sha: string | null | undefined): string {
  const b = (branch || '').trim();
  const s = shortSha(sha);
  if (b && s) return `${b}@${s}`;
  return b || (s ? `@${s}` : '');
}
