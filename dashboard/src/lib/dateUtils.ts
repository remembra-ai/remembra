/**
 * Date utilities for consistent timezone handling across the dashboard.
 * All timestamps from the API are in UTC (ISO 8601 format).
 * These utilities ensure proper conversion to local time.
 */

/**
 * Ensure a date string is interpreted as UTC
 */
export function ensureUTC(dateStr: string): string {
  if (!dateStr) return dateStr;
  return dateStr.endsWith('Z') ? dateStr : dateStr + 'Z';
}

/**
 * Parse a date string ensuring UTC interpretation
 */
export function parseUTC(dateStr: string): Date {
  return new Date(ensureUTC(dateStr));
}

/**
 * Format a date to local date string
 */
export function formatLocalDate(dateStr: string, options?: Intl.DateTimeFormatOptions): string {
  const date = parseUTC(dateStr);
  return date.toLocaleDateString('en-US', {
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    ...options,
  });
}

/**
 * Format a date to local time string
 */
export function formatLocalTime(dateStr: string, options?: Intl.DateTimeFormatOptions): string {
  const date = parseUTC(dateStr);
  return date.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    ...options,
  });
}

/**
 * Format a date to local date and time string
 */
export function formatLocalDateTime(dateStr: string): string {
  const date = parseUTC(dateStr);
  return date.toLocaleString('en-US', {
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  });
}

/**
 * Get relative time (e.g., "2h ago", "3d ago")
 */
export function formatRelativeTime(dateStr: string): string {
  const date = parseUTC(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 0) return 'just now'; // Handle slight server time drift
  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  
  return formatLocalDate(dateStr, { month: 'short', day: 'numeric' });
}
