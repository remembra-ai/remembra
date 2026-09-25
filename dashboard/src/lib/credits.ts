// Plain-language reading of GET /cloud/usage/summary: smart credits for the
// current period (a month, or the whole year banked up front on annual
// plans), what "degraded" means, and the plan line.

import type { UsageSummaryResponse } from './api';

export interface CreditsView {
  limit: number;
  used: number;
  reserved: number;
  remaining: number;
  /** 0..1 of the period's credits spent (settled). */
  usedFraction: number;
  /** 0..1 held for enrichment still running. */
  reservedFraction: number;
  /** 10% or less left (and not already degraded). */
  low: boolean;
  degraded: boolean;
  yearly: boolean;
}

export function creditsView(summary: UsageSummaryResponse): CreditsView {
  const { limit, used, reserved, remaining } = summary.credits;
  const safeLimit = Math.max(0, limit);
  const usedFraction = safeLimit > 0 ? Math.min(1, Math.max(0, used / safeLimit)) : 0;
  const reservedFraction = safeLimit > 0 ? Math.min(1 - usedFraction, Math.max(0, reserved / safeLimit)) : 0;
  const degraded = summary.enrichment.status === 'degraded';
  return {
    limit: safeLimit,
    used,
    reserved,
    remaining: Math.max(0, remaining),
    usedFraction,
    reservedFraction,
    low: !degraded && safeLimit > 0 && remaining / safeLimit <= 0.1,
    degraded,
    yearly: summary.credits.bank === 'yearly',
  };
}

function parseDate(iso: string): Date | null {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** When the credits come back: "Resets Oct 1" or "Yearly bank, renews Sep 1, 2027". */
export function resetLabel(summary: UsageSummaryResponse): string {
  const end = parseDate(summary.period.end);
  if (!end) return summary.credits.bank === 'yearly' ? 'Yearly bank' : 'Monthly allowance';
  if (summary.credits.bank === 'yearly') {
    return `Yearly bank, renews ${end.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })}`;
  }
  return `Resets ${end.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })}`;
}

export interface DegradedCopy {
  title: string;
  body: string;
}

/** What degraded means, in words; null when enrichment is running normally. */
export function degradedCopy(summary: UsageSummaryResponse): DegradedCopy | null {
  if (summary.enrichment.status !== 'degraded') return null;
  const reset = resetLabel(summary).replace(/^Resets /, 'on ').replace(/^Yearly bank, renews /, 'on ');
  if (summary.enrichment.reason === 'free_breaker_open') {
    return {
      title: 'Free-tier enrichment is paused for now',
      body:
        "The free tier's shared AI budget for this month is spent, so new memories are saved without enrichment until it refills. " +
        'Relay, pickups, inbox, trail and recall keep working. Paid plans are never paused.',
    };
  }
  return {
    title: 'Out of smart credits: stores are degraded',
    body:
      `New memories still save and stay searchable, but skip AI enrichment (fact extraction and entity linking) until credits come back ${reset}. ` +
      'Relay, pickups, inbox, trail and recall never use credits and keep working.',
  };
}

/** "Solo, yearly" / "Team, 4 seats, monthly" / "Relay Free". */
export function planLine(summary: UsageSummaryResponse): string {
  const parts = [summary.plan_name || summary.plan];
  if (summary.plan === 'team' || summary.seats > 1) parts.push(`${summary.seats} seat${summary.seats === 1 ? '' : 's'}`);
  if (summary.plan !== 'free') parts.push(summary.interval === 'year' ? 'yearly' : 'monthly');
  if (summary.founding) parts.push('Founding 100');
  return parts.join(', ');
}

/** USD cents to "$12" / "$12.50". */
export function formatUsd(cents: number): string {
  const dollars = cents / 100;
  return Number.isInteger(dollars) ? `$${dollars.toLocaleString()}` : `$${dollars.toFixed(2)}`;
}

/** Team seats are sold with a floor; clamp any input to it. */
export function clampSeats(value: number, minSeats: number, maxSeats = 1000): number {
  if (!Number.isFinite(value)) return minSeats;
  return Math.min(maxSeats, Math.max(minSeats, Math.round(value)));
}
